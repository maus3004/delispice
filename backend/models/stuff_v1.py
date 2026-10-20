"""Stuff+ v1, the production model (Model 2: probability pieces). What a pitch's SHAPE is worth.

Ported from ``backend/research/stuff_model/stuff_v1_model2.ipynb`` and the shared steps of
``stuff_v1.ipynb``. The decisions behind every choice are in ``stuff_decisions.md`` in that folder.
This module is the MODEL (loading, tagging, targets, features, training, grading).
``stuff_store.py`` handles the artifacts and the CLI, the same split as ``eye_v1`` / ``eye_store``.

**What a grade means.** For each group (FB / BB / OS), three LightGBM pieces describe a swing at a
pitch, learned from D1 swings 2022-26 (swings only, T1):

  1. P(whiff)                    on every swing
  2. P(foul | contact)           on swings that weren't whiffs
  3. contact value               plain contact xRV of balls in play (realized RV + shift if untracked)

A pitch is graded against an AVERAGE hitter (``hitter_oe`` = its training average), across the
12 counts x 2 batter sides, weighted the way pitches of its group are used (its group's count mix and
group x pitcher-hand platoon mix, package A). At each context the pieces are priced with one RE288
price list averaged over the training seasons (P5):

    value = P(whiff)·whiff price + (1 − P(whiff))·[P(foul|c)·foul price + (1 − P(foul|c))·(contact − shift)]

That's expected runs per swing (batter's side, negative = good for the pitcher). Stuff+ = 100 + 10 x
SDs better than the group average, on a scale FROZEN with the model (PR3: retrain once a season).
Whiff+, Foul+ and Contact+ are the pieces on the same kind of scale. The run breakdown swaps this pitch's
pieces in one at a time along the chain, against the group's average pieces.

**Inputs a grade needs:** shape only (velo, IVB/HB or acceleration, spin, release height/side) plus the
pitcher's FB_1 that outing for the differentials. The outing's real counts, batters and results don't
enter a grade. Feature runs per group (PR1): movement for FB and OS, acceleration for BB.

**Training** (``train``) also needs the hitter feature, built the C3 way: a stage-1 model with no
hitter feature, cross-fitted by pitcher; each hitter's swing value over expected (leave-current-game-out,
shrunk by n/(n+k)); one-game hitters left missing. Grading never uses a real hitter.
"""
from __future__ import annotations

import json
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

from pipeline_v2 import config

PIPELINE = config.PITCHES_DIR
LEVEL = "D1"
MODEL_VERSION = "v1"
GROUPS = ["FB", "BB", "OS"]
PIECES = ["whiff", "foul", "contact"]
SWING_CALLS = ["StrikeSwinging", "InPlay", "FoulBall", "FoulBallFieldable", "FoulBallNotFieldable"]
FOULS = ["FoulBall", "FoulBallFieldable", "FoulBallNotFieldable"]

# Features (F1-F7). Raw shape per run; the differentials vs. FB_1 are movement in both runs.
RAW = {"movement": ["RelSpeed", "IVB", "HB", "SpinRate", "RelHeight", "RelSideM"],
       "acceleration": ["RelSpeed", "acc_v", "acc_h", "SpinRate", "RelHeight", "RelSideM"]}
DIFFS = ["dVelo", "dHB", "dIVB"]
CONTEXT = ["Balls", "Strikes", "same", "hitter_oe"]
RUN_BY_GROUP = {"FB": "movement", "BB": "acceleration", "OS": "movement"}      # PR1
SHAPE_ALL = sorted(set(RAW["movement"]) | set(RAW["acceleration"]))
STAGE1_FEATS = RAW["movement"] + DIFFS + ["Balls", "Strikes", "same"]         # no hitter feature (C3)


def feats_for(group: str) -> list[str]:
    return RAW[RUN_BY_GROUP[group]] + DIFFS + CONTEXT


# The grading contexts: 12 counts x (same-hand, opposite-hand) batter.
CTX = [(b, s, side) for b in range(4) for s in range(3) for side in (1, 0)]

LGB_PARAMS = dict(learning_rate=0.05, num_leaves=31, max_depth=6, min_child_samples=500,
                  n_jobs=-1, verbose=-1, seed=42)
