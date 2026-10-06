"""Per-pitch RUN VALUE (RV) — state-transition artifacts + run-expectancy lookup.

RV is the realized change in run expectancy across a pitch:

    rv = RunsScored + RE(state after the pitch) - RE(state before the pitch)

Two independent halves, and keeping them apart is the whole design:

  * **The transition is a FACT about the game** — which ``re288_state`` the pitch started in, which
    state the next pitch started in, and how many runs crossed. That never changes. It is what these
    artifacts store, one file per YEAR, covering every level.

  * **The run environment is a CHOICE** — the RE288 matrix for some (Level, year), applied at read
    time. delispice_app's "Change run environment" picker chooses it.

Storing the finished ``rv`` number instead would bake one environment into the artifact, and then a
pitch simply would not exist in another environment's file: forcing a different YEAR blanked RV
entirely, and forcing a different LEVEL silently dropped to whichever subset of the player's pitches
happened to be in that level's population. Separating them makes every override work, and makes RV
behave like xRV — where the artifact holds the balls and the model is applied on top.

Why it must be precomputed at all: the "state after" is the state of the NEXT PITCH in the
half-inning, which almost always belongs to a different batter and often a different pitcher.
delispice_app scans only the selected player's rows, so a ``shift(-1)`` there would step to that
player's next plate appearance and produce nonsense.

Only COMPLETE half-innings (3 outs recorded) are stored — 99.0% of D1 2025 pitches. In a truncated
half-inning the last pitch has no next state, and treating that as RE=0 would invent a large negative
run value for a half-inning that merely ran out of data.

Sign is OFFENSE-POSITIVE for both roles, matching xRV: +1.41 for a home run, -0.33 for a strikeout.
On a pitcher report a negative RV is therefore the good one (runs prevented).

Build:  python -m backend.models.rv_store            # every year found on disk
        python -m backend.models.rv_store --years 2025 2026
"""
from __future__ import annotations

import argparse
import functools
from pathlib import Path

import polars as pl

from pipeline_v2 import config

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / "backend" / "models" / "artifacts"
PIPELINE = config.PITCHES_DIR
REM_PATH = config.RE288_PATH

HALF = ["GameUID", "Inning", "_tb"]
_COLS = ["PitchUID", "GameUID", "Inning", "Top/Bottom", "PAofInning", "PitchofPA",
         "re288_state", "RunsScored", "OutsOnPlay", "KorBB"]


def _state_path(year: str) -> Path:
    return ARTIFACT_DIR / f"rvstate_{year}.parquet"


def state_exists(year: str) -> bool:
    return _state_path(year).exists()


def build_state_cache(year: str) -> tuple[Path, int]:
    """Store ``PitchUID -> (state before, state after, RunsScored)`` for one season, ALL levels.

    Level-independent on purpose: the transition is a property of the game, so one file serves every
    level and every run-environment override. Games are read from both physical partitions and
    de-duplicated on PitchUID BEFORE ordering — a pitch present in two partitions would otherwise
    interleave into the half-inning sequence and corrupt the shift(-1) that defines "the next state".
    """
    files = sorted(PIPELINE.glob(f"*/{year}/**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for year={year} under {PIPELINE}")
    out = (pl.scan_parquet([str(f) for f in files])
             .select(_COLS)
             .unique(subset=["PitchUID"], keep="first")
             .with_columns(pl.when(pl.col("Top/Bottom") == "Top").then(0).otherwise(1).alias("_tb"))
             .sort(["GameUID", "Inning", "_tb", "PAofInning", "PitchofPA"], nulls_last=True)
             # Strikeouts carry OutsOnPlay=0 here, so outs must count them explicitly — the same
             # completeness rule re_matrix.py uses.
             .with_columns((pl.col("OutsOnPlay")
                            + (pl.col("KorBB") == "Strikeout").cast(pl.Int64)).alias("_om"))
             .with_columns(pl.col("_om").sum().over(HALF).alias("_tot"))
             .filter(pl.col("_tot") >= 3)
             .with_columns(pl.col("re288_state").shift(-1).over(HALF).alias("next_re288_state"))
             .select("PitchUID", "re288_state", "next_re288_state",
                     pl.col("RunsScored").fill_null(0).cast(pl.Int16))
             .collect())
    path = _state_path(year)
    from backend.models.cq_store import _atomic_write
    _atomic_write(out, path)
    return path, out.height


# maxsize=2, not one-per-season: a season's table is ~139 MB resident (36-char PitchUIDs dominate),
# so holding all five would sit on ~484 MB for the sake of a 116 ms reload. Two covers the usual
# "this season plus last" browsing; jumping further back just re-reads the parquet.
@functools.lru_cache(maxsize=2)
def state_lookup(year: str) -> pl.DataFrame | None:
    """PitchUID -> state transition for one season; None when the artifact isn't built."""
    path = _state_path(year)
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path).unique(subset=["PitchUID"])
    except Exception as e:                      # a truncated artifact must never break a report
        print(f"rv state cache {path.name} unreadable, RV will read blank: {e}")
        return None


@functools.lru_cache(maxsize=32)
def re_lookup(level: str, year: str) -> pl.DataFrame | None:
    """The 288-row run-expectancy table for one (Level, year) — the chosen run environment."""
    try:
        rem = pl.read_parquet(REM_PATH)
    except Exception:
        return None
    r = rem.filter((pl.col("Level") == level) & (pl.col("year") == year))
    return r.select("re288_state", "run_expectancy") if r.height else None


def attach_rv(df: pl.DataFrame, data_year: str, re_level: str, re_year: str) -> pl.Series | None:
    """Run value for ``df`` (needs a PitchUID column), aligned to ``df``'s rows.

    ``data_year`` selects the TRANSITION table (a fact about when the pitch was thrown);
    ``re_level``/``re_year`` select the RUN ENVIRONMENT it is valued in. Those are deliberately
    independent, which is what lets the app re-value a 2026 pitch against the 2022 D1 matrix.
    Returns None when either artifact is missing."""
    states = state_lookup(data_year)
    rem = re_lookup(re_level, re_year)
    if states is None or rem is None or df.height == 0 or "PitchUID" not in df.columns:
        return None
    j = (df.select("PitchUID")
           .join(states, on="PitchUID", how="left")
           .join(rem.rename({"re288_state": "_b", "run_expectancy": "_re_b"}),
                 left_on="re288_state", right_on="_b", how="left")
           .join(rem.rename({"re288_state": "_a", "run_expectancy": "_re_a"}),
                 left_on="next_re288_state", right_on="_a", how="left")
           # No next state = the half-inning ended, and a completed inning is worth 0 going forward.
           # Sound only because incomplete half-innings were dropped at build time.
           .with_columns(pl.col("_re_a").fill_null(0.0))
           .with_columns((pl.col("RunsScored") + pl.col("_re_a") - pl.col("_re_b")).alias("rv")))
    return j["rv"]


def available_years() -> list[str]:
    return sorted({p.name for p in PIPELINE.glob("*/[0-9][0-9][0-9][0-9]") if p.is_dir()})


def main(argv=None):
    p = argparse.ArgumentParser(description="Build per-pitch run-value state artifacts.")
    p.add_argument("--years", nargs="*", default=None)
    args = p.parse_args(argv)
    years = args.years or available_years()
    if not years:
        raise SystemExit("no seasons found on disk")
    for yr in years:
        print(f"building rv states {yr} …", flush=True)
        path, n = build_state_cache(yr)
        print(f"  saved {path.name} ({n:,} pitches)")


if __name__ == "__main__":
    main()
