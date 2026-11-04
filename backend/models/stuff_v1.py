"""Stuff+ v1.1, the production model (Model 2: probability pieces). What a pitch's SHAPE is worth.

**v1.1** (2026-11, ``backend/research/stuff_model/p4_stuff.md``, evidence in ``stuff_v1_1_skills.ipynb``
and ``stuff_v1_1_p4view.ipynb``): three hitter skills replace ``hitter_oe``, and every pitch has two views.
- **Skills** (``skill_features``): whiff, foul and contact skill, each built the old ``hitter_oe`` way per
  channel and shrunk toward the hitter's GROUP average (P4 or not, ``p4_hitters``), in every piece.
- **Grading against a hitter mix**, not one average hitter: 16 hitter prototypes (k-means cells of the D1
  hitters' skills). The D1 view weights them by the D1 mix, the P4 view by the P4 hitters' mix. The shared
  cells miss part of the D1 -> P4 shift, so the P4 view adds one frozen correction per group, measured at
  training against a 2,048-hitter reference mix. Both views sit on the D1 scale (``*_p4`` columns).
- Re-scoring reuses the training pitcher-ID merge (``pid_map``), closing the 0.08% re-score gap.
The text below describes v1, and still holds except where these replace it.

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

**Platoon splits** (``*_rhb`` / ``*_lhb``): the same four grades with the weights restricted to one
batter side, on the same frozen scale, so a vs-RHB 110 and a blended 110 mean the same thing. The model's
batter feature is same/opposite hand, so vs RHB is the same-hand half for a RHP and the opposite-hand half
for a LHP. The blended grade is the weighted mix of the two.

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
MODEL_VERSION = "v1.1"
GROUPS = ["FB", "BB", "OS"]
PIECES = ["whiff", "foul", "contact"]
SWING_CALLS = ["StrikeSwinging", "InPlay", "FoulBall", "FoulBallFieldable", "FoulBallNotFieldable"]
FOULS = ["FoulBall", "FoulBallFieldable", "FoulBallNotFieldable"]

# Features (F1-F7). Raw shape per run; the differentials vs. FB_1 are movement in both runs.
RAW = {"movement": ["RelSpeed", "IVB", "HB", "SpinRate", "RelHeight", "RelSideM"],
       "acceleration": ["RelSpeed", "acc_v", "acc_h", "SpinRate", "RelHeight", "RelSideM"]}
DIFFS = ["dVelo", "dHB", "dIVB"]
SKILL_COLS = ["whiff_skill", "foul_skill", "contact_skill"]                 # v1.1: in place of hitter_oe (P4S4)
CONTEXT = ["Balls", "Strikes", "same", *SKILL_COLS]
RUN_BY_GROUP = {"FB": "movement", "BB": "acceleration", "OS": "movement"}      # PR1
SHAPE_ALL = sorted(set(RAW["movement"]) | set(RAW["acceleration"]))
STAGE1_FEATS = RAW["movement"] + DIFFS + ["Balls", "Strikes", "same"]         # no hitter feature (C3)


def feats_for(group: str) -> list[str]:
    return RAW[RUN_BY_GROUP[group]] + DIFFS + CONTEXT


# The grading contexts: 12 counts x (same-hand, opposite-hand) batter.
CTX = [(b, s, side) for b in range(4) for s in range(3) for side in (1, 0)]
SAME = np.array([side for _, _, side in CTX], dtype=float)     # 1 at the same-hand contexts
SIDES = ("rhb", "lhb")                                          # the platoon-split suffixes


def side_weights(w: np.ndarray, hand: str) -> dict[str, np.ndarray]:
    """A pitch's 24 grading weights restricted to one batter side and renormalized: vs RHB, vs LHB."""
    same, opp = w * SAME, w * (1 - SAME)
    same, opp = same / same.sum(), opp / opp.sum()
    return {"rhb": same, "lhb": opp} if hand == "Right" else {"rhb": opp, "lhb": same}

LGB_PARAMS = dict(learning_rate=0.05, num_leaves=31, max_depth=6, min_child_samples=500,
                  n_jobs=-1, verbose=-1, seed=42)
