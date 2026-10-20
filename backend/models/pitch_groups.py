"""Pitch groups for D1: Fastball / Breaking / Offspeed, numbered within each pitcher-season.

The production copy of the research tagger. ``backend/research/`` is git-ignored, so the code the stuff
model depends on lives here. It is ``backend/research/stuff_model/plan_b.py`` (Plan B/D: a
template-anchored Gaussian mixture with rule guardrails), plus the two pieces of ``plan_a.py`` it used
(``fill_pitcher_ids`` and the ΔSpeed features). The logic is unchanged; the research notebooks keep
their own copies. ``plan_b.ipynb`` walks through it, and ``stuff_primer.md`` §5 has the design.

1. **Templates** (population level): one per pitch type, each a center, a between-pitcher spread and
   a within-pitch ellipse, built with robust statistics from the operator tags, then re-estimated
   from the fits (empirical Bayes).
2. **Per pitcher-season**: a MAP-EM Gaussian mixture whose components belong to groups, pulled toward
   the templates, scored by ICL. Plan D's rules veto layouts that break a definition (Fastball_1 =
   the fastest component with >= 10% of pitches, cutters are breaking balls, ...).

Training builds the templates once (``build_templates``). Scoring new pitches reuses the frozen
templates (``label``), so a pitcher-season is tagged the same way the training data was.

    T, labels = build_templates(raw)          # templates from tags, pass 1, refresh, pass 2
    labels    = label(raw, T)                 # frozen templates, one pass
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import polars as pl

TAG_GROUP = {"Fastball": "FB", "FourSeamFastBall": "FB", "TwoSeamFastBall": "FB", "OneSeamFastBall": "FB", "Sinker": "FB",
             "Slider": "BB", "Curveball": "BB", "Sweeper": "BB", "Cutter": "BB",
             "ChangeUp": "OS", "Splitter": "OS"}
GROUP_NAME = {"FB": "Fastball", "BB": "Breaking", "OS": "Offspeed"}
K_SHRINK = 15          # outing of n pitches: weight n/(n+15) on its own p95 velo, the rest on the season p95
UNIT = ["PitcherId", "Year"]

_RAW_COLS = ["GameID", "PitchUID", "PitcherId", "Pitcher", "PitcherThrows", "TaggedPitchType",
             "RelSpeed", "SpinRate", "InducedVertBreak", "HorzBreak", "Year"]


# ── Ids and ΔSpeed (from plan_a) ───────────────────────────────────────────────────────────────────
def fill_pitcher_ids(df: pl.DataFrame) -> pl.DataFrame:
    """Give every row a PitcherId. TrackMan sometimes leaves it null; grouping on a null id would lump every
    such pitcher into one fake pitcher. Null ids take the name's id when that name has exactly one known id,
    otherwise "name:<Pitcher>"."""
    if "Pitcher" not in df.columns or df["PitcherId"].null_count() == 0:
        return df
    one_id = (df.filter(pl.col("PitcherId").is_not_null()).group_by("Pitcher")
                .agg(pl.col("PitcherId").unique().alias("_ids"))
                .filter(pl.col("_ids").list.len() == 1)
                .select("Pitcher", pl.col("_ids").list.first().alias("_id")))
    return (df.join(one_id, on="Pitcher", how="left")
              .with_columns(PitcherId=pl.coalesce("PitcherId", "_id", pl.lit("name:") + pl.col("Pitcher")))
              .drop("_id"))



def _base_features(df: pl.DataFrame) -> pl.DataFrame:
    """Mirror HB for lefties (+ = arm side), tag group, and ΔSpeed vs. the outing's p95 velo (shrunk to the
    season p95 for short outings). Drops rows without handedness or any of velo/movement/spin."""
    return (fill_pitcher_ids(df)
        .filter(pl.col("PitcherThrows").is_in(["Right", "Left"]))
        .drop_nulls(["RelSpeed", "InducedVertBreak", "HorzBreak", "SpinRate"])
        .with_columns(
            HB=pl.when(pl.col("PitcherThrows") == "Left").then(-pl.col("HorzBreak")).otherwise(pl.col("HorzBreak")),
            IVB=pl.col("InducedVertBreak"),
            tag_group=pl.col("TaggedPitchType").replace_strict(TAG_GROUP, default=None))
        .with_columns(
            n_out=pl.len().over("PitcherId", "GameID"),
            p95_out=pl.col("RelSpeed").quantile(0.95).over("PitcherId", "GameID"),
            p95_season=pl.col("RelSpeed").quantile(0.95).over(UNIT),
            n_season=pl.len().over(UNIT))
        .with_columns(w=pl.col("n_out") / (pl.col("n_out") + K_SHRINK))
        .with_columns(v_ref=pl.col("w") * pl.col("p95_out") + (1 - pl.col("w")) * pl.col("p95_season"))
        .with_columns(dSpeed=pl.col("RelSpeed") - pl.col("v_ref"))
        .drop("w", "p95_out", "p95_season"))


FEATS = ["dSpeed", "HB", "IVB", "spin_pct"]
D = len(FEATS)

TYPES = ["FS", "SI", "CT", "SL", "CB", "CH", "SP"]
TYPE_NAME = {"FS": "Four-Seam", "SI": "Sinker / 2-Seam", "CT": "Cutter", "SL": "Slider / Sweeper",
             "CB": "Curveball", "CH": "Changeup", "SP": "Splitter"}
TYPE_GROUP = {"FS": "FB", "SI": "FB", "CT": "BB", "SL": "BB", "CB": "BB", "CH": "OS", "SP": "OS"}
GROUP_TYPES = {"FB": ["FS", "SI"], "BB": ["CT", "SL", "CB"], "OS": ["CH", "SP"]}
GROUPS = ["FB", "BB", "OS"]
# Explicit tags only. The generic "Fastball" tag is split into FS / SI by shape (split_generic_fastballs).
TAG_TYPE = {"FourSeamFastBall": "FS", "Sinker": "SI", "TwoSeamFastBall": "SI", "OneSeamFastBall": "SI",
            "Cutter": "CT", "Slider": "SL", "Sweeper": "SL", "Curveball": "CB", "ChangeUp": "CH", "Splitter": "SP"}

# ── Knobs (the open decisions; see plan_b.ipynb) ──────────────────────────────────────────────────
MIN_FIT = 20            # pitcher-seasons below this: pitch-by-pitch template posterior, group only (no _n)
SMALL = 50              # 20..49 pitches: restricted layouts (1 FB, ≤ 2 BB, ≤ 1 OS)
MAX_PER_GROUP = {"FB": 2, "BB": 3, "OS": 2}
MAX_K = 6
KAPPA_COV = 20.0        # a component's ellipse shape counts the template's W as this many pitches
PRIOR_SCALE = 1.0       # multiplies B: > 1 = looser templates, < 1 = stricter
MIN_SHARE = 0.03        # every component needs >= 3% of the pitcher's pitches (and >= 5 pitches)
FB1_MIN_SHARE = 0.10    # Fastball_1 = the fastest component with >= 10% of the pitches (a tiny max-effort
                        # cluster can't define the fastball)
# "Fastball-like" (relative to FB_1): not > 4" glove-side, and either within 2 mph (any spin: nothing that close
# is off-speed) or within 5 mph with spin >= 92% of fastball spin. Fastball-like components MUST be fastballs
# and only they can be Fastball_n.
FB_GAP_ANY_MPH, FB_GAP_MPH, FB_GLOVE_IN, FB_SPIN_MIN = 2.0, 5.0, 4.0, 92.0
OS_GLOVE_IN = 6.0       # an offspeed component can't sit >= 6" glove-side of FB_1
FB_DSPEED_SD_MIN = 1.5  # FB templates are self-referential in ΔSpeed and spin_pct (both are measured against the
FB_SPIN_SD_MIN = 4.0    # pitcher's own fastball, so primary fastballs sit at ~-1.2 mph / ~100% ± 0.5). Floor their
                        # between-pitcher spread so a legit 2nd fastball (slower, less spin) isn't "impossible".
RELAX_ON_FAIL = False   # if no layout passes the rules: True = retry without the group minimums; False = fall back
                        # to each pitch's template posterior (group only). See plan_b.ipynb for the comparison.
REG = 1e-3              # ridge on covariances (feature units are mph / in / %)
LOG2PI = np.log(2 * np.pi)


# ── Features ──────────────────────────────────────────────────────────────────────────────────────
def add_features(raw: pl.DataFrame) -> pl.DataFrame:
    """ΔSpeed, mirrored HB and filled ids (``_base_features``) + ``spin_pct`` = 100 × spin / the pitcher's fastball spin.
    The fastball spin is a tag-free proxy: the median spin of the pitcher-season's pitches within 2 mph of top velo."""
    d = _base_features(raw)
    fb = d.filter(pl.col("dSpeed") > -2).group_by(UNIT).agg(fb_spin=pl.col("SpinRate").median())
    return (d.join(fb, on=UNIT, how="left")
             .with_columns(fb_spin=pl.coalesce("fb_spin", pl.col("SpinRate").median().over(UNIT)))
             .with_columns(spin_pct=100 * pl.col("SpinRate") / pl.col("fb_spin")))


