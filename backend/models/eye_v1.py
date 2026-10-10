"""Batter EYE (v1) — swing/take decision value, in runs above the average decision.

Ported from ``backend/notebooks/eye_v1.ipynb``. This module is the MODEL (population loading,
feature prep, the three fits, scoring); ``eye_store.py`` handles artifacts + the training CLI, the
same split ``contact_quality.py`` / ``cq_store.py`` uses.

Eye scores the DECISION, not the outcome:

    eye = (swung - P(swing)) * (EV(swing) - EV(take))

which is the closed form of ``EV(chosen action) - EV(league's average action)``. Read it directly:
a batter gains when they swing MORE than the league would on a pitch where swinging is the better
call, or lay off where taking is. A swing is always valued at the LEAGUE-AVERAGE result of swinging
at that pitch — never the batter's own contact — which is what separates "eye" from "bat". A hitter
who chases a slider and doubles is still debited for the chase.

Three models, all LightGBM, all fit on one (Level, Year) population:

  * **umpire**  P(called strike | location, count, side). Feeds EV(take) against the count run
    values. Location-only ON PURPOSE — we want the NEUTRAL expected call, and adding pitch family
    measurably HURT held-out log loss (0.206 -> 0.212 on D1 2026): where a pitch crosses decides the
    call, not what it was.
  * **swing**   P(swing | location, count, side, family) — the league policy the batter is measured
    against. Family HELPS here (0.504 -> 0.479), because chasing is deception-driven. Same feature,
    opposite verdict, which is why the two models do not share a feature list.
  * **ev**      E[run value | swinging here] -> EV(swing). Regressed DIRECTLY on per-pitch run value
    rather than modelling whiff/foul/in-play separately: run value already encodes all three,
    including the 2-strike foul that is worth ~0 because the count does not change.

Sanity notes for anyone re-deriving these:
  * EV(swing) is negative almost everywhere in ABSOLUTE terms (~-0.004 even middle-middle, because
    whiffs and fouls drag down the in-play value). The metric lives entirely in EV(swing) vs
    EV(take), never in the sign of either alone.
  * The EV model's R^2 is ~0.02 and that is correct: one swing's run value is dominated by contact
    luck, which we deliberately refuse to model. It is a conditional-mean estimator, so judge it by
    calibration, not R^2.

Validation (D1, 2022-2026): year-over-year r ~= 0.55 on returning batters (>=200 pitches in both
seasons), stable across all four consecutive-season pairs — a real, persistent skill.

Known v1 limitation: HEIGHT-AGNOSTIC. The umpire model uses raw PlateLocHeight, so unusually tall or
short hitters carry a top/bottom-edge zone bias. v2 normalizes pz by the batter's own zone once the
height table lands; only pz normalizes, never px (plate width is fixed).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

from pipeline_v2 import config

PIPELINE = config.PITCHES_DIR
REM_PATH = config.RE288_PATH

# Outcome vocabulary. FoulBall is the legacy tag from older TrackMan CSVs; keep both spellings.
SWING_CALLS = ["StrikeSwinging", "InPlay", "FoulBall", "FoulBallFieldable", "FoulBallNotFieldable"]
TAKE_CALLS = ["BallCalled", "StrikeCalled", "BallinDirt"]
# Everything else (HitByPitch, BallIntentional, Automatic*, Undefined) is not a competitive
# swing/take decision, and is left UNSCORED rather than silently counted as a take.

# AutoPitchType -> family. Cutters ride with the breaking balls: in college they behave far more
# like a short slider than a fastball.
FAMILY = {"Four-Seam": "Fastball", "Sinker": "Fastball",
          "Slider": "Breaking", "Curveball": "Breaking", "Cutter": "Breaking",
          "Changeup": "Offspeed", "Splitter": "Offspeed"}
# Fixed category order so the codes LightGBM learns match the codes it is later scored with.
# "Unknown" is a real bucket (unlabeled pitches), not a missing-value marker.
FAM_CATS = ["Breaking", "Fastball", "Offspeed", "Unknown"]

# Feature lists differ per model on purpose — see the module docstring on pitch family.
UMP_FEATS = ["BatterSide", "Balls", "Strikes", "PlateLocHeight", "PlateLocSide"]
SWING_FEATS = ["BatterSide", "Balls", "Strikes", "PlateLocHeight", "PlateLocSide", "PitchFamily"]
EV_FEATS = ["BatterSide", "PitchFamily", "Balls", "Strikes", "PlateLocHeight", "PlateLocSide"]
MODEL_FEATS = {"ump": UMP_FEATS, "swing": SWING_FEATS, "ev": EV_FEATS}

# Plate-location sanity bounds (feet). The feed carries sentinel garbage — heights of -18 ft, sides
# of +/-10 ft — which would otherwise spend tree splits on physically impossible pitches.
MAX_SIDE, MIN_HEIGHT, MAX_HEIGHT = 3.0, 0.0, 5.5

# Tree counts settled by early stopping on a game-split validation set (games, never pitches: two
# pitches from one game share an umpire, so a random split leaks). Fixed here so a rebuild is
# deterministic and needs no holdout. The rest is the smooth-surface config — a lumpy probability
# surface invents borderline pitches that do not exist.
N_TREES = {"ump": 360, "swing": 650, "ev": 110}
LGB_PARAMS = dict(learning_rate=0.05, num_leaves=31, max_depth=6, min_child_samples=500,
                  n_jobs=-1, random_state=42, verbose=-1)

_PITCH_COLS = ["PitchUID", "BatterSide", "AutoPitchType", "Balls", "Strikes", "PitchCall",
               "PlateLocHeight", "PlateLocSide"]


# ── Population ───────────────────────────────────────────────────────────────────────────────────
def load_pitches(level: str, year: str) -> pl.DataFrame:
    """Every pitch for one (level, year).

    Scans BOTH physical partitions and filters on the scope column, rather than globbing
    ``{level}/{year}/`` — a level's rows are not confined to its own partition, and the pitches that
    sit under ``Others/`` would otherwise never be scored. P4 is a League-based pseudo-level with no
    partition of its own, so it scopes on League instead."""
    from backend.models import contact_quality as cq
    files = sorted(PIPELINE.glob(f"*/{year}/**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for year={year} under {PIPELINE}")
    scope_col = "League" if level == cq.P4_LEVEL else "Level"
    scope = (pl.col("League").is_in(cq.P4_LEAGUES) if level == cq.P4_LEVEL
             else pl.col("Level") == level)
    from backend.models import rv_store
    lf = pl.scan_parquet([str(f) for f in files])
    names = lf.collect_schema().names()
    state = [c for c in rv_store.ROW_STATE if c in names and c not in _PITCH_COLS]   # v2: run value from the row
    return (lf.select(cq.with_verified(lf, [*_PITCH_COLS, scope_col]) + state)
              .filter(scope).drop(scope_col).collect())


def load_re_matrix(level: str, year: str) -> pl.DataFrame:
    return (pl.read_parquet(REM_PATH)
              .filter((pl.col("Level") == level) & (pl.col("year") == year)))


def prepare(df: pl.DataFrame) -> pl.DataFrame:
    """Competitive decisions only, inside the plate bounds, with model-ready feature columns.

    Uniqued on PitchUID FIRST: the serving tree writes some pitches into both physical partitions,
    which would both double-weight training rows and fan out the PitchUID join downstream."""
    return (df.unique(subset=["PitchUID"], keep="first")
              .filter(pl.col("PitchCall").is_in(SWING_CALLS + TAKE_CALLS),
                      pl.col("BatterSide").is_in(["Right", "Left"]),
                      pl.col("PlateLocSide").abs() <= MAX_SIDE,
                      pl.col("PlateLocHeight") >= MIN_HEIGHT,
                      pl.col("PlateLocHeight") <= MAX_HEIGHT)
              .drop_nulls(["PitchUID", "PlateLocHeight", "PlateLocSide", "Balls", "Strikes"])
              .with_columns(
                  # 0 = R, 1 = L. Binary, so a plain integer splits identically to a category.
                  (pl.col("BatterSide") == "Left").cast(pl.Int8).alias("BatterSide"),
                  pl.col("AutoPitchType").fill_null("Unknown")
                    .replace_strict(FAMILY, default="Unknown")
                    .cast(pl.Enum(FAM_CATS)).alias("PitchFamily"),
                  pl.col("PitchCall").is_in(SWING_CALLS).cast(pl.Int8).alias("swung")))


def attach_run_value(df: pl.DataFrame, level: str, year: str) -> pl.DataFrame:
    """Add ``run_value`` — the realized change in run expectancy across each pitch.

    Delegates to ``rv_store``, which already precomputes the state transition (the "state after" is
    the NEXT pitch in the half-inning, so it cannot be derived from a filtered frame). Null for the
    ~1% of pitches in half-innings that never recorded three outs."""
    from backend.models import rv_store
    rv = rv_store.attach_rv(df, data_year=year, re_level=level, re_year=year)
    if rv is None:
        raise FileNotFoundError(f"rv state artifact missing for {year}; run: "
                                f"python -m backend.models.rv_store --years {year}")
    return df.with_columns(rv.alias("run_value"))


def load_training_frame(level: str, year: str) -> tuple[pl.DataFrame, pl.DataFrame, int | None]:
    """``(prepared pitches with run_value, re288 matrix, unverified pitches skipped)`` for one
    (level, year). Verified games only (plan.md §8); scoring (``eye_store.scorable_pitches``) still
    covers every pitch."""
    from backend.models import contact_quality as cq
    rem = load_re_matrix(level, year)
    if rem.height == 0:
        raise ValueError(f"re288_matrix has no rows for level={level} year={year}")
    # count_run_values looks up the state after every ball and strike, so a table missing a rare
    # state can't price the counts. No eye for that (level, year) rather than a borrowed value (plan.md §16).
    missing = 288 - rem["re288_state"].n_unique()
    if missing:
        raise cq.Refused(f"re288_matrix for {level} {year} lacks {missing} of 288 states; eye needs all of them")
    d, skipped = cq.keep_verified(prepare(load_pitches(level, year)))
    if d.height == 0:
        raise ValueError(f"no scorable pitches for level={level} year={year}")
    return attach_run_value(d, level, year), rem, skipped


# ── Count run values ─────────────────────────────────────────────────────────────────────────────
def count_run_values(rem: pl.DataFrame) -> pl.DataFrame:
    """The 12x2 table of what a called strike / ball is worth in each count, in runs.

    Each cell is the change in run expectancy caused by the pitch, averaged over base-out states and
    weighted by how often they actually occur (``n_obs``) — a flat mean would treat bases-loaded as
    being as common as bases-empty. The delta is taken BEFORE averaging, which is exact here because
    a called pitch moves no runners: the base-out state is identical on both sides of the subtraction.

    The two boundaries have no "next count" and are PA-ending events:
      * a strike on two strikes is a STRIKEOUT — same runners, one more out, fresh 0-0 (and RE=0 if
        that was the third out);
      * a ball on three balls is a WALK — batter to first, runners forced along only where the base
        behind them is occupied, a run scoring with the bases loaded.

    Sanity: 0-0 reproduces the textbook linear weights (strike ~= -0.083, ball ~= +0.072), and every
    cell matches the mean REALIZED run value of that event to ~0.005 runs — an independent check,
    since one number comes from matrix arithmetic and the other from telescoping actual games.
    """
    red = rem.to_pandas()
    RE = {(r.base_state, r.Outs, r.Balls, r.Strikes): r.run_expectancy for r in red.itertuples()}

    def re_at(base, outs, balls, strikes):
        return 0.0 if outs >= 3 else RE[(base, outs, balls, strikes)]

    # base_state is "1st|2nd|3rd", left to right (verified: "100" RE < "010" RE < "001" RE).
    walk = {"000": ("100", 0), "100": ("110", 0), "010": ("110", 0), "001": ("101", 0),
            "110": ("111", 0), "101": ("111", 0), "011": ("111", 0), "111": ("111", 1)}
    rows = []
    for r in red.itertuples():
        base, outs, b, s = r.base_state, r.Outs, r.Balls, r.Strikes
        re_now = r.run_expectancy
        re_strike = re_at(base, outs, b, s + 1) if s < 2 else re_at(base, outs + 1, 0, 0)
        if b < 3:
            re_ball = re_at(base, outs, b + 1, s)
        else:
            after, forced = walk[base]
            re_ball = re_at(after, outs, 0, 0) + forced
        rows.append((b, s, re_strike - re_now, re_ball - re_now, r.n_obs))
    t = pl.DataFrame(rows, schema=["Balls", "Strikes", "sd", "bd", "n"], orient="row")
    return (t.group_by(["Balls", "Strikes"])
             .agg(((pl.col("sd") * pl.col("n")).sum() / pl.col("n").sum()).alias("rv_strike"),
                  ((pl.col("bd") * pl.col("n")).sum() / pl.col("n").sum()).alias("rv_ball"))
             .sort(["Balls", "Strikes"]))


# ── Model ────────────────────────────────────────────────────────────────────────────────────────
class EyeModel:
    """The three boosters + the count run-value table, bundled so they can't be mismatched.

    All four pieces share one run environment: the count values come from a (Level, Year) matrix and
    the EV model is trained on run values computed in that same environment, so mixing a booster
    from one season with a count table from another would silently mis-value every decision."""

    def fit(self, d: pl.DataFrame, count_rv: pl.DataFrame):
        """``d`` is a prepared frame carrying ``swung`` and ``run_value``."""
        import lightgbm as lgb
        pdf = d.to_pandas()
        pdf["PitchFamily"] = pdf["PitchFamily"].astype("category")
        takes = pdf[pdf.swung == 0]
        # EV trains only on swings that HAVE a run value: incomplete half-innings score null, and
        # LightGBM would otherwise treat those NaNs as a target rather than a missing label.
        ev_train = pdf[(pdf.swung == 1) & pdf.run_value.notna()]
        if ev_train.empty:
            raise ValueError("no swings with run values — cannot fit EV(swing)")

        def _fit(kind, frame, y):
            Model = lgb.LGBMRegressor if kind == "ev" else lgb.LGBMClassifier
            obj = "regression" if kind == "ev" else "binary"
            m = Model(objective=obj, n_estimators=N_TREES[kind], **LGB_PARAMS)
            return m.fit(frame[MODEL_FEATS[kind]], y)

        self.ump = _fit("ump", takes, (takes.PitchCall == "StrikeCalled").astype(int))
        self.swing = _fit("swing", pdf, pdf.swung)
        self.ev = _fit("ev", ev_train, ev_train.run_value)
        self.count_rv = count_rv
        self.meta = {}
        return self

    def _frame(self, d: pl.DataFrame):
        pdf = d.to_pandas()
        pdf["PitchFamily"] = pdf["PitchFamily"].astype("category")
        return pdf

    def components(self, d: pl.DataFrame) -> pl.DataFrame:
        """``d`` + p_strike / p_swing / ev_swing / ev_take — the pieces behind an eye score.

        Exposed because a bare score is hard to argue with: a scout asking why a hitter graded out
        badly wants to see that the model called a pitch a 92% strike, not just the net number."""
        pdf = self._frame(d)
        return (d.with_columns(
                    pl.Series("p_strike", self.ump.predict_proba(pdf[UMP_FEATS])[:, 1]),
                    pl.Series("p_swing", self.swing.predict_proba(pdf[SWING_FEATS])[:, 1]),
                    pl.Series("ev_swing", self.ev.predict(pdf[EV_FEATS])))
                 .join(self.count_rv, on=["Balls", "Strikes"], how="left")
                 .with_columns((pl.col("p_strike") * pl.col("rv_strike")
                                + (1 - pl.col("p_strike")) * pl.col("rv_ball")).alias("ev_take")))

    def predict_eye(self, d: pl.DataFrame) -> pl.Series:
        """Eye score per row of a prepared frame, aligned to its rows."""
        c = self.components(d)
        return ((c["swung"] - c["p_swing"]) * (c["ev_swing"] - c["ev_take"])).rename("eye")

    def save(self, path):
        """Persist to ``{path}_{ump,swing,ev}.txt`` + ``{path}.json``.

        No pickle, for the same reason ``ContactQualityModel`` avoids it: LightGBM's native text
        dump survives library upgrades and can be read by a human, whereas a pickled sklearn wrapper
        silently rots across versions. The JSON carries the count run values and the feature lists,
        so a loaded model is self-describing."""
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        for kind in ("ump", "swing", "ev"):
            getattr(self, kind).booster_.save_model(str(base.with_name(f"{base.name}_{kind}.txt")))
        with open(base.with_name(base.name + ".json"), "w") as f:
            json.dump({"count_rv": self.count_rv.to_dicts(), "feats": MODEL_FEATS,
                       "fam_cats": FAM_CATS, "meta": self.meta}, f, indent=2)
        return base

    @classmethod
    def load(cls, path):
        """Rebuild a fitted model from its text dumps + JSON (no ``fit`` call)."""
        import lightgbm as lgb
        base = Path(path)
        model = cls()
        for kind in ("ump", "swing", "ev"):
            setattr(model, kind,
                    _BoosterShim(lgb.Booster(model_file=str(base.with_name(f"{base.name}_{kind}.txt")))))
        with open(base.with_name(base.name + ".json")) as f:
            cfg = json.load(f)
        model.count_rv = pl.DataFrame(cfg["count_rv"])
        model.feats, model.meta = cfg.get("feats", MODEL_FEATS), cfg.get("meta", {})
        return model


class _BoosterShim:
    """Gives a raw ``lgb.Booster`` the sklearn-wrapper surface the scoring path uses.

    A Booster loaded from text has ``predict`` but not ``predict_proba``/``booster_``, so without
    this a round-tripped model would not be callable by the same code that scores a freshly fitted
    one — the exact mismatch that makes save/load bugs show up only in production."""

    def __init__(self, booster):
        self.booster_ = booster

    def predict(self, X):
        return self.booster_.predict(X)

    def predict_proba(self, X):
        p = np.asarray(self.booster_.predict(X), dtype=float)
        return np.column_stack([1.0 - p, p])            # binary objective -> P(0), P(1)


def train(level: str, year: str) -> EyeModel:
    """Fit an EyeModel for one (level, year) from the pipeline parquets."""
    d, rem, skipped = load_training_frame(level, year)
    model = EyeModel().fit(d, count_run_values(rem))
    model.meta = {"level": level, "year": year, "n_rows": d.height,
                  "n_swings": int(d["swung"].sum()), "feats": MODEL_FEATS,
                  "verified_only": skipped is not None, "unverified_rows_skipped": skipped or 0}
    return model


def eye_scores(level: str, year: str) -> pl.DataFrame:
    """``PitchUID -> eye`` for a whole season — train (verified games) and score (every pitch) in one pass."""
    d, rem, _ = load_training_frame(level, year)
    model = EyeModel().fit(d, count_run_values(rem))
    every = prepare(load_pitches(level, year))
    return every.select("PitchUID").with_columns(model.predict_eye(every))