MAX_TREES, PATIENCE, N_FOLDS, SEED = 3000, 50, 5, 42
K_REF = 10                                   # FB_1 reference: (n·outing + 10·season) / (n + 10)  (F3)
GAP_MAX, REL_H_MAX, REL_S_MAX = 2, 0.75, 1.0  # pitcher-ID merge, rule C (M2)
# PR2 (decided 2026-10-16: per pitch): which population sets the Stuff+ scale. "per_pitch" = every graded pitch (10 points = 1 SD
# of single pitches); "per_pitch_type" = pitch type-seasons with >= MIN_PT pitches (10 points = 1 SD of
# pitch types). Only the frozen mean/SD change; nothing else in the model does.
SCALE_METHOD = "per_pitch"
MIN_PT = 100
CHUNK = 120_000
# Raw columns a pitch needs to be graded (the tagger's shape + the sources of SHAPE_ALL), plus a pitcher hand.
RAW_NEEDED = ["RelSpeed", "SpinRate", "InducedVertBreak", "HorzBreak", "RelHeight", "RelSide", "ax0", "az0"]


def can_grade() -> pl.Expr:
    """True for raw rows the model can grade: a pitcher (id or name; merge_pitcher_ids drops rows with
    neither), a hand and every shape number. Rows failing it never get a grade (they're not in the
    score tables), so a lookup shouldn't treat them as misses to grade live."""
    return ((pl.col("PitcherId").is_not_null() | pl.col("Pitcher").is_not_null())
            & pl.col("PitcherThrows").is_in(["Right", "Left"]) & pl.all_horizontal(pl.col(RAW_NEEDED).is_not_null()))

_COLS = ["PitchUID", "GameID", "Date", "PitcherId", "Pitcher", "PitcherThrows", "PitcherTeam",
         "BatterId", "Batter", "BatterSide", "League", "Level", "Balls", "Strikes", "PitchCall",
         "TaggedPitchType", "TaggedHitType", "RelSpeed", "SpinRate", "InducedVertBreak", "HorzBreak",
         "RelHeight", "RelSide", "ax0", "az0"]


# ── Loading ──────────────────────────────────────────────────────────────────────────────────────
def available_years() -> list[str]:
    return sorted(p.name for p in (PIPELINE / LEVEL).glob("*") if p.is_dir() and p.name.isdigit())


def load_pitches(years: list[str] | None = None, with_outcomes: bool = True) -> pl.DataFrame:
    """Every D1 pitch of ``years``. Scans both physical partitions and filters on Level, like eye: a
    level's rows are not confined to its own partition. ``with_outcomes`` adds the run-value state
    and ``is_verified`` (training needs them; grading doesn't)."""
    from backend.models import pitch_groups as pg, rv_store
    years = years or available_years()
    files = sorted(f for y in years for f in PIPELINE.glob(f"*/{y}/**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for years={years} under {PIPELINE}")
    lf = pl.scan_parquet([str(f) for f in files])
    names = set(lf.collect_schema().names())
    extra = ([*rv_store.ROW_STATE, "is_verified"] if with_outcomes else [])
    cols = [c for c in _COLS + extra if c in names]
    raw = (lf.select(cols).filter(pl.col("Level") == LEVEL).drop("Level")
             .with_columns(Year=pl.col("Date").str.slice(0, 4).cast(pl.Int32)).drop("Date")
             .unique(subset=["PitchUID"], keep="first").collect())
    if with_outcomes and "is_verified" not in raw.columns:
        raw = raw.with_columns(is_verified=pl.lit(True))
    return pg.fill_pitcher_ids(raw)


# ── One ID per pitcher (M2) ──────────────────────────────────────────────────────────────────────
def _norm_name(s):
    if not s:
        return None
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.lower().replace(".", " ").replace("'", "").replace("-", " ").split())