# ── Templates ─────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Templates:
    mu: dict      # type -> (D,) center
    B: dict       # type -> (D, D) between-pitcher covariance
    W: dict       # type -> (D, D) within-pitch covariance
    freq: dict    # type -> share of pitcher-season pitch types within its group (prior weight)
    n: dict       # type -> number of pitcher-season pitch types it was built from

    def arrays(self):
        """Plain dict of numpy arrays (cheap to ship to joblib workers), with B scaled by PRIOR_SCALE."""
        B = {t: self.B[t] * PRIOR_SCALE for t in TYPES}
        for t in GROUP_TYPES["FB"]:                                 # let a 2nd fastball sit a few mph off top velo
            B[t] = B[t].copy()                                      # and spin a bit less than the primary one
            B[t][0, 0] = max(B[t][0, 0], FB_DSPEED_SD_MIN ** 2)
            B[t][3, 3] = max(B[t][3, 3], FB_SPIN_SD_MIN ** 2)
        return {"mu": self.mu, "B": B, "Binv": {t: np.linalg.inv(B[t]) for t in TYPES},
                "LB": {t: np.linalg.cholesky(B[t]) for t in TYPES}, "W": self.W,
                "logw": {t: np.log(self.freq[t]) for t in TYPES}}

    def to_dict(self) -> dict:
        """Plain JSON-able dict (the production model freezes its templates in its artifact)."""
        arr = lambda d: {t: np.asarray(v).tolist() for t, v in d.items()}
        return {"mu": arr(self.mu), "B": arr(self.B), "W": arr(self.W), "freq": dict(self.freq), "n": dict(self.n)}

    @classmethod
    def from_dict(cls, d: dict) -> "Templates":
        arr = lambda x: {t: np.asarray(v, dtype=float) for t, v in x.items()}
        return cls(arr(d["mu"]), arr(d["B"]), arr(d["W"]), {t: float(v) for t, v in d["freq"].items()},
                   {t: int(v) for t, v in d["n"].items()})

    def table(self) -> pl.DataFrame:
        rows = []
        for t in TYPES:
            r = {"type": TYPE_NAME[t], "group": TYPE_GROUP[t], "built from": self.n[t]}
            for i, f in enumerate(FEATS):
                r[f] = float(self.mu[t][i])
            for i, f in enumerate(FEATS):
                r[f"{f} between SD"] = float(np.sqrt(self.B[t][i, i]))
            for i, f in enumerate(FEATS):
                r[f"{f} within SD"] = float(np.sqrt(self.W[t][i, i]))
            rows.append(r)
        return pl.DataFrame(rows)


