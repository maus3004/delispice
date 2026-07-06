"""Compute RE288 run-expectancy matrices from the wbaserunners parquets, split by Level x Year.

RE288 state = base occupancy x outs x count (8 x 3 x 12 = 288).  For every pitch in a COMPLETE
half-inning we record runs_to_end = runs scored from that pitch to the end of the half-inning
(inclusive).  run_expectancy(state) = mean(runs_to_end) over all pitches in that state, computed
separately for each (Level, Year).

Completeness: a half-inning is kept only if 3 outs were recorded.  Outs are counted as
    OutsOnPlay + (KorBB == "Strikeout")
because strikeouts carry OutsOnPlay = 0 in this data (caught-stealing/pickoffs already set
OutsOnPlay).  This keeps ~99% of half-innings; the naive "max(Outs+OutsOnPlay) >= 3" test wrongly
drops strikeout-ending innings (~1/3 of them), which biases RE upward.

Extra innings (10th+) carry a ghost runner on 2nd (see baserunner_state.py). They are KEPT for D1 but
EXCLUDED here for every other level.

Output (one tidy long table -> re_matrices/re288_matrix.{parquet,csv}):
    Level, year, re288_state, base_state, Outs, Balls, Strikes, n_obs, run_expectancy
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serving

BASE = Path(__file__).resolve().parent
OUT = BASE / "re_matrices"

NEEDED = ["Level", "GameUID", "Inning", "Top/Bottom", "PAofInning", "PitchofPA",
          "base_state", "Outs", "Balls", "Strikes", "RunsScored", "OutsOnPlay", "KorBB"]
HALF = ["GameUID", "Inning", "_tb"]
KEYS = ["Level", "base_state", "Outs", "Balls", "Strikes"]


def year_matrix(year: str, paths: list[str]) -> pl.DataFrame:
    if not paths:
        return pl.DataFrame()
    lf = (
        pl.scan_parquet(paths)
        .select(NEEDED)
        .with_columns(pl.when(pl.col("Top/Bottom") == "Top").then(0).otherwise(1).alias("_tb"))
        .sort(["GameUID", "Inning", "_tb", "PAofInning", "PitchofPA"], nulls_last=True)
        .with_columns(
            (pl.col("OutsOnPlay") + (pl.col("KorBB") == "Strikeout").cast(pl.Int64)).alias("_outs_made")
        )
        .with_columns(
            pl.col("_outs_made").sum().over(HALF).alias("_tot_outs"),
            pl.col("RunsScored").cum_sum(reverse=True).over(HALF).alias("runs_to_end"),
        )
        .filter(pl.col("_tot_outs") >= 3)                      # complete half-innings only
        .filter(~((pl.col("Inning") >= 10) & (pl.col("Level") != "D1")))  # non-D1: drop extra innings (10th+)
        .group_by(KEYS)
        .agg(pl.len().alias("n_obs"), pl.col("runs_to_end").mean().alias("run_expectancy"))
        .with_columns(pl.lit(year).alias("year"))
    )
    return lf.collect()


def build() -> pl.DataFrame:
    files_by_year: dict[str, list[str]] = {}
    for f in serving.serving_files():
        _, y = serving.path_level_year(f)
        if y:
            files_by_year.setdefault(y, []).append(f)
    print("years:", sorted(files_by_year))
    parts = []
    for y in sorted(files_by_year):
        part = year_matrix(y, files_by_year[y])
        if part.height:
            parts.append(part)
            print(f"  {y}: {part.height} (Level,state) cells")
    matrix = pl.concat(parts)
    return (
        matrix.with_columns(
            (pl.col("base_state") + "|" + pl.col("Outs").cast(pl.Utf8) + "|"
             + pl.col("Balls").cast(pl.Utf8) + "-" + pl.col("Strikes").cast(pl.Utf8)).alias("re288_state")
        )
        .select(["Level", "year", "re288_state", "base_state", "Outs", "Balls", "Strikes",
                 "n_obs", "run_expectancy"])
        .sort(["Level", "year", "base_state", "Outs", "Balls", "Strikes"])
    )


def build_and_write() -> pl.DataFrame:
    """Build the RE288 matrix from the wbaserunners serving layer and write parquet + csv."""
    OUT.mkdir(parents=True, exist_ok=True)
    df = build()
    df.write_parquet(OUT / "re288_matrix.parquet")
    df.write_csv(OUT / "re288_matrix.csv")
    return df


if __name__ == "__main__":
    df = build_and_write()
    print(f"\nwrote {OUT/'re288_matrix.parquet'} and .csv  | rows={df.height}")
    print("groups (Level x year):", df.select(['Level', 'year']).unique().height)
    # quick validation: D1 leadoff RE by year (bases empty, 0 outs, 0-0 count)
    lead = (df.filter((pl.col("Level") == "D1") & (pl.col("re288_state") == "000|0|0-0"))
              .select(["year", "n_obs", "run_expectancy"]).sort("year"))
    print("\nD1 leadoff RE(000|0|0-0) by year:")
    print(lead)
