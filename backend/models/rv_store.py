"""Per-pitch RUN VALUE (RV) — each row's own state transition + run-expectancy lookup.

RV is the realized change in run expectancy across a pitch:

    rv = RunsScored + RE(state after the pitch) - RE(state before the pitch)

Two independent halves, and keeping them apart is the whole design:

  * **The transition is a FACT about the game** — which ``re288_state`` the pitch started in, which
    state the next pitch started in (``next_re288_state``), and how many runs crossed. That never
    changes. ``pipeline_v2/load.py`` writes it into every pitch row while it walks each game.

  * **The run environment is a CHOICE** — the RE288 matrix for some (Level, year), applied at read
    time. delispice_app's "Change run environment" picker chooses it.

Storing the finished ``rv`` number instead would bake one environment into the data, and then a
pitch simply would not exist in another environment: forcing a different YEAR blanked RV entirely,
and forcing a different LEVEL silently dropped to whichever subset of the player's pitches happened
to be in that level's population. Separating them makes every override work, and means a new RE288
matrix never requires rewriting the pitch data (plan.md §8).

Why the state after must come from the pipeline: it is the state of the NEXT PITCH in the
half-inning, which almost always belongs to a different batter and often a different pitcher.
delispice_app scans only the selected player's rows, so a ``shift(-1)`` there would step to that
player's next plate appearance and produce nonsense.

RV is blank unless the half-inning recorded three outs (``half_complete``, 99.0% of D1 2025 pitches).
In a truncated half-inning the last pitch has no next state, and treating that as RE=0 would invent
a large negative run value for a half-inning that merely ran out of data.

Sign is OFFENSE-POSITIVE for both roles, matching xRV: +1.41 for a home run, -0.33 for a strikeout.
On a pitcher report a negative RV is therefore the good one (runs prevented).
"""
from __future__ import annotations

import functools

import polars as pl

from pipeline_v2 import config

REM_PATH = config.RE288_PATH

ROW_STATE = ["re288_state", "next_re288_state", "half_complete", "RunsScored"]


@functools.lru_cache(maxsize=32)
def re_lookup(level: str, year: str) -> pl.DataFrame | None:
    """The 288-row run-expectancy table for one (Level, year) — the chosen run environment."""
    try:
        rem = pl.read_parquet(REM_PATH)
    except Exception:
        return None
    r = rem.filter((pl.col("Level") == level) & (pl.col("year") == year))
    return r.select("re288_state", "run_expectancy") if r.height else None


def attach_rv(df: pl.DataFrame, re_level: str, re_year: str) -> pl.Series | None:
    """Run value for ``df`` (which must carry ``ROW_STATE``), aligned to ``df``'s rows.

    ``re_level``/``re_year`` select the RUN ENVIRONMENT it is valued in, independently of the
    pitches' own level and year, which is what lets the app re-value a 2026 pitch against the 2022
    D1 matrix. Null where the half-inning never recorded three outs. Returns None when that
    (Level, year) has no matrix."""
    rem = re_lookup(re_level, re_year)
    if rem is None or df.height == 0:
        return None
    j = (df.select(ROW_STATE)
           .join(rem.rename({"re288_state": "_b", "run_expectancy": "_re_b"}),
                 left_on="re288_state", right_on="_b", how="left")
           .join(rem.rename({"re288_state": "_a", "run_expectancy": "_re_a"}),
                 left_on="next_re288_state", right_on="_a", how="left")
           # No next state = the half-inning ended, and a completed inning is worth 0 going forward.
           # Sound only for complete half-innings, hence the half_complete guard.
           .with_columns(pl.col("_re_a").fill_null(0.0))
           .with_columns(pl.when(pl.col("half_complete"))
                           .then(pl.col("RunsScored").fill_null(0) + pl.col("_re_a") - pl.col("_re_b"))
                           .alias("rv")))
    return j["rv"]