def _robust(X: np.ndarray, seed: int = 0):
    from sklearn.covariance import MinCovDet
    mcd = MinCovDet(random_state=seed, support_fraction=0.75).fit(X)
    return mcd.location_, mcd.covariance_ + REG * np.eye(X.shape[1])


def templates_from_labels(df: pl.DataFrame, type_col: str, key_col: str | None = None, min_n: int = 20,
                          within_sample: int = 40_000, seed: int = 0) -> Templates:
    """Robust templates from labeled pitches. One "unit" = a pitcher-season's pitches sharing ``key_col``
    (default: the type itself; for fitted components, the component id). Unit centers are medians; ``mu``/``B``
    are the MCD location/covariance of the unit centers; ``W`` is the MCD covariance of pitch deviations
    from their unit's center."""
    key = [*UNIT, key_col or type_col]
    g = df.filter(pl.col(type_col).is_not_null())
    units = (g.group_by(key).agg(pl.len().alias("_n"), pl.col(type_col).first().alias("_type"),
                                 *[pl.col(f).median().alias(f"_c_{f}") for f in FEATS])
              .filter(pl.col("_n") >= min_n))
    dev = (g.join(units.select(*key, *[f"_c_{f}" for f in FEATS]), on=key)
            .select(pl.col(type_col).alias("_type"),
                    *[(pl.col(f) - pl.col(f"_c_{f}")).alias(f) for f in FEATS]))
    mu, B, W, n = {}, {}, {}, {}
    for t in TYPES:
        C = units.filter(pl.col("_type") == t).select([f"_c_{f}" for f in FEATS]).to_numpy()
        mu[t], B[t] = _robust(C, seed)
        dv = dev.filter(pl.col("_type") == t)
        dv = dv.sample(min(dv.height, within_sample), seed=seed)
        _, W[t] = _robust(dv.select(FEATS).to_numpy(), seed)
        n[t] = C.shape[0]
    freq = {}
    for grp, ts in GROUP_TYPES.items():
        tot = sum(n[t] for t in ts)
        for t in ts:
            freq[t] = n[t] / tot
    return Templates(mu, B, W, freq, n)