MAX_TREES, PATIENCE, N_FOLDS, SEED = 3000, 50, 5, 42
K_REF = 10                                   # FB_1 reference: (n·outing + 10·season) / (n + 10)  (F3)
GAP_MAX, REL_H_MAX, REL_S_MAX = 2, 0.75, 1.0  # pitcher-ID merge, rule C (M2)
# PR2: which population sets the Stuff+ scale. "per_pitch" = every graded pitch (10 points = 1 SD of single
# pitches); "per_pitch_type" = pitch type-seasons with >= MIN_PT pitches (10 points = 1 SD of pitch types,
# the grades the app's tables show). Decided per pitch 2026-10-16; switched to per pitch type 2026-11-03.
# Only the frozen mean/SD change; nothing else in the model does (stuff_store --rescale re-maps saved grades).
SCALE_METHOD = "per_pitch_type"
MIN_PT = 100
CHUNK = 120_000                              # pitches x hitter points per predict call (x 24 contexts): ~2.9M rows
GRADE_CHUNK = 500_000                        # pitches per grading block, so grading memory stays flat
# v1.1 hitter mixes (P4S1-P4S3, P4S6). P4 hitter = team -> home-game conference table (PAC-12 counts
# through 2024); team-seasons with < MIN_HOME tracked home games fall back to >= P4_SHARE of swings in P4 games.
PAC12_LAST, MIN_HOME, P4_SHARE = 2024, 5, 0.70
N_PROTO = 16                                 # shared hitter prototypes (P4S2): 16 points per pitch for both views
MIX_SAMPLE = 200_000                         # swings sampled per hitter mix (k-means and the reference)
REF_CELLS, REF_DRAWS = 256, 4                # the P4-shift reference: 4 random hitters from each of 256 cells per mix
REF_FIT = 400                                # pitches per group the P4-view correction is measured on
AVG_SAMPLE = 20_000                          # pitches per group for the average pieces (the run breakdown)
# The columns grading needs (the boosters' shape features + the labels and keys the outputs carry).
GRADE_COLS = ["PitchUID", "pid", "Year", "group", "retag", "type", "PitcherThrows", *SHAPE_ALL, *DIFFS]
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