def _close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def merge_pitcher_ids(raw: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Add ``pid``: one id per real pitcher. Ids sharing a normalized name and throwing hand merge when
    they share a team in the same season (A), share a team in another season (B), or are <= 2 seasons
    apart with a matching release point (C). Same name + same season + different teams never merge.
    Rows with neither an id nor a name are dropped."""
    seasons = (raw.group_by("PitcherId", "Year")
                  .agg(name=pl.col("Pitcher").drop_nulls().mode().sort().first(),
                       throws=pl.col("PitcherThrows").drop_nulls().mode().sort().first(),
                       team=pl.col("PitcherTeam").drop_nulls().mode().sort().first(), n=pl.len(),
                       rel_h=pl.col("RelHeight").median(),
                       rel_s=pl.when(pl.col("PitcherThrows") == "Left").then(-pl.col("RelSide"))
                               .otherwise(pl.col("RelSide")).median()))
    ids = (seasons.sort(["n", "PitcherId", "Year"], descending=[True, False, False])
                  .group_by("PitcherId", maintain_order=True)
                  .agg(pl.col("name").first(), pl.col("throws").first(), pl.col("n").sum(),
                       pl.struct("Year", "team", "rel_h", "rel_s").alias("prof"))
                  .with_columns(key=pl.col("name").map_elements(_norm_name, return_dtype=pl.String)
                                    + "|" + pl.col("throws").fill_null("?")))
    by_key = defaultdict(list)
    for r in ids.iter_rows(named=True):
        by_key[r["key"]].append(r)
    pid_of, rules = {}, defaultdict(int)
    for key, members in by_key.items():
        if key is None or len(members) == 1:
            for m in members:
                pid_of[m["PitcherId"]] = m["PitcherId"]
            continue
        clusters = []
        for m in sorted(members, key=lambda m: (-m["n"], m["PitcherId"] or "")):
            prof = {p["Year"]: p for p in m["prof"]}
            best = None
            for c in clusters:
                overlap = prof.keys() & c["prof"].keys()
                if any(prof[y]["team"] != c["prof"][y]["team"] for y in overlap):
                    continue
                if overlap:
                    rule = "A"
                elif {p["team"] for p in prof.values()} & {p["team"] for p in c["prof"].values()}:
                    rule = "B"
                else:
                    y1, y2 = min(((a, b) for a in prof for b in c["prof"]), key=lambda t: abs(t[0] - t[1]))
                    if abs(y1 - y2) > GAP_MAX or not (_close(prof[y1]["rel_h"], c["prof"][y2]["rel_h"], REL_H_MAX)
                                                     and _close(prof[y1]["rel_s"], c["prof"][y2]["rel_s"], REL_S_MAX)):
                        continue
                    rule = "C"
                if best is None or rule < best[0]:
                    best = (rule, c)
            if best is None:
                clusters.append({"pid": m["PitcherId"], "prof": dict(prof)})
                pid_of[m["PitcherId"]] = m["PitcherId"]
            else:
                best[1]["prof"].update(prof)
                pid_of[m["PitcherId"]] = best[1]["pid"]
                rules[best[0]] += 1
    out = (raw.join(pl.DataFrame({"PitcherId": list(pid_of), "pid": list(pid_of.values())},
                                 schema={"PitcherId": pl.String, "pid": pl.String}), on="PitcherId", how="left")
              .filter(pl.col("pid").is_not_null()))
    return out, {"ids": ids.height, "pitchers": len(set(pid_of.values())), **{f"rule {k}": v for k, v in sorted(rules.items())}}


# ── Pitch groups ─────────────────────────────────────────────────────────────────────────────────
def add_labels(raw: pl.DataFrame, templates=None, n_jobs: int = -1) -> tuple[pl.DataFrame, object]:
    """Join plan_b's groups. ``templates=None`` builds them (training); otherwise uses the frozen ones.
    ``n_jobs`` = joblib workers for the per-pitcher fits (1 for a handful of pitcher-seasons: starting
    the process pool costs more than the fits)."""
    from backend.models import pitch_groups as pg
    if templates is None:
        templates, lab = pg.build_templates(raw, n_jobs=n_jobs)
    else:
        lab = pg.label(raw, templates, n_jobs=n_jobs)
    lab = lab.select("PitchUID", pl.col("b_group").alias("group"), pl.col("b_retag").alias("retag"),
                     pl.col("b_type").alias("type"), "pFB", "pBB", "pOS", "tag_group")
    return raw.join(lab, on="PitchUID", how="left"), templates


# ── Features (F1-F7) ─────────────────────────────────────────────────────────────────────────────
def add_features(df: pl.DataFrame) -> pl.DataFrame:
    """Mirrored shape (+ = arm side for both hands), spin-induced acceleration (ft/s²), the FB_1
    reference per outing shrunk toward the season, the differentials, and the same-hand flag. Adds
    ``gradeable``: has a group and every raw-shape number."""
    lhp = pl.col("PitcherThrows") == "Left"
    df = df.with_columns(
        HB=pl.when(lhp).then(-pl.col("HorzBreak")).otherwise(pl.col("HorzBreak")),
        IVB=pl.col("InducedVertBreak"),
        RelSideM=pl.when(lhp).then(-pl.col("RelSide")).otherwise(pl.col("RelSide")),
        acc_h=pl.when(lhp).then(pl.col("ax0")).otherwise(-pl.col("ax0")),
        acc_v=pl.col("az0") + 32.174,
        same=pl.when(pl.col("BatterSide").is_in(["Right", "Left"]))
               .then((pl.col("PitcherThrows") == pl.col("BatterSide")).cast(pl.Int8)))
    ref = ["RelSpeed", "HB", "IVB"]
    fb1 = df.filter(pl.col("retag").is_in(["Fastball_1", "Fastball"]))
    out_ref = fb1.group_by("pid", "Year", "GameID").agg(n_ref=pl.len(), *[pl.col(c).mean().alias(f"o_{c}") for c in ref])
    sea_ref = fb1.group_by("pid", "Year").agg(*[pl.col(c).mean().alias(f"s_{c}") for c in ref])
    df = (df.join(out_ref, on=["pid", "Year", "GameID"], how="left").join(sea_ref, on=["pid", "Year"], how="left")
            .with_columns(w_ref=pl.col("n_ref").fill_null(0) / (pl.col("n_ref").fill_null(0) + K_REF))
            .with_columns([(pl.col("w_ref") * pl.col(f"o_{c}").fill_null(0) + (1 - pl.col("w_ref")) * pl.col(f"s_{c}"))
                           .alias(f"ref_{c}") for c in ref])
            .with_columns(dVelo=pl.col("RelSpeed") - pl.col("ref_RelSpeed"), dHB=pl.col("HB") - pl.col("ref_HB"),
                          dIVB=pl.col("IVB") - pl.col("ref_IVB"))
            .drop("n_ref", "w_ref", *[f"{p}_{c}" for p in ("o", "s", "ref") for c in ref]))
    return df.with_columns(gradeable=pl.col("group").is_in(GROUPS) & pl.all_horizontal(pl.col(SHAPE_ALL).is_not_null()))


# ── Prices and targets (T1-T7, P2, P5) ───────────────────────────────────────────────────────────
def season_prices(years: list[str]) -> pl.DataFrame:
    """Per season and count: the whiff (= called-strike) value and the contact count shift
    (RE before a ball in play in this count − RE before an average ball in play)."""
    from backend.models import contact_quality as cq, eye_v1
    out = []
    for y in years:
        rem = eye_v1.load_re_matrix(LEVEL, str(y))
        crv = eye_v1.count_run_values(rem).with_columns(Year=pl.lit(int(y), pl.Int32))
        bip, _ = cq.load_training_frame(LEVEL, str(y))
        bip = bip.join(rem.select("re288_state", "Balls", "Strikes"), on="re288_state", how="left")
        sh = (bip.group_by("Balls", "Strikes").agg(re_count=pl.col("re_before").mean())
                 .with_columns(shift=pl.col("re_count") - bip["re_before"].mean(), Year=pl.lit(int(y), pl.Int32)))
        out.append(crv.join(sh.drop("re_count"), on=["Year", "Balls", "Strikes"]))
    return pl.concat(out)


def averaged_prices(season: pl.DataFrame, swings: pl.DataFrame) -> pl.DataFrame:
    """One price list (P5): each season's prices weighted by its swings in that count."""
    n = swings.group_by("Year", "Balls", "Strikes").agg(n=pl.len())
    return (season.join(n, on=["Year", "Balls", "Strikes"])
                  .group_by("Balls", "Strikes")
                  .agg(whiff_price=(pl.col("rv_strike") * pl.col("n")).sum() / pl.col("n").sum(),
                       shift=(pl.col("shift") * pl.col("n")).sum() / pl.col("n").sum())
                  .with_columns(foul_price=pl.when(pl.col("Strikes") < 2).then(pl.col("whiff_price")).otherwise(0.0))
                  .sort("Balls", "Strikes"))


def add_targets(raw: pl.DataFrame, season: pl.DataFrame) -> pl.DataFrame:
    """Every swing's value (Model 1's target ``y``) and the three pieces' targets. Bunts and untracked
    balls with no realized RV get no target. Each season is priced with its own RE288 (T7)."""
    from backend.models import cq_store, rv_store
    years = sorted(raw["Year"].unique().to_list())
    xrv = pl.concat([t for y in years if (t := cq_store.xrv_lookup(LEVEL, str(y))) is not None]).unique("PitchUID")
    raw = pl.concat([d.with_columns(rv=rv_store.attach_rv(d, LEVEL, str(y)))
                     for (y,), d in sorted(raw.partition_by("Year", as_dict=True).items())])
    kind = (pl.when(pl.col("PitchCall") == "StrikeSwinging").then(pl.lit("whiff"))
              .when(pl.col("PitchCall").is_in(FOULS)).then(pl.lit("foul"))
              .when(pl.col("TaggedHitType") == "Bunt").then(pl.lit("bunt"))
              .when(pl.col("xrv").is_not_null()).then(pl.lit("contact, tracked"))
              .otherwise(pl.lit("contact, untracked")))
    sw = (raw.filter(pl.col("PitchCall").is_in(SWING_CALLS), pl.col("BatterSide").is_in(["Right", "Left"]),
                     pl.col("Balls").is_between(0, 3), pl.col("Strikes").is_between(0, 2))
             .select("PitchUID", "Year", "Balls", "Strikes", "PitchCall", "TaggedHitType", "rv")
             .join(season, on=["Year", "Balls", "Strikes"], how="left")
             .join(xrv, on="PitchUID", how="left")
             .with_columns(kind=kind)
             .with_columns(y=pl.when(pl.col("kind") == "whiff").then(pl.col("rv_strike"))
                              .when(pl.col("kind") == "foul").then(pl.when(pl.col("Strikes") < 2).then(pl.col("rv_strike")).otherwise(0.0))
                              .when(pl.col("kind") == "contact, tracked").then(pl.col("xrv") - pl.col("shift"))
                              .when(pl.col("kind") == "contact, untracked").then(pl.col("rv")))
             .filter(pl.col("kind") != "bunt", pl.col("y").is_not_null())
             .with_columns(is_whiff=(pl.col("kind") == "whiff").cast(pl.Int8),
                           is_foul=pl.when(pl.col("kind") != "whiff").then((pl.col("kind") == "foul").cast(pl.Int8)),
                           contact_value=pl.when(pl.col("kind").str.starts_with("contact")).then(pl.col("y") + pl.col("shift")))
             .select("PitchUID", "kind", "y", "is_whiff", "is_foul", "contact_value"))
    return raw.join(sw, on="PitchUID", how="left")


# ── Fitting ──────────────────────────────────────────────────────────────────────────────────────
def _X(d: pl.DataFrame, feats: list[str]) -> np.ndarray:
    return d.select(feats).to_numpy().astype(np.float32)


def _fit(d: pl.DataFrame, feats: list[str], label: str, objective: str, refit_all: bool = False):
    """LightGBM with early stopping on the held-aside pitchers (``es``). With ``refit_all``, refit on
    every row with the number of trees early stopping chose (the production fits)."""
    import lightgbm as lgb
    params = {**LGB_PARAMS, "objective": objective}
    tr, es = d.filter(~pl.col("es")), d.filter(pl.col("es"))
    dtr = lgb.Dataset(_X(tr, feats), tr[label].to_numpy(), feature_name=feats, free_raw_data=True)
    m = lgb.train(params, dtr, num_boost_round=MAX_TREES,
                  valid_sets=[lgb.Dataset(_X(es, feats), es[label].to_numpy(), reference=dtr)],
                  callbacks=[lgb.early_stopping(PATIENCE, verbose=False)])
    if not refit_all:
        return m
    return lgb.train(params, lgb.Dataset(_X(d, feats), d[label].to_numpy(), feature_name=feats),
                     num_boost_round=max(1, m.best_iteration))


def _assign_folds(train: pl.DataFrame) -> pl.DataFrame:
    pids = train.select("pid").unique().sort("pid")
    rng = np.random.default_rng(SEED)
    pids = pids.with_columns(fold=pl.Series(rng.permutation(pids.height) % N_FOLDS).cast(pl.Int8),
                             es=pl.Series(rng.random(pids.height) < 0.10))
    return train.join(pids, on="pid", how="left")


def hitter_feature(train: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """C3: stage-1 held-out predictions (no hitter feature) → each hitter's swing value over expected,
    leaving out the current game, shrunk by n/(n+k). One-game hitters stay missing (never 0)."""
    preds = []
    for g in GROUPS:
        dg = train.filter(pl.col("group") == g)
        for f in range(N_FOLDS):
            m = _fit(dg.filter(pl.col("fold") != f), STAGE1_FEATS, "y", "regression")
            ho = dg.filter(pl.col("fold") == f)
            preds.append(ho.select("PitchUID").with_columns(
                pl.Series("s1", m.predict(_X(ho, STAGE1_FEATS), num_iteration=m.best_iteration))))
    r = train.join(pl.concat(preds), on="PitchUID").with_columns(res=pl.col("y") - pl.col("s1"))
    bs = (r.group_by("BatterId", "Year").agg(n=pl.len(), s=pl.col("res").sum(), ss=(pl.col("res") ** 2).sum())
           .with_columns(m=pl.col("s") / pl.col("n")))
    b = bs.filter(pl.col("n") >= 30)
    noise = (b["ss"] - b["n"] * b["m"] ** 2).sum() / (b["n"] - 1).sum()
    true = b["m"].var() - noise * (1 / b["n"]).mean()
    k = noise / true
    bg = r.group_by("BatterId", "Year", "GameID").agg(n_g=pl.len(), s_g=pl.col("res").sum())
    hit = (r.select("PitchUID", "BatterId", "Year", "GameID")
            .join(bs.select("BatterId", "Year", "n", "s"), on=["BatterId", "Year"])
            .join(bg, on=["BatterId", "Year", "GameID"])
            .with_columns(n_lgo=pl.col("n") - pl.col("n_g"))
            .with_columns(hitter_oe=pl.when(pl.col("n_lgo") > 0)
                                      .then((pl.col("s") - pl.col("s_g")) / (pl.col("n_lgo") + k)).otherwise(None)))
    train = train.join(hit.select("PitchUID", "hitter_oe"), on="PitchUID", how="left")
    return train, {"k": float(k), "hit_avg": float(train["hitter_oe"].mean()),
                   "one_game_swings": int(train["hitter_oe"].null_count())}


# ── The model ────────────────────────────────────────────────────────────────────────────────────
class StuffModel:
    """The 9 boosters (3 groups x 3 pieces) plus everything grading needs, frozen together: the
    package A context weights, the averaged price list, HIT_AVG, the groups' average pieces (for the
    run breakdown), the Stuff+ scale and the pitch-group templates."""

    def __init__(self):
        self.boosters, self.weights, self.avg_pieces, self.scale, self.meta = {}, {}, {}, {}, {}
        self.prices = None          # 24-row arrays: whiff price, foul price, shift
        self.hit_avg = 0.0
        self.templates = None

    # pieces at the 24 contexts, for a block of pitches of one group
    def _pieces(self, g: str, d: pl.DataFrame):
        feats = feats_for(g)
        fixed = {"Balls": 0.0, "Strikes": 0.0, "same": 0.0, "hitter_oe": self.hit_avg}
        base = d.select([pl.lit(fixed[c]).alias(c) if c in fixed else pl.col(c) for c in feats]).to_numpy().astype(np.float32)
        ci, ctx = [feats.index(c) for c in ("Balls", "Strikes", "same")], np.array(CTX, dtype=np.float32)
        for a in range(0, len(base), CHUNK):
            B = base[a:a + CHUNK]
            Xr = np.repeat(B, len(CTX), axis=0)
            Xr[:, ci] = np.tile(ctx, (len(B), 1))
            yield a, len(B), [self.boosters[(g, p)].predict(Xr).reshape(len(B), len(CTX)) for p in PIECES]

    def _value(self, w, f, v):
        WP, FP, SH = self.prices
        return w * WP + (1 - w) * (f * FP + (1 - f) * (v - SH))

    def set_average_pieces(self, P: pl.DataFrame, n_sample: int = 200_000):
        for g in GROUPS:
            d = P.filter(pl.col("group") == g)
            d = d.sample(min(n_sample, d.height), seed=SEED)
            sums, cnt = [np.zeros(len(CTX)) for _ in PIECES], 0
            for _, n, arrs in self._pieces(g, d):
                for k, arr in enumerate(arrs):
                    sums[k] += arr.sum(0)
                cnt += n
            self.avg_pieces[g] = [s / cnt for s in sums]

    def raw_grades(self, P: pl.DataFrame) -> pl.DataFrame:
        """PitchUID + runs (expected runs per swing), the context-averaged pieces and the run breakdown
        (``d_*``, runs vs. a pitch with the group's average pieces). ``P`` = gradeable pitches."""
        keys = ["runs", "whiff", "foul", "contact", "d_whiff", "d_foul", "d_contact"]
        out = {k: np.full(P.height, np.nan) for k in keys}
        fr = P.with_row_index("_i")
        for (g, hand), d in fr.partition_by(["group", "PitcherThrows"], as_dict=True).items():
            if g not in GROUPS or hand not in self.weights.get(g, {}):
                continue
            w, (wb, fb, vb), idx = self.weights[g][hand], self.avg_pieces[g], d["_i"].to_numpy()
            V0 = self._value(wb, fb, vb)
            for a, n, (pw, pf, pv) in self._pieces(g, d):
                V, V1, V2 = self._value(pw, pf, pv), self._value(pw, fb, vb), self._value(pw, pf, vb)
                sl = idx[a:a + n]
                out["runs"][sl], out["whiff"][sl], out["foul"][sl], out["contact"][sl] = V @ w, pw @ w, pf @ w, pv @ w
                out["d_whiff"][sl], out["d_foul"][sl], out["d_contact"][sl] = (V1 - V0) @ w, (V2 - V1) @ w, (V - V2) @ w
        return P.select("PitchUID").with_columns(**{k: pl.Series(v) for k, v in out.items()})

    # (column, output name, higher raw value is better for the pitcher)
    PLUS = [("runs", "stuff", False), ("whiff", "whiff_plus", True), ("foul", "foul_plus", True),
            ("contact", "contact_plus", False)]

    def set_scale(self, P: pl.DataFrame, raw: pl.DataFrame):
        """Freeze the per-group mean/SD that define 100 and 10 points (PR2: ``SCALE_METHOD``)."""
        d = P.select("PitchUID", "pid", "Year", "group", "retag").join(raw, on="PitchUID")
        if SCALE_METHOD == "per_pitch_type":
            d = (d.group_by("pid", "Year", "retag", "group").agg(pl.len().alias("n"), *[pl.col(c).mean() for c, _, _ in self.PLUS])
                  .filter(pl.col("n") >= MIN_PT))
        for g in GROUPS:
            x = d.filter(pl.col("group") == g)
            self.scale[g] = {c: (float(x[c].mean()), float(x[c].std())) for c, _, _ in self.PLUS}
        self.meta["scale_method"] = SCALE_METHOD

    def grade(self, P: pl.DataFrame) -> pl.DataFrame:
        """PitchUID + Stuff+, Whiff+, Foul+, Contact+, expected runs per swing and runs saved per 100
        swings by piece (positive = good for the pitcher)."""
        return self.apply_scale(self.raw_grades(P), P)

    def apply_scale(self, raw: pl.DataFrame, P: pl.DataFrame) -> pl.DataFrame:
        raw = raw.join(P.select("PitchUID", "group"), on="PitchUID")
        exprs = []
        for c, name, higher in self.PLUS:
            mu = pl.col("group").replace_strict({g: self.scale[g][c][0] for g in GROUPS}, default=None)
            sd = pl.col("group").replace_strict({g: self.scale[g][c][1] for g in GROUPS}, default=None)
            exprs.append((100 + 10 * (1 if higher else -1) * (pl.col(c) - mu) / sd).alias(name))
        return (raw.with_columns(exprs)
                   .with_columns(*[(-100 * pl.col(f"d_{p}")).alias(f"saved_{p}") for p in PIECES])
                   .select("PitchUID", "stuff", "whiff_plus", "foul_plus", "contact_plus", "runs",
                           "saved_whiff", "saved_foul", "saved_contact"))

    # ── persistence: LightGBM text + JSON (no pickle; survives library upgrades, readable) ──
    def save(self, path):
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        for (g, p), b in self.boosters.items():
            b.save_model(str(base.with_name(f"{base.name}_{g}_{p}.txt")))
        with open(base.with_name(base.name + ".json"), "w") as f:
            json.dump({"version": MODEL_VERSION, "run_by_group": RUN_BY_GROUP,
                       "feats": {g: feats_for(g) for g in GROUPS}, "ctx": CTX,
                       "weights": {g: {h: w.tolist() for h, w in hw.items()} for g, hw in self.weights.items()},
                       "prices": [a.tolist() for a in self.prices], "hit_avg": self.hit_avg,
                       "avg_pieces": {g: [a.tolist() for a in v] for g, v in self.avg_pieces.items()},
                       "scale": self.scale, "templates": self.templates.to_dict(), "meta": self.meta}, f, indent=1)
        return base

    @classmethod
    def load(cls, path):
        import lightgbm as lgb
        from backend.models import pitch_groups as pg
        base, m = Path(path), cls()
        with open(base.with_name(base.name + ".json")) as f:
            cfg = json.load(f)
        if cfg["run_by_group"] != RUN_BY_GROUP or cfg["feats"] != {g: feats_for(g) for g in GROUPS}:
            raise ValueError(f"{base.name}: saved features don't match this code; retrain")
        for g in GROUPS:
            for p in PIECES:
                m.boosters[(g, p)] = lgb.Booster(model_file=str(base.with_name(f"{base.name}_{g}_{p}.txt")))
        m.weights = {g: {h: np.array(w) for h, w in hw.items()} for g, hw in cfg["weights"].items()}
        m.prices = tuple(np.array(a) for a in cfg["prices"])
        m.hit_avg, m.scale, m.meta = cfg["hit_avg"], {g: {c: tuple(v) for c, v in s.items()} for g, s in cfg["scale"].items()}, cfg["meta"]
        m.avg_pieces = {g: [np.array(a) for a in v] for g, v in cfg["avg_pieces"].items()}
        m.templates = pg.Templates.from_dict(cfg["templates"])
        return m


# ── Training and scoring ─────────────────────────────────────────────────────────────────────────
def _context_weights(train: pl.DataFrame) -> dict:
    cmix = (train.group_by("group", "Balls", "Strikes").agg(n=pl.len())
                 .with_columns(w=pl.col("n") / pl.col("n").sum().over("group")))
    pmix = train.group_by("group", "PitcherThrows").agg(p_same=pl.col("same").mean())
    out = {}
    for g in GROUPS:
        cw = {(r["Balls"], r["Strikes"]): r["w"] for r in cmix.filter(pl.col("group") == g).iter_rows(named=True)}
        out[g] = {}
        for r in pmix.filter(pl.col("group") == g).iter_rows(named=True):
            ps = r["p_same"]
            out[g][r["PitcherThrows"]] = np.array([cw.get((b, s), 0.0) * (ps if side else 1 - ps) for b, s, side in CTX])
    return out


def _log(msg, t0):
    print(f"  {msg} ({time.time() - t0:.0f}s)", flush=True)


def train(years: list[str] | None = None, fraction: float = 1.0) -> tuple[StuffModel, pl.DataFrame]:
    """Fit the production model on every pitcher of ``years`` (verified games), then grade every pitch.
    Returns (model, PitchUID -> grades + labels). ``fraction`` < 1 keeps a slice of pitchers (smoke tests)."""
    t0 = time.time()
    years = years or available_years()
    raw = load_pitches(years)
    _log(f"loaded {raw.height:,} pitches, {years[0]}-{years[-1]}", t0)
    raw, merged = merge_pitcher_ids(raw)
    _log(f"pitcher ids merged: {merged}", t0)
    if fraction < 1:                         # smoke test: a slice of pitchers (tagging included)
        raw = raw.filter((pl.col("pid").hash(SEED) % 10_000) < int(fraction * 10_000))
    raw, templates = add_labels(raw)
    _log("pitch groups tagged", t0)
    season = season_prices(years)
    raw = add_features(add_targets(raw, season))
    P = raw.filter(pl.col("gradeable"))
    train_ = _assign_folds(P.filter(pl.col("is_verified"), pl.col("y").is_not_null(), pl.col("same").is_not_null()))
    _log(f"{train_.height:,} training swings, {P.height:,} gradeable pitches", t0)
    train_, hit = hitter_feature(train_)
    _log(f"hitter feature: {hit}", t0)

    model = StuffModel()
    model.templates, model.hit_avg = templates, hit["hit_avg"]
    avg = averaged_prices(season, train_)
    model.prices = tuple(np.array([dict(zip(zip(avg["Balls"], avg["Strikes"]), avg[c]))[(b, s)] for b, s, _ in CTX])
                         for c in ("whiff_price", "foul_price", "shift"))
    model.weights = _context_weights(train_)
    rows = {"whiff": (pl.lit(True), "is_whiff", "binary"),
            "foul": (pl.col("is_foul").is_not_null(), "is_foul", "binary"),
            "contact": (pl.col("contact_value").is_not_null(), "contact_value", "regression")}
    trees = {}
    for g in GROUPS:
        dg = train_.filter(pl.col("group") == g)
        for p, (keep, label, objective) in rows.items():
            model.boosters[(g, p)] = b = _fit(dg.filter(keep), feats_for(g), label, objective, refit_all=True)
            trees[f"{g}_{p}"] = b.num_trees()
    _log(f"pieces fitted: {trees}", t0)
    model.set_average_pieces(P)
    raw_g = model.raw_grades(P)
    model.set_scale(P, raw_g)
    grades = model.apply_scale(raw_g, P)
    _log(f"graded {grades.height:,} pitches", t0)
    model.meta = {**model.meta, "version": MODEL_VERSION, "level": LEVEL, "years": years, "fraction": fraction,
                  "n_swings": train_.height, "n_pitches": P.height, "trees": trees, **merged, **hit,
                  "trained": time.strftime("%Y-%m-%d %H:%M")}
    return model, _with_labels(grades, P)


def _with_labels(grades: pl.DataFrame, P: pl.DataFrame) -> pl.DataFrame:
    return (P.select("PitchUID", "Year", "group", "retag", "type").join(grades, on="PitchUID")
             .with_columns(pl.exclude("PitchUID", "Year", "group", "retag", "type").cast(pl.Float32)))


def prepare(raw: pl.DataFrame, model: StuffModel, n_jobs: int = -1) -> pl.DataFrame:
    """Raw pitches (``load_pitches`` columns, or a TrackMan CSV with the same names + ``Year``) ->
    gradeable pitches with features, tagged with the model's frozen templates."""
    from backend.models import pitch_groups as pg
    raw = pg.fill_pitcher_ids(raw)
    raw, _ = merge_pitcher_ids(raw)
    raw, _ = add_labels(raw, model.templates, n_jobs=n_jobs)
    return add_features(raw).filter(pl.col("gradeable"))


def score(raw: pl.DataFrame, model: StuffModel, n_jobs: int = -1) -> pl.DataFrame:
    """PitchUID -> group, retag, type, Stuff+, Whiff+, Foul+, Contact+, runs and the run breakdown.
    ``n_jobs=1`` for a few pitchers (the app's live pass); the default for whole seasons."""
    P = prepare(raw, model, n_jobs=n_jobs)
    return _with_labels(model.grade(P), P) if P.height else pl.DataFrame()