def _logpdf(X, m, S):
    """Gaussian log-density of each row of X (numpy only: SciPy's per-call overhead dominates at 4×4)."""
    L = np.linalg.cholesky(S)
    z = np.linalg.solve(L, (X - m).T)
    return -0.5 * np.einsum("ij,ij->j", z, z) - np.log(np.diag(L)).sum() - 0.5 * X.shape[1] * LOG2PI


def _logpdf1(x, m, S):
    """Same, for a single point."""
    d = x - m
    _, logdet = np.linalg.slogdet(S)
    return -0.5 * (d @ np.linalg.solve(S, d) + logdet + len(x) * LOG2PI)


def _logsumexp(a, axis=1):
    mx = a.max(axis=axis, keepdims=True)
    return (mx + np.log(np.exp(a - mx).sum(axis=axis, keepdims=True))).squeeze(axis)


def _type_scores(X, T, types):
    """log of each type's prior-predictive density N(x; mu, B + W) + log prior weight. (n, len(types))"""
    return np.column_stack([_logpdf(X, T["mu"][t], T["B"][t] + T["W"][t]) + T["logw"][t] for t in types])


def _split_one(X, T):
    """One pitcher-season's generic-"Fastball" pitches -> FS/SI per pitch. 1 or 2 clusters (ICL), and each
    cluster takes the type whose template best explains its center."""
    from sklearn.mixture import GaussianMixture
    best = None
    for k in (1, 2):
        if k == 2 and len(X) < 40:
            break
        g = GaussianMixture(k, covariance_type="full", n_init=2, random_state=0, reg_covar=REG).fit(X)
        r = g.predict_proba(X)
        icl = g.bic(X) + 2 * -np.sum(r * np.log(r + 1e-12))
        if best is None or icl < best[0]:
            best = (icl, g, r.argmax(1))
    _, g, lab = best
    out = np.empty(len(X), dtype=object)
    for c in range(g.n_components):
        sc = [_logpdf1(g.means_[c], T["mu"][t], T["B"][t] + T["W"][t] / max((lab == c).sum(), 1)) + T["logw"][t]
              for t in ("FS", "SI")]
        out[lab == c] = ("FS", "SI")[int(np.argmax(sc))]
    return out