def load_pitches(years: list[str] | None = None, with_outcomes: bool = True,
                 columns: list[str] | None = None) -> pl.DataFrame:
    """Every D1 pitch of ``years``. Scans both physical partitions and filters on Level, like eye: a
    level's rows are not confined to its own partition. ``with_outcomes`` adds the run-value state
    and ``is_verified`` (training needs them; grading doesn't). ``columns`` keeps only those (plus
    PitchUID and Year), for callers that need a few columns of every season."""
    from backend.models import pitch_groups as pg, rv_store
    years = years or available_years()
    files = sorted(f for y in years for f in PIPELINE.glob(f"*/{y}/**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for years={years} under {PIPELINE}")
    lf = pl.scan_parquet([str(f) for f in files])
    names = set(lf.collect_schema().names())
    extra = ([*rv_store.ROW_STATE, "is_verified"] if with_outcomes else [])
    keep = set(columns) | {"PitchUID", "Date", "Level"} if columns else None
    cols = [c for c in _COLS + extra if c in names and (keep is None or c in keep)]
    raw = (lf.select(cols).filter(pl.col("Level") == LEVEL).drop("Level")
             .with_columns(Year=pl.col("Date").str.slice(0, 4).cast(pl.Int32)).drop("Date")
             .unique(subset=["PitchUID"], keep="first").collect())
    if with_outcomes and "is_verified" not in raw.columns:
        raw = raw.with_columns(is_verified=pl.lit(True))
    return pg.fill_pitcher_ids(raw)


def p4_hitters(years: list[str]) -> pl.DataFrame:
    """v1.1 (P4S1): BatterId, Year, P4h for every hitter-season with a D1 swing. A hitter is P4 when his most
    common team plays its home games in a P4 conference that season (``League`` is the home team's
    conference; PAC-12 counts through 2024). A team-season with fewer than MIN_HOME tracked home games isn't
    in that table, and its hitters fall back to their own share of swings in P4-conference games (>= P4_SHARE).
    It reads the team columns training doesn't load, in a short-lived child process: the operating system takes
    all of its memory back before training loads every pitch (measured: in-process, polars kept ~3 GB of it)."""
    import concurrent.futures as cf
    import multiprocessing as mp
    try:
        with cf.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as ex:
            return ex.submit(_p4_hitters_table, years).result()
    except (OSError, cf.process.BrokenProcessPool):          # no child processes here: same result, in-process
        return _p4_hitters_table(years)


def _p4_hitters_table(years: list[str]) -> pl.DataFrame:
    from backend.models import contact_quality as cq
    files = sorted(f for y in years for f in PIPELINE.glob(f"*/{y}/**/*.parquet"))
    p4 = lambda c: (pl.col(c).is_in(cq.P4_LEAGUES) | ((pl.col(c) == "PAC12") & (pl.col("Year") <= PAC12_LAST))).fill_null(False)
    sw = (pl.scan_parquet([str(f) for f in files])
            .select("PitchUID", "Date", "Level", "League", "BatterId", "BatterTeam", "HomeTeam", "GameID", "PitchCall")
            .filter(pl.col("Level") == LEVEL, pl.col("PitchCall").is_in(SWING_CALLS), pl.col("BatterId").is_not_null())
            .unique(subset=["PitchUID"], keep="first")
            .with_columns(Year=pl.col("Date").str.slice(0, 4).cast(pl.Int32))
            .select("BatterId", "Year", "GameID", "League", "BatterTeam", "HomeTeam").collect(engine="streaming"))
    home = (sw.unique("GameID").filter(pl.col("HomeTeam").is_not_null())
              .group_by(pl.col("HomeTeam").alias("team"), "Year")
              .agg(conf=pl.col("League").drop_nulls().mode().sort().first(), home_games=pl.len()))
    return (sw.with_columns(P4g=p4("League"))
              .group_by("BatterId", "Year").agg(share=pl.col("P4g").mean(), team=pl.col("BatterTeam").drop_nulls().mode().sort().first())
              .join(home, on=["team", "Year"], how="left")
              .select("BatterId", "Year", P4h=pl.when((pl.col("home_games") >= MIN_HOME).fill_null(False)).then(p4("conf"))
                                                .otherwise(pl.col("share") >= P4_SHARE)))


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


_SKILL_DEFS = {"whiff_skill": ("is_whiff", pl.lit(True), "binary"),                       # per swing
               "foul_skill": ("is_foul", pl.col("is_foul").is_not_null(), "binary"),       # per contact swing
               "contact_skill": ("contact_value", pl.col("contact_value").is_not_null(), "regression")}   # per BIP


def skill_features(train: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """v1.1 (P3, P4S4, P4S6): the three hitter skills that replace ``hitter_oe``, each the C3 way for its
    own channel: a stage-1 model with no hitter input, cross-fit by pitcher fold; each swing's residual;
    each hitter-season's residuals from his OTHER games, shrunk toward his group's average (P4 or not,
    ``P4h``) with the within-group k: (sum + k·group mean) / (n + k). One-game hitters and swings with no
    BatterId stay missing. ``train`` needs fold, es, group, P4h, BatterId, GameID and the stage-1 features."""
    keys = train.select("PitchUID", "BatterId", "Year", "GameID", "P4h")
    out, info = keys.select("PitchUID"), {}
    for name, (label, keep, objective) in _SKILL_DEFS.items():
        res = []
        for g in GROUPS:
            dg = train.filter(pl.col("group") == g, keep)
            for f in range(N_FOLDS):
                m = _fit(dg.filter(pl.col("fold") != f), STAGE1_FEATS, label, objective)
                ho = dg.filter(pl.col("fold") == f)
                p = m.predict(_X(ho, STAGE1_FEATS), num_iteration=m.best_iteration)
                res.append(ho.select("PitchUID").with_columns(pl.Series("res", ho[label].cast(pl.Float64).to_numpy() - p)))
        r = pl.concat(res).join(keys, on="PitchUID").filter(pl.col("BatterId").is_not_null())
        mu = r.group_by("P4h").agg(mu=pl.col("res").mean())
        bs = (r.group_by("BatterId", "Year", "P4h").agg(n=pl.len(), s=pl.col("res").sum(), ss=(pl.col("res") ** 2).sum())
                .with_columns(m=pl.col("s") / pl.col("n")).join(mu, on="P4h"))
        b = bs.filter(pl.col("n") >= 30)
        b = b if b.height >= 100 else bs.filter(pl.col("n") >= 2)       # smoke tests: few hitters reach 30
        noise = (b["ss"] - b["n"] * b["m"] ** 2).sum() / (b["n"] - 1).sum()
        true = (b["m"] - b["mu"]).var() - noise * (1 / b["n"]).mean()
        k = noise / true if true > 0 else 1e4
        bg = r.group_by("BatterId", "Year", "GameID").agg(n_g=pl.len(), s_g=pl.col("res").sum())
        sk = (keys.join(bs.select("BatterId", "Year", "n", "s", "mu"), on=["BatterId", "Year"], how="left")
                  .join(bg, on=["BatterId", "Year", "GameID"], how="left")
                  .with_columns(pl.col("n_g", "s_g").fill_null(0))
                  .with_columns(n_o=pl.col("n") - pl.col("n_g"), s_o=pl.col("s") - pl.col("s_g"))
                  .select("PitchUID", pl.when(pl.col("n_o") > 0).then((pl.col("s_o") + k * pl.col("mu")) / (pl.col("n_o") + k)).alias(name)))
        out = out.join(sk, on="PitchUID", how="left")
        mus = dict(zip(mu["P4h"].to_list(), mu["mu"].to_list()))
        info[name] = {"k": float(k), "mean_p4": float(mus.get(True, float("nan"))), "mean_other": float(mus.get(False, float("nan")))}
        del r, res, bs, bg, sk
    train = train.join(out, on="PitchUID", how="left")
    info["missing_swings"] = {c: int(train[c].null_count()) for c in SKILL_COLS}
    return train, info


def hitter_feature(train: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """v1's hitter number (C3), kept for the research notebooks; v1.1 trains on ``skill_features``.
    Stage-1 held-out predictions (no hitter feature) → each hitter's swing value over expected,
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
RAW4 = ("runs", "whiff", "foul", "contact")
VIEWS = {"D1": "", "P4": "_p4"}                 # view -> column suffix


class StuffModel:
    """The 9 boosters (3 groups x 3 pieces) plus everything grading needs, frozen together: the
    package A context weights, the averaged price list, the hitter prototypes and each view's weights
    over them, the P4-view correction, the groups' average pieces (for the run breakdown), the Stuff+
    scale, the pitch-group templates and the training pitcher-ID merge."""

    def __init__(self):
        self.boosters, self.weights, self.avg_pieces, self.scale, self.meta = {}, {}, {}, {}, {}
        self.prices = None          # 24-row arrays: whiff price, foul price, shift
        self.points = None          # N_PROTO x 3 hitter prototypes (SKILL_COLS), shared by both views (P4S2)
        self.pop = {}               # view -> weights over the points: the D1 hitter mix / the P4 hitters' mix
        self.p4_fix = {}            # group -> raw value ("runs", "whiff_rhb", ...) -> frozen P4-view correction
        self.templates = None
        self.pid_map = None         # PitcherId -> pid of the training merge (re-scores reuse it)

    def _pieces(self, g: str, d: pl.DataFrame, points: np.ndarray):
        """The three pieces for a block of one group's pitches at every (hitter point, context): arrays of
        shape (pitches, points, 24). Each predict call is at most CHUNK x 24 rows, whatever the number of
        points, so memory doesn't grow with them."""
        feats = feats_for(g)
        K, nc = len(points), len(CTX)
        lit = ("Balls", "Strikes", "same", *SKILL_COLS)
        base = d.select([pl.lit(0.0).alias(c) if c in lit else pl.col(c) for c in feats]).to_numpy().astype(np.float32)
        ci, hi = [feats.index(c) for c in ("Balls", "Strikes", "same")], [feats.index(c) for c in SKILL_COLS]
        ctx_k = np.tile(np.array(CTX, dtype=np.float32), (K, 1))                 # point 0's 24 contexts, then point 1's, ...
        pts_k = np.repeat(np.asarray(points, dtype=np.float32), nc, axis=0)
        step = max(1, CHUNK // K)
        for a in range(0, len(base), step):
            B = base[a:a + step]
            n = len(B)
            Xr = np.repeat(B, K * nc, axis=0)
            Xr[:, ci] = np.tile(ctx_k, (n, 1))
            Xr[:, hi] = np.tile(pts_k, (n, 1))
            yield a, n, [self.boosters[(g, p)].predict(Xr).reshape(n, K, nc) for p in PIECES]
            del Xr

    def _value(self, w, f, v):
        WP, FP, SH = self.prices
        return w * WP + (1 - w) * (f * FP + (1 - f) * (v - SH))

    def set_average_pieces(self, P: pl.DataFrame, n_sample: int = AVG_SAMPLE):
        """Each group's average pieces at every (point, context), the run breakdown's baseline."""
        P = P.select("group", *SHAPE_ALL, *DIFFS)
        for g in GROUPS:
            d = P.filter(pl.col("group") == g)
            d = d.sample(min(n_sample, d.height), seed=SEED)
            sums, cnt = [np.zeros((len(self.points), len(CTX))) for _ in PIECES], 0
            for _, n, arrs in self._pieces(g, d, self.points):
                for k, arr in enumerate(arrs):
                    sums[k] += arr.sum(0)
                cnt += n
            self.avg_pieces[g] = [s / cnt for s in sums]

    def raw_grades(self, P: pl.DataFrame) -> pl.DataFrame:
        """PitchUID + runs (expected runs per swing), the averaged pieces, the run breakdown (``d_*``, runs
        vs. a pitch with the group's average pieces, D1 view) and the platoon splits, for both views (the P4
        view as ``*_p4``). ``P`` = gradeable pitches, graded ``GRADE_CHUNK`` at a time (each pitch's grade
        depends only on its own row)."""
        if P.height <= GRADE_CHUNK:
            return self._raw_grades_block(P)
        return pl.concat([self._raw_grades_block(P.slice(a, GRADE_CHUNK)) for a in range(0, P.height, GRADE_CHUNK)])

    def _raw_grades_block(self, P: pl.DataFrame) -> pl.DataFrame:
        out = self._grade_block(P, self.points, self.pop, fix=True, breakdown=True)
        return P.select("PitchUID").with_columns(**{k: pl.Series(v) for k, v in out.items()})

    def _grade_block(self, P: pl.DataFrame, points, pop: dict, fix: bool, breakdown: bool) -> dict:
        """Raw values per pitch for each view in ``pop`` (view -> weights over ``points``): every point x context
        prediction averaged with the context weights x the view's point weights. ``fix`` adds the frozen P4
        correction; ``breakdown`` adds the D1 view's run breakdown (needs the average pieces)."""
        keys = [f"{k}{VIEWS[v]}{'_' + s if s else ''}" for v in pop for s in ("", *SIDES) for k in RAW4]
        keys += ["d_whiff", "d_foul", "d_contact"] if breakdown else []
        out = {k: np.full(P.height, np.nan) for k in keys}
        fr = P.select("group", "PitcherThrows", *SHAPE_ALL, *DIFFS).with_row_index("_i")
        pis = {v: np.asarray(w, dtype=float) for v, w in pop.items()}
        for (g, hand), d in fr.partition_by(["group", "PitcherThrows"], as_dict=True).items():
            if g not in GROUPS or hand not in self.weights.get(g, {}):
                continue
            w, idx = self.weights[g][hand], d["_i"].to_numpy()
            ctx_w = {"": w, **side_weights(w, hand)}
            J = {(v, s): np.outer(pis[v], cw) for v in pis for s, cw in ctx_w.items()}      # joint (point, context) weights
            if breakdown:
                wb, fb, vb = self.avg_pieces[g]
                V0 = self._value(wb, fb, vb)
            for a, n, (pw, pf, pv) in self._pieces(g, d, points):
                V = self._value(pw, pf, pv)
                sl = idx[a:a + n]
                for (v, s), Jw in J.items():
                    for k, arr in zip(RAW4, (V, pw, pf, pv)):
                        col = f"{k}{'_' + s if s else ''}"
                        corr = self.p4_fix[g][col] if fix and v == "P4" else 0.0
                        out[f"{k}{VIEWS[v]}{'_' + s if s else ''}"][sl] = np.einsum("nkc,kc->n", arr, Jw) + corr
                if breakdown:
                    J1 = J[("D1", "")]
                    V1, V2 = self._value(pw, fb, vb), self._value(pw, pf, vb)
                    out["d_whiff"][sl] = np.einsum("nkc,kc->n", V1 - V0, J1)
                    out["d_foul"][sl] = np.einsum("nkc,kc->n", V2 - V1, J1)
                    out["d_contact"][sl] = np.einsum("nkc,kc->n", V - V2, J1)
        return out

    # (column, output name, higher raw value is better for the pitcher)
    PLUS = [("runs", "stuff", False), ("whiff", "whiff_plus", True), ("foul", "foul_plus", True),
            ("contact", "contact_plus", False)]
    SIDE_COLS = [f"{name}_{s}" for _, name, _ in PLUS for s in SIDES]       # stuff_rhb, stuff_lhb, ..., contact_plus_lhb
    P4_COLS = [f"{name}_p4{'_' + s if s else ''}" for _, name, _ in PLUS for s in ("", *SIDES)]   # stuff_p4, stuff_p4_rhb, ...

    def set_scale(self, P: pl.DataFrame, raw: pl.DataFrame, method: str = SCALE_METHOD):
        """Freeze the per-group mean/SD that define 100 and 10 points (PR2: ``SCALE_METHOD``)."""
        d = P.select("PitchUID", "pid", "Year", "group", "retag").join(raw, on="PitchUID")
        if method == "per_pitch_type":
            d = (d.filter(pl.col("pid").is_not_null())
                  .group_by("pid", "Year", "retag", "group").agg(pl.len().alias("n"), *[pl.col(c).mean() for c, _, _ in self.PLUS])
                  .filter(pl.col("n") >= MIN_PT))
        for g in GROUPS:
            x = d.filter(pl.col("group") == g)
            self.scale[g] = {c: (float(x[c].mean()), float(x[c].std())) for c, _, _ in self.PLUS}
        self.meta["scale_method"] = method

    def unscale(self, grades: pl.DataFrame, scale: dict | None = None) -> pl.DataFrame:
        """Saved grades (PitchUID, group, stuff, ...) -> PitchUID + the raw values they were scaled from
        (runs, whiff, foul, contact). Exact, because each grade is linear in its raw value."""
        scale = scale or self.scale
        return grades.select("PitchUID", *[self._by_group(lambda g, c=c, n=n, h=h: scale[g][c][0] + (1 if h else -1)
                                                          * (pl.col(n) - 100) * scale[g][c][1] / 10).alias(c)
                                           for c, n, h in self.PLUS])

    def remap(self, grades: pl.DataFrame, old_scale: dict) -> pl.DataFrame:
        """Saved grades made on ``old_scale`` -> the same grades on this model's scale, blends and platoon
        splits alike, without re-grading: recover each raw value, then scale it again."""
        exprs = []
        for c, name, higher in self.PLUS:
            s = 1 if higher else -1
            for col in [f"{name}{view}{'_' + x if x else ''}" for view in VIEWS.values() for x in ("", *SIDES)]:
                if col in grades.columns:
                    exprs.append(self._by_group(lambda g, c=c, col=col, s=s: 100 + 10 * s * (
                        old_scale[g][c][0] + s * (pl.col(col) - 100) * old_scale[g][c][1] / 10 - self.scale[g][c][0])
                        / self.scale[g][c][1]).cast(grades.schema[col]).alias(col))
        return grades.with_columns(exprs)

    @staticmethod
    def _by_group(expr_of):
        """One expression per pitch group, picked by the row's ``group`` (null outside the three)."""
        e = pl.when(pl.col("group") == GROUPS[0]).then(expr_of(GROUPS[0]))
        for g in GROUPS[1:]:
            e = e.when(pl.col("group") == g).then(expr_of(g))
        return e.otherwise(None)

    def grade(self, P: pl.DataFrame) -> pl.DataFrame:
        """PitchUID + Stuff+, Whiff+, Foul+, Contact+, expected runs per swing, runs saved per 100
        swings by piece (positive = good for the pitcher) and the four grades vs RHB / vs LHB only."""
        return self.apply_scale(self.raw_grades(P), P)

    def apply_scale(self, raw: pl.DataFrame, P: pl.DataFrame) -> pl.DataFrame:
        raw = raw.join(P.select("PitchUID", "group"), on="PitchUID")
        exprs = []
        for c, name, higher in self.PLUS:
            mu = pl.col("group").replace_strict({g: self.scale[g][c][0] for g in GROUPS}, default=None)
            sd = pl.col("group").replace_strict({g: self.scale[g][c][1] for g in GROUPS}, default=None)
            # the splits and the P4 view all use the blended D1 grade's scale (P4S7: the P4 view reads as a shift)
            for suf in [f"{view}{'_' + s if s else ''}" for view in VIEWS.values() for s in ("", *SIDES)]:
                exprs.append((100 + 10 * (1 if higher else -1) * (pl.col(c + suf) - mu) / sd).alias(name + suf))
        return (raw.with_columns(exprs)
                   .with_columns(*[(-100 * pl.col(f"d_{p}")).alias(f"saved_{p}") for p in PIECES])
                   .select("PitchUID", "stuff", "whiff_plus", "foul_plus", "contact_plus", "runs",
                           "saved_whiff", "saved_foul", "saved_contact", *self.SIDE_COLS, "runs_p4", *self.P4_COLS))

    # ── persistence: LightGBM text + JSON (no pickle; survives library upgrades, readable) ──
    def save(self, path, boosters: bool = True):
        """``boosters=False`` writes only the JSON (a rescale leaves the 9 LightGBM files as they are)."""
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        for (g, p), b in (self.boosters.items() if boosters else ()):
            b.save_model(str(base.with_name(f"{base.name}_{g}_{p}.txt")))
        if boosters and self.pid_map is not None:
            self.pid_map.write_parquet(base.with_name(base.name + "_pids.parquet"))
        with open(base.with_name(base.name + ".json"), "w") as f:
            json.dump({"version": MODEL_VERSION, "run_by_group": RUN_BY_GROUP,
                       "feats": {g: feats_for(g) for g in GROUPS}, "ctx": CTX,
                       "weights": {g: {h: w.tolist() for h, w in hw.items()} for g, hw in self.weights.items()},
                       "prices": [a.tolist() for a in self.prices],
                       "points": np.asarray(self.points).tolist(), "pop": {v: np.asarray(w).tolist() for v, w in self.pop.items()},
                       "p4_fix": self.p4_fix,
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
        m.points, m.pop, m.p4_fix = np.array(cfg["points"]), {v: np.array(w) for v, w in cfg["pop"].items()}, cfg["p4_fix"]
        m.scale, m.meta = {g: {c: tuple(v) for c, v in s.items()} for g, s in cfg["scale"].items()}, cfg["meta"]
        m.avg_pieces = {g: [np.array(a) for a in v] for g, v in cfg["avg_pieces"].items()}
        m.templates = pg.Templates.from_dict(cfg["templates"])
        pids = base.with_name(base.name + "_pids.parquet")
        m.pid_map = pl.read_parquet(pids) if pids.exists() else None
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


def _peak_gb() -> float:
    """This process's peak memory so far (ru_maxrss is KB on Linux, bytes on macOS)."""
    import resource
    import sys
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 2**30 if sys.platform == "darwin" else r / 2**20


LOG_HOOK = None          # stuff_store --notify sets this: called with (message, seconds elapsed) for every log line


def _log(msg, t0):
    print(f"  {msg} ({time.time() - t0:.0f}s, peak {_peak_gb():.1f} GB)", flush=True)
    if LOG_HOOK is not None:
        LOG_HOOK(msg, time.time() - t0)


# Training swings keep only what the fits, the skills and the mixes read (memory).
TRAIN_COLS = ["PitchUID", "pid", "Year", "GameID", "BatterId", "group", "PitcherThrows",
              *SHAPE_ALL, *DIFFS, "Balls", "Strikes", "same", "y", "is_whiff", "is_foul", "contact_value"]


def _mix(train: pl.DataFrame, keep: pl.Expr) -> np.ndarray:
    """A hitter mix (P4S3: pooled seasons, weighted by swings): the skills of MIX_SAMPLE random training swings
    with all three skills (one-game hitters are left out, as hitter_oe's average left them out)."""
    d = train.filter(keep, pl.all_horizontal(pl.col(SKILL_COLS).is_not_null())).select(SKILL_COLS)
    return d.sample(min(MIX_SAMPLE, d.height), seed=SEED).to_numpy()


def _prototypes(Z1: np.ndarray, Z4: np.ndarray, K: int = N_PROTO) -> tuple[np.ndarray, dict]:
    """P4S2: K shared prototypes, k-means cells of the D1 mix (skills standardized by the D1 mix's SD), each
    cell's mean as its point. The D1 view weights cells by D1 swings, the P4 view by P4 hitters' swings."""
    from sklearn.cluster import KMeans
    sd = Z1.std(0)
    km = KMeans(n_clusters=K, n_init=4, random_state=SEED).fit(Z1 / sd)
    return km.cluster_centers_ * sd, {"D1": np.bincount(km.labels_, minlength=K) / len(Z1),
                                      "P4": np.bincount(km.predict(Z4 / sd), minlength=K) / len(Z4)}


def _reference(Z1: np.ndarray, Z4: np.ndarray) -> tuple[np.ndarray, dict]:
    """The stand-in for the full hitter mix: per mix, REF_CELLS k-means cells and REF_DRAWS random hitters from
    each, weighted by cell share / REF_DRAWS. Random draws within cells are unbiased for the mix, where cell
    means aren't (a grade is a step function of the skills)."""
    from sklearn.cluster import KMeans
    rng = np.random.default_rng(SEED)
    sd, blocks = Z1.std(0), []
    for Z in (Z1, Z4):
        k = min(REF_CELLS, len(Z) // (2 * REF_DRAWS))
        lab = KMeans(n_clusters=k, n_init=1, random_state=SEED).fit(Z / sd).labels_
        pick = np.stack([rng.choice(np.flatnonzero(lab == c), size=REF_DRAWS, replace=True) for c in range(k)])
        blocks.append((Z[pick.ravel()], np.repeat(np.bincount(lab, minlength=k) / len(Z), REF_DRAWS) / REF_DRAWS))
    (p1, w1), (p4, w4) = blocks
    return np.vstack([p1, p4]), {"D1": np.r_[w1, np.zeros(len(p4))], "P4": np.r_[np.zeros(len(p1)), w4]}


def _p4_correction(model: StuffModel, P: pl.DataFrame, Z1: np.ndarray, Z4: np.ndarray) -> dict:
    """The shared cells miss part of the D1 -> P4 shift: P4 hitters sit higher INSIDE each D1 cell, which
    reweighting cells can't see. On REF_FIT random pitches per group, per raw value (blend and each side):
    the reference mix's average shift minus the prototypes'. The miss is near-constant across pitch types
    (stuff_v1_1_p4view.ipynb §10: shared 16 + this correction is within 0.11-0.20 of the reference by pitch type)."""
    fit = pl.concat([(d := P.filter(pl.col("group") == g)).sample(min(REF_FIT, d.height), seed=SEED) for g in GROUPS])
    ref = model._grade_block(fit, *_reference(Z1, Z4), fix=False, breakdown=False)
    pro = model._grade_block(fit, model.points, model.pop, fix=False, breakdown=False)
    grp = fit["group"].to_numpy()
    out = {}
    for g in GROUPS:
        m = grp == g
        out[g] = {}
        for s in ("", *SIDES):
            for k in RAW4:
                col, col4 = f"{k}{'_' + s if s else ''}", f"{k}_p4{'_' + s if s else ''}"
                out[g][col] = float(np.nanmean(ref[col4][m] - ref[col][m]) - np.nanmean(pro[col4][m] - pro[col][m]))
    return out


def train(years: list[str] | None = None, fraction: float = 1.0) -> tuple[StuffModel, pl.DataFrame]:
    """Fit the production model on every pitcher of ``years`` (verified games), then grade every pitch in
    both views. Returns (model, PitchUID -> grades + labels). ``fraction`` < 1 keeps a slice of pitchers
    (smoke tests). Peak memory is the targets + features step; every grading pass after it is chunked."""
    t0 = time.time()
    years = years or available_years()
    p4 = p4_hitters(years)
    _log(f"P4 hitters: {p4.height:,} hitter-seasons, {p4['P4h'].sum():,} P4", t0)
    raw = load_pitches(years)
    _log(f"loaded {raw.height:,} pitches, {years[0]}-{years[-1]}", t0)
    raw, merged = merge_pitcher_ids(raw)
    pid_map = raw.select("PitcherId", "pid").unique("PitcherId").sort("PitcherId")
    _log(f"pitcher ids merged: {merged}", t0)
    if fraction < 1:                         # smoke test: a slice of pitchers (tagging included)
        raw = raw.filter((pl.col("pid").hash(SEED) % 10_000) < int(fraction * 10_000))
    raw, templates = add_labels(raw)
    raw = raw.drop("pFB", "pBB", "pOS", "tag_group", strict=False)     # memory: nothing after tagging reads them
    _log("pitch groups tagged", t0)
    season = season_prices(years)
    # Memory (the old peak step, 13.0 GB on the server): targets and features one season at a time, keeping only
    # the slim columns grading and training read, so the full wide frame is never built. Every target and
    # feature is per season already (each season's RE288, the FB_1 reference per pitcher-season), so nothing changes.
    P_parts, T_parts = [], []
    for yr in sorted(raw["Year"].unique().to_list()):
        d = add_features(add_targets(raw.filter(pl.col("Year") == yr), season)).filter(pl.col("gradeable"))
        T_parts.append(d.filter(pl.col("is_verified"), pl.col("y").is_not_null(), pl.col("same").is_not_null()).select(TRAIN_COLS))
        P_parts.append(d.select(GRADE_COLS))
        del d
        _log(f"{yr}: targets + features", t0)
    del raw
    P, train_ = pl.concat(P_parts), pl.concat(T_parts)
    del P_parts, T_parts
    train_ = _assign_folds(train_.join(p4, on=["BatterId", "Year"], how="left").with_columns(pl.col("P4h").fill_null(False)))
    _log(f"{train_.height:,} training swings ({train_['P4h'].mean():.1%} by P4 hitters), {P.height:,} gradeable pitches", t0)
    train_, skills = skill_features(train_)
    _log(f"hitter skills: {skills}", t0)

    model = StuffModel()
    model.templates, model.pid_map = templates, pid_map
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
    Z1, Z4 = _mix(train_, pl.lit(True)), _mix(train_, pl.col("P4h"))
    n_swings, p4_share = train_.height, float(train_["P4h"].mean())
    del train_, dg                           # memory: grading needs only the boosters, the mixes and P
    model.points, model.pop = _prototypes(Z1, Z4)
    model.p4_fix = _p4_correction(model, P, Z1, Z4)
    _log(f"{N_PROTO} prototypes; P4-view correction (runs): " + ", ".join(f"{g} {model.p4_fix[g]['runs']:+.4f}" for g in GROUPS), t0)
    model.set_average_pieces(P)
    _log("average pieces set; grading every pitch against the prototypes", t0)
    parts = []                               # raw_grades' blocks, one log line each (grading is most of the run)
    for a in range(0, P.height, GRADE_CHUNK):
        parts.append(model._raw_grades_block(P.slice(a, GRADE_CHUNK)))
        _log(f"graded {min(a + GRADE_CHUNK, P.height):,} of {P.height:,} pitches", t0)
    raw_g = pl.concat(parts)
    del parts
    model.set_scale(P, raw_g)
    grades = model.apply_scale(raw_g, P)
    del raw_g
    _log(f"graded {grades.height:,} pitches (both views)", t0)
    model.meta = {**model.meta, "version": MODEL_VERSION, "level": LEVEL, "years": years, "fraction": fraction,
                  "n_swings": n_swings, "p4_swing_share": p4_share, "n_pitches": P.height, "trees": trees, **merged,
                  "skills": skills, "prototypes": N_PROTO, "reference": [REF_CELLS, REF_DRAWS, REF_FIT],
                  "p4_rule": f"team home-game conference (PAC-12 through {PAC12_LAST}), < {MIN_HOME} home games -> "
                             f">= {P4_SHARE:.0%} of swings in P4 games",
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
    if getattr(model, "pid_map", None) is not None:
        raw = _training_pids(raw, model.pid_map)
    raw, _ = add_labels(raw, model.templates, n_jobs=n_jobs)
    return add_features(raw).filter(pl.col("gradeable"))


def _training_pids(raw: pl.DataFrame, pid_map: pl.DataFrame) -> pl.DataFrame:
    """A one-season merge can group a few PitcherIds differently from training's all-season merge, which
    re-tags their pitches (the old 0.08% re-score gap). Ids the model was trained on keep their training
    pid; an id new to the model takes the training pid of a known id in the same one-season cluster, else
    keeps its one-season pid."""
    m = (raw.select("PitcherId", "pid").unique("PitcherId")
            .join(pid_map.rename({"pid": "pid_t"}), on="PitcherId", how="left"))
    cl = m.filter(pl.col("pid_t").is_not_null()).group_by("pid").agg(pid_c=pl.col("pid_t").mode().sort().first())
    m = m.join(cl, on="pid", how="left").select("PitcherId", pid_new=pl.coalesce("pid_t", "pid_c", "pid"))
    return (raw.join(m, on="PitcherId", how="left")
               .with_columns(pid=pl.coalesce("pid_new", "pid")).drop("pid_new"))


def score(raw: pl.DataFrame, model: StuffModel, n_jobs: int = -1) -> pl.DataFrame:
    """PitchUID -> group, retag, type, Stuff+, Whiff+, Foul+, Contact+, runs and the run breakdown.
    ``n_jobs=1`` for a few pitchers (the app's live pass); the default for whole seasons."""
    P = prepare(raw, model, n_jobs=n_jobs)
    return _with_labels(model.grade(P), P) if P.height else pl.DataFrame()