def split_generic_fastballs(df: pl.DataFrame, T: Templates, min_n: int = 20, n_jobs: int = -1) -> pl.Series:
    """FS / SI for every generic-"Fastball" pitch (None elsewhere), per pitcher-season by shape."""
    from joblib import Parallel, delayed
    Ta = T.arrays()
    gen = df.filter(pl.col("TaggedPitchType") == "Fastball").select("_rid", *UNIT, *FEATS)
    big = gen.filter(pl.len().over(UNIT) >= min_n).partition_by(UNIT)
    labs = Parallel(n_jobs=n_jobs)(delayed(_split_one)(p.select(FEATS).to_numpy(), Ta) for p in big)
    out = np.full(df.height, None, dtype=object)
    for p, lab in zip(big, labs):
        out[p["_rid"].to_numpy()] = lab
    small = gen.filter(pl.len().over(UNIT) < min_n)
    if small.height:
        sc = _type_scores(small.select(FEATS).to_numpy(), Ta, ["FS", "SI"])
        out[small["_rid"].to_numpy()] = np.array(["FS", "SI"], dtype=object)[sc.argmax(1)]
    return pl.Series("ttype_generic", out.tolist(), dtype=pl.String)


def templates_from_tags(df: pl.DataFrame, n_jobs: int = -1):
    """Pass-0 templates from TaggedPitchType (robust), with generic "Fastball" tags split into FS / SI by shape.
    Returns (templates, df with ``_rid`` and ``ttype`` = the tag-derived type per pitch)."""
    if "_rid" not in df.columns:
        df = df.with_row_index("_rid")
    df = df.with_columns(ttype=pl.col("TaggedPitchType").replace_strict(TAG_TYPE, default=None))
    T0 = templates_from_labels(df, "ttype")                     # explicit tags only
    df = df.with_columns(split_generic_fastballs(df, T0, n_jobs=n_jobs)).with_columns(
        ttype=pl.coalesce("ttype", "ttype_generic"))
    return templates_from_labels(df, "ttype"), df


def templates_from_fits(out: pl.DataFrame) -> Templates:
    """Empirical-Bayes refresh: templates from the fitted components (unit = pitcher-season × component)."""
    return templates_from_labels(out.filter(pl.col("b_comp") >= 0), "b_type", key_col="b_comp")


# ── Per-pitcher MAP-EM ────────────────────────────────────────────────────────────────────────────
def _m_step(X, rk, nk, S_k, group, T, t=None):
    """Pick the component's template (best fit within its group) unless ``t`` is given, then the MAP center
    and the blended ellipse."""
    xbar = rk @ X / nk
    if t is None:
        types = GROUP_TYPES[group]
        sc = [_logpdf1(xbar, T["mu"][u], T["B"][u] + S_k / nk) + T["logw"][u] for u in types]
        t = types[int(np.argmax(sc))]
    Sinv, Binv = np.linalg.inv(S_k), T["Binv"][t]
    m = np.linalg.solve(nk * Sinv + Binv, nk * Sinv @ xbar + Binv @ T["mu"][t])   # Gaussian prior N(mu, B)
    diff = X - m
    S = (KAPPA_COV * T["W"][t] + (rk[:, None] * diff).T @ diff) / (KAPPA_COV + nk) + REG * np.eye(D)
    return t, m, S


def _fit_layout(X, groups, T, init, max_iter=60, tol=1e-5, rechoose_every=3):
    n, K = len(X), len(groups)
    m = np.array(init, dtype=float)
    types = [GROUP_TYPES[g][0] for g in groups]
    S = [T["W"][t].copy() for t in types]
    logpi = np.full(K, -np.log(K))
    prev = -np.inf
    for it in range(max_iter):
        logp = np.column_stack([_logpdf(X, m[k], S[k]) for k in range(K)]) + logpi
        lse = _logsumexp(logp)
        r = np.exp(logp - lse[:, None])
        ll = lse.sum()
        if abs(ll - prev) < tol * abs(ll):
            break
        prev = ll
        nk = r.sum(0)
        if (nk < 2).any():
            return None
        logpi = np.log(nk / n)
        rechoose = it % rechoose_every == 0                         # template choice is stable; skip most iterations
        for k in range(K):
            types[k], m[k], S[k] = _m_step(X, r[:, k], nk[k], S[k], groups[k], T, None if rechoose else types[k])
    nk = r.sum(0)
    maha = 0.0
    for k in range(K):
        z = np.linalg.solve(T["LB"][types[k]], m[k] - T["mu"][types[k]])
        maha += z @ z
    entropy = -np.sum(r * np.log(r + 1e-12))
    p = K * (D + D * (D + 1) // 2) + K - 1
    icl = -2 * (ll - 0.5 * maha) + p * np.log(n) + 2 * entropy
    return {"icl": icl, "r": r, "m": m, "S": S, "types": types, "groups": list(groups), "nk": nk}


def _fb1(m, share):
    """Fastball_1: the fastest component holding >= FB1_MIN_SHARE of the pitches (fastest overall if none does)."""
    big = np.flatnonzero(share >= FB1_MIN_SHARE)
    pool = big if len(big) else np.arange(len(share))
    return int(pool[np.argmax(m[pool, 0])])


def _valid(fit, n):
    """Plan D's definitions as guardrails."""
    m, g, nk = fit["m"], fit["groups"], fit["nk"]
    if (nk < max(5, MIN_SHARE * n)).any():
        return False
    fb1 = _fb1(m, nk / n)
    if g[fb1] != "FB":
        return False                                                # the (substantial) fastest pitch is a fastball
    for k in range(len(g)):
        if k == fb1:
            continue
        faster = m[k, 0] >= m[fb1, 0]
        gap = m[fb1, 0] - m[k, 0]
        fb_like = (m[k, 1] - m[fb1, 1] >= -FB_GLOVE_IN
                   and (gap <= FB_GAP_ANY_MPH or (gap <= FB_GAP_MPH and m[k, 3] >= FB_SPIN_MIN)))
        if (faster or fb_like) != (g[k] == "FB"):
            return False                                            # fastball-like <=> fastball (cut ones are BB)
        if g[k] == "OS" and m[k, 1] - m[fb1, 1] <= -OS_GLOVE_IN:
            return False                                            # offspeed can't sit well glove-side
    return True


def _layouts(pre_share, n, small, force=True):
    """Candidate layouts. The template pre-assignment caps each group (none if < 1% of pitches) and, with
    ``force``, requires at least one component for a group holding >= 10% (protects small changeups from being
    merged away). fit_pitcher retries with force=False when nothing valid comes out — the pre-assignment can be
    wrong for unusual pitchers (e.g. a low-spin curveball it reads as offspeed)."""
    mx = {"FB": 1, "BB": 2, "OS": 1} if small else MAX_PER_GROUP
    ranges = {"FB": range(1, mx["FB"] + 1)}
    for grp in ("BB", "OS"):
        s = pre_share[grp]
        lo = 1 if (force and s >= 0.10) else 0
        hi = 0 if s < 0.01 else mx[grp]
        ranges[grp] = range(lo, hi + 1)
    out = []
    for a, b, c in itertools.product(ranges["FB"], ranges["BB"], ranges["OS"]):
        if a + b + c <= min(MAX_K, n // 10):
            out.append(("FB",) * a + ("BB",) * b + ("OS",) * c)
    return out


def _init_means(X, pre, groups, T, cache):
    """Starting centers: k-means of the pitches the templates pre-assign to each group (cached per group/k)."""
    from sklearn.cluster import KMeans
    init, sd = [], X.std(0) + 1e-9
    for grp in GROUPS:
        k = groups.count(grp)
        if not k:
            continue
        if (grp, k) not in cache:
            pts = X[pre == grp]
            if len(pts) >= 3 * k:
                c = pts.mean(0, keepdims=True) if k == 1 else \
                    KMeans(k, n_init=2, random_state=0).fit(pts / sd).cluster_centers_ * sd
                if grp == "FB":
                    c = c[np.argsort(-c[:, 0])]                     # fastest first
            else:
                c = np.array([T["mu"][t] for t in (GROUP_TYPES[grp] * k)[:k]])
            cache[(grp, k)] = c
        init.extend(cache[(grp, k)])
    return np.array(init)


def fit_pitcher(X: np.ndarray, T: dict, small: bool = False) -> dict:
    """Best valid layout for one pitcher-season. Returns per-pitch component, max posterior and group
    probabilities, plus one row per component."""
    n = len(X)
    sc = _type_scores(X, T, TYPES)
    pre = np.array([TYPE_GROUP[t] for t in TYPES])[sc.argmax(1)]
    share = {grp: float(np.mean(pre == grp)) for grp in GROUPS}
    best, cache, tried = None, {}, 0
    for force in ((True, False) if RELAX_ON_FAIL else (True,)):
        for groups in _layouts(share, n, small, force):
            if force is False and groups in _layouts(share, n, small, True):
                continue                                            # already tried
            tried += 1
            f = _fit_layout(X, groups, T, _init_means(X, pre, groups, T, cache))
            if f is not None and _valid(f, n) and (best is None or f["icl"] < best["icl"]):
                best = f
        if best is not None:
            break
    if best is None:                                                # nothing valid: one fastball + template posterior
        return {"comp": np.full(n, -1, np.int16), "conf": np.full(n, np.nan, np.float32),
                "pgroup": _group_posterior(X, T), "comps": [], "n_layouts": tried}
    r, g = best["r"], np.array(best["groups"])
    pgroup = np.column_stack([r[:, g == grp].sum(1) for grp in GROUPS]).astype(np.float32)
    comps = [{"comp": k, "group": best["groups"][k], "type": best["types"][k], "n": float(best["nk"][k]),
              **{f"m_{f}": float(best["m"][k, i]) for i, f in enumerate(FEATS)}} for k in range(len(g))]
    return {"comp": r.argmax(1).astype(np.int16), "conf": r.max(1).astype(np.float32), "pgroup": pgroup,
            "comps": comps, "n_layouts": tried}


def _group_posterior(X, T):
    sc = _type_scores(X, T, TYPES)
    p = np.exp(sc - _logsumexp(sc)[:, None])
    return np.column_stack([p[:, [TYPES.index(t) for t in GROUP_TYPES[grp]]].sum(1) for grp in GROUPS]).astype(np.float32)


# ── Run over a dataset ────────────────────────────────────────────────────────────────────────────
def run(df: pl.DataFrame, T: Templates, n_jobs: int = -1, min_fit: int = MIN_FIT, small: int = SMALL):
    """Fit every pitcher-season. Adds b_comp (-1 = not fitted), b_conf, pFB/pBB/pOS, b_group, b_type, b_retag,
    b_source. Returns (pitches, components)."""
    from joblib import Parallel, delayed
    if "_rid" not in df.columns:
        df = df.with_row_index("_rid")
    Ta = T.arrays()
    parts = df.filter(pl.col("n_season") >= min_fit).select("_rid", *UNIT, *FEATS).partition_by(UNIT)
    fits = Parallel(n_jobs=n_jobs, batch_size=16)(
        delayed(fit_pitcher)(p.select(FEATS).to_numpy(), Ta, p.height < small) for p in parts)

    comp = np.full(df.height, -1, dtype=np.int16)
    conf = np.full(df.height, np.nan, dtype=np.float32)
    pg = np.full((df.height, 3), np.nan, dtype=np.float32)
    rows = []
    for p, f in zip(parts, fits):
        rid = p["_rid"].to_numpy()
        comp[rid], conf[rid], pg[rid] = f["comp"], f["conf"], f["pgroup"]
        key = p.row(0, named=True)
        for c in f["comps"]:
            rows.append({"PitcherId": key["PitcherId"], "Year": key["Year"], **c, "n_layouts": f["n_layouts"]})
    unfit = np.isnan(pg[:, 0])
    if unfit.any():                                                 # small pitcher-seasons: template posterior
        pg[unfit] = _group_posterior(df.filter(pl.Series(unfit)).select(FEATS).to_numpy(), Ta)

    comps = pl.DataFrame(rows, schema_overrides={"Year": pl.Int32, "comp": pl.Int16}) if rows else pl.DataFrame()
    comps = _name_components(comps)
    out = (df.with_columns(b_comp=pl.Series(comp), b_conf=pl.Series(conf),
                           pFB=pl.Series(pg[:, 0]), pBB=pl.Series(pg[:, 1]), pOS=pl.Series(pg[:, 2]))
             .join(comps.select(*UNIT, pl.col("comp").alias("b_comp"), pl.col("group").alias("_cg"),
                                pl.col("type").alias("b_type"), pl.col("retag").alias("b_retag")),
                   on=[*UNIT, "b_comp"], how="left")
             .with_columns(_pg=pl.concat_list("pFB", "pBB", "pOS").list.arg_max())
             .with_columns(
                 b_source=pl.when(pl.col("b_comp") >= 0).then(pl.lit("fit")).otherwise(pl.lit("template posterior")),
                 b_group=pl.coalesce("_cg", pl.lit(GROUPS).list.get(pl.col("_pg"))))
             .with_columns(b_retag=pl.coalesce("b_retag", pl.col("b_group").replace_strict(GROUP_NAME, default=None)))
             .drop("_cg", "_pg"))
    return out, comps


def _name_components(comps: pl.DataFrame) -> pl.DataFrame:
    """Fastball_1 = the FB component _valid treated as FB_1 (fastest with >= 10% of pitches); everything else
    numbered by usage within its group."""
    if comps.is_empty():
        return comps.with_columns(retag=pl.lit(None, dtype=pl.String))
    big = (pl.col("n") / pl.col("n").sum().over(UNIT)) >= FB1_MIN_SHARE
    fb1_speed = pl.coalesce(pl.col("m_dSpeed").filter((pl.col("group") == "FB") & big).max().over(UNIT),
                            pl.col("m_dSpeed").filter(pl.col("group") == "FB").max().over(UNIT))
    return (comps
        .with_columns(_fast=(pl.col("m_dSpeed") == fb1_speed) & (pl.col("group") == "FB"))
        .with_columns(_key=pl.when(pl.col("_fast")).then(1e9).otherwise(pl.col("n")))
        .with_columns(_n=pl.col("_key").rank("ordinal", descending=True).over(*UNIT, "group"))
        .with_columns(retag=pl.col("group").replace_strict(GROUP_NAME) + "_" + pl.col("_n").cast(pl.String))
        .drop("_fast", "_key", "_n"))


# ── Entry points ──────────────────────────────────────────────────────────────────────────────────
_OUT = ["PitchUID", "b_group", "b_retag", "b_type", "pFB", "pBB", "pOS", "b_source", "tag_group"]


def _ordered(raw: pl.DataFrame) -> pl.DataFrame:
    """A fixed row order. The per-pitcher fits start from k-means, which depends on row order, so the
    same pitches must arrive in the same order for training and scoring to give the same labels."""
    return raw.select(_RAW_COLS).sort("PitcherId", "Year", "PitchUID", nulls_last=True)


def build_templates(raw: pl.DataFrame, n_jobs: int = -1) -> tuple[Templates, pl.DataFrame]:
    """Training: templates from the tags (pass 0), fit every pitcher-season (pass 1), re-estimate the
    templates from the fits, and fit again (pass 2). Returns (the pass-2 templates, the pass-2 labels).
    About 10 minutes for all of D1 2022-26 on 8 cores."""
    d = add_features(_ordered(raw))
    T0, d = templates_from_tags(d, n_jobs=n_jobs)
    out1, _ = run(d, T0, n_jobs=n_jobs)
    T1 = templates_from_fits(out1)
    out, _ = run(d, T1, n_jobs=n_jobs)
    return T1, out.select(_OUT)


def label(raw: pl.DataFrame, T: Templates, n_jobs: int = -1) -> pl.DataFrame:
    """Scoring: tag pitches with frozen templates (one pass). PitchUID -> group, retag (``Fastball_1`` ...),
    type, group probabilities, source and the operator-tag group. Pitches without handedness or shape
    are dropped (they can't be tagged)."""
    if raw.height == 0:
        return pl.DataFrame(schema={c: pl.String for c in _OUT})
    out, _ = run(add_features(_ordered(raw)), T, n_jobs=n_jobs)
    return out.select(_OUT)
