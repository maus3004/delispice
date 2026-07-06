"""Add baserunner-state + RE288-state columns to the cleaned Trackman parquets.

Reads every parquet in clean/, reconstructs base occupancy pitch-by-pitch within each
half-inning, and writes new parquets to wbaserunners/{D1|Others}/YYYY/MM/DD/<name>.parquet.

New columns (occupancy is the state AT THE START of each pitch -- i.e. the RE288 input state):
  on_1b, on_2b, on_3b : Int8  (1 if that base is occupied when the pitch is thrown)
  base_state          : str   "abc" with a/b/c = 1B/2B/3B occupancy, e.g. "101" = 1st & 3rd
  re288_state         : str   "base|outs|balls-strikes", e.g. "101|1|3-2"  (8 bases x 3 outs x 12 counts = 288)

Advancement model (conservative + RunsScored top-off):
  - BB / HBP        : forced advance only.
  - Single/Double/Triple/HR : batter takes its bases; each runner advances the MINIMUM
                      (+1/+2/+3/score); then lead runners advance further only as needed to
                      satisfy RunsScored.
  - Error           : batter -> 1B, runners +1, then top-off to RunsScored.
  - Sacrifice       : batter out, runners +1, top-off.
  - Out             : batter out; extra OutsOnPlay remove the trailing (forced) runner (DP);
                      survivors hold; top-off to RunsScored (productive out).
  - FieldersChoice  : batter -> 1B; OutsOnPlay remove the lead forced (trailing) runner(s).
  - StolenBase      : advance the trailing runner one base (steal of home only if RunsScored).
  - CaughtStealing  : remove the trailing runner.

Steals/CS source: PlayResult labels when the file has any; otherwise (older files) a catcher-data
heuristic -- on a mid-PA pitch with ThrowSpeed present, OutsOnPlay>0 => CS, else => SB.

State resets every half-inning (Inning + Top/Bottom) -- to a GHOST RUNNER on 2nd in extra innings
(10th+), at every level except NCAA D1 in the postseason. Rows are processed in
Inning -> Top/Bottom -> PAofInning -> PitchofPA order and written in that order.
"""

from __future__ import annotations

import os
os.environ.setdefault("POLARS_MAX_THREADS", "1")   # 1 thread/process; pool parallelizes files

import logging
import multiprocessing as mp
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import polars as pl

log = logging.getLogger("baserunner")

# ---------------------------------------------------------------------------
# Baserunning model.  Bases are a set drawn from {1, 2, 3}.  Each helper returns
# (new_bases, runs_scored_by_model) -- runs are only used internally for top-off;
# the authoritative run count is the RunsScored column.
# ---------------------------------------------------------------------------

def _topoff(occ: set[int], runs: int, target: int) -> tuple[set[int], int]:
    """Advance lead runners one base at a time until runs == target (or none left)."""
    occ = set(occ)
    while runs < target and occ:
        lead = max(occ)            # highest base always moves into empty space above it
        occ.discard(lead)
        if lead + 1 >= 4:
            runs += 1
        else:
            occ.add(lead + 1)
    return occ, runs


def apply_hit(occ: set[int], hit_bases: int, runs_scored: int) -> tuple[set[int], int]:
    runs = 0
    moved = set()
    for b in sorted(occ, reverse=True):          # lead-to-trail avoids collisions
        if b + hit_bases >= 4:
            runs += 1
        else:
            moved.add(b + hit_bases)
    if hit_bases >= 4:                            # home run: batter scores
        runs += 1
    else:
        moved.add(hit_bases)                      # batter to its base
    return _topoff(moved, runs, runs_scored)


def apply_walk(occ: set[int]) -> tuple[set[int], int]:
    occ = set(occ); runs = 0
    if 1 in occ:                                  # batter forces the chain only through occupied bases
        if 2 in occ:
            if 3 in occ:
                runs += 1                          # bases loaded -> runner from 3rd forced home
            occ.add(3)
        occ.add(2)
    occ.add(1)
    return occ, runs


def apply_sacrifice(occ: set[int], runs_scored: int) -> tuple[set[int], int]:
    runs = 0; moved = set()
    for b in sorted(occ, reverse=True):
        if b + 1 >= 4:
            runs += 1
        else:
            moved.add(b + 1)
    return _topoff(moved, runs, runs_scored)      # batter is out -> not placed


def apply_out(occ: set[int], outs_on_play: int, runs_scored: int) -> tuple[set[int], int]:
    occ = set(occ)
    for _ in range(max(0, outs_on_play - 1)):     # batter is 1 out; extras (DP) take trailing runners
        if occ:
            occ.discard(min(occ))
    return _topoff(occ, 0, runs_scored)           # survivors hold unless a run is needed


def apply_fielders_choice(occ: set[int], outs_on_play: int, runs_scored: int) -> tuple[set[int], int]:
    occ = set(occ)
    for _ in range(max(1, outs_on_play)):         # lead forced (trailing) runner(s) out
        if occ:
            occ.discard(min(occ))
    occ.add(1)                                    # batter safe at 1B
    return _topoff(occ, 0, runs_scored)


def apply_steal(occ: set[int], runs_scored: int) -> tuple[set[int], int]:
    occ = set(occ); runs = 0
    if runs_scored > 0 and 3 in occ:              # steal of home
        occ.discard(3); runs += 1
        return occ, runs
    for b in sorted(occ):                         # trailing runner takes the next open base
        if b + 1 <= 3 and (b + 1) not in occ:
            occ.discard(b); occ.add(b + 1)
            break
    return occ, runs


def apply_caught_stealing(occ: set[int]) -> tuple[set[int], int]:
    occ = set(occ)
    if occ:
        occ.discard(min(occ))                     # trailing runner caught
    return occ, 0


# ---------------------------------------------------------------------------
# Per-game annotation
# ---------------------------------------------------------------------------
_HIT = {"Single": 1, "Double": 2, "Triple": 3, "HomeRun": 4}


def _apply_event(bases: set[int], r: dict, use_heuristic: bool) -> set[int]:
    pc, kb, pr = r["PitchCall"], r["KorBB"], r["PlayResult"]
    oop = r["OutsOnPlay"] or 0
    rs = r["RunsScored"] or 0
    mid_pa = pr == "Undefined" and kb == "Undefined" and pc != "HitByPitch"

    # 1) Baserunning event FIRST -- a steal/CS is in PlayResult and can co-occur with a
    #    batter K/BB on the same pitch (e.g. strikeout + stolen base).
    if pr == "StolenBase":
        bases = apply_steal(bases, rs)[0]
    elif pr == "CaughtStealing":
        bases = apply_caught_stealing(bases)[0]
    elif mid_pa and use_heuristic and r["ThrowSpeed"] is not None and bases:
        # older files w/o steal labels: out-on-play => CS, else => SB (PA still continuing)
        bases = apply_caught_stealing(bases)[0] if oop > 0 else apply_steal(bases, rs)[0]
    elif mid_pa and rs > 0:
        bases = _topoff(bases, 0, rs)[0]          # run with no labeled play (wild pitch / passed ball / balk)

    # 2) Batter outcome that ends the PA.
    if kb == "Walk" or pc == "HitByPitch":
        bases = apply_walk(bases)[0]
    elif kb == "Strikeout":
        pass                                      # batter out; any steal already applied above
    elif pr in _HIT:
        bases = apply_hit(bases, _HIT[pr], rs)[0]
    elif pr == "Error":
        bases = apply_hit(bases, 1, rs)[0]
    elif pr == "Sacrifice":
        bases = apply_sacrifice(bases, rs)[0]
    elif pr == "FieldersChoice":
        bases = apply_fielders_choice(bases, oop, rs)[0]
    elif pr == "Out":
        bases = apply_out(bases, oop, rs)[0]
    return bases


# ---------------------------------------------------------------------------
# Extra innings: ghost runner (automatic runner placed on 2nd to start each half-inning, from the
# 10th). Applied at every level EXCEPT NCAA D1 once the postseason starts -- regionals begin the
# Friday after Memorial Day (the last Monday of May), approximated below; adjust if a season differs.
# ---------------------------------------------------------------------------
EXTRA_INNING = 10


def d1_playoff_start(year: int) -> date:
    d = date(year, 5, 31)
    while d.weekday() != 0:                        # back up to Memorial Day (last Monday of May)
        d -= timedelta(days=1)
    return d + timedelta(days=4)                   # regionals begin that Friday


def _filename_date(filename: str) -> date | None:
    s = filename[:8]
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def _ghost_applies(level: str | None, game_date: date | None) -> bool:
    """Ghost runner on 2nd to start extra half-innings -- every level EXCEPT D1 in the postseason."""
    if level == "D1" and game_date is not None and game_date >= d1_playoff_start(game_date.year):
        return False
    return True


def annotate_game(df: pl.DataFrame, game_date: date | None = None, level: str | None = None) -> pl.DataFrame:
    df = df.with_columns(
        pl.when(pl.col("Top/Bottom") == "Top").then(0).otherwise(1).alias("_tb")
    ).sort(["Inning", "_tb", "PAofInning", "PitchofPA"], nulls_last=True)

    has_labels = df.select(
        pl.col("PlayResult").is_in(["StolenBase", "CaughtStealing"]).any()
    ).item()
    has_catcher = df.select(pl.col("ThrowSpeed").is_not_null().any()).item()
    use_heuristic = (not has_labels) and bool(has_catcher)
    if level is None:
        _lv = df["Level"].drop_nulls().head(1).to_list()
        level = _lv[0] if _lv else None
    ghost = _ghost_applies(level, game_date)

    rows = df.select(
        ["Inning", "_tb", "Outs", "Balls", "Strikes",
         "PitchCall", "KorBB", "PlayResult", "OutsOnPlay", "RunsScored", "ThrowSpeed"]
    ).to_dicts()

    on1, on2, on3, bstate, re288 = [], [], [], [], []
    cur_half = None
    bases: set[int] = set()
    for r in rows:
        half = (r["Inning"], r["_tb"])
        if half != cur_half:
            cur_half = half
            inn = r["Inning"]
            bases = {2} if (ghost and inn is not None and inn >= EXTRA_INNING) else set()
        b1, b2, b3 = int(1 in bases), int(2 in bases), int(3 in bases)
        on1.append(b1); on2.append(b2); on3.append(b3)
        bs = f"{b1}{b2}{b3}"
        bstate.append(bs)
        re288.append(f"{bs}|{r['Outs']}|{r['Balls']}-{r['Strikes']}")
        bases = _apply_event(bases, r, use_heuristic)

    return df.with_columns(
        pl.Series("on_1b", on1, dtype=pl.Int8),
        pl.Series("on_2b", on2, dtype=pl.Int8),
        pl.Series("on_3b", on3, dtype=pl.Int8),
        pl.Series("base_state", bstate, dtype=pl.Utf8),
        pl.Series("re288_state", re288, dtype=pl.Utf8),
    ).drop("_tb")


# ---------------------------------------------------------------------------
# IO / parallel driver
# ---------------------------------------------------------------------------

def output_path(out_root: Path, level_value: str | None, filename: str) -> Path:
    lvl = "D1" if level_value == "D1" else "Others"
    date = filename[:8]
    if len(date) == 8 and date.isdigit():
        sub = Path(lvl) / date[:4] / date[4:6] / date[6:8]
    else:
        sub = Path(lvl) / "unknown_date"
    return out_root / sub / filename


def process_file(in_path: str, out_root: str, processed_dir: str | None = None) -> tuple[str, bool, str]:
    """Annotate one clean parquet -> wbaserunners (path from the FILENAME date, so a 2025 game
    processed in 2026 still lands under .../2025/...). On success the clean parquet is moved into
    processed_dir so nightly runs only annotate the new files."""
    p = Path(in_path)
    try:
        df = pl.read_parquet(p)
        level = df["Level"].drop_nulls().head(1).to_list()
        level_val = level[0] if level else None
        df = annotate_game(df, game_date=_filename_date(p.name), level=level_val)
        out = output_path(Path(out_root), level_val, p.name)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".parquet.tmp")
        df.write_parquet(tmp)
        tmp.replace(out)
        if processed_dir:
            Path(processed_dir).mkdir(parents=True, exist_ok=True)
            os.replace(p, Path(processed_dir) / p.name)
        return (p.name, True, "")
    except Exception as exc:  # noqa: BLE001
        return (p.name, False, f"{type(exc).__name__}: {exc}")


def run(in_dir: Path, out_dir: Path, processed_dir: Path | None = None,
        max_workers: int | None = None, limit: int | None = None) -> tuple[int, int]:
    """Annotate every top-level parquet in in_dir (clean/) -> out_dir (wbaserunners/). Files already
    moved into processed_dir (clean/processed/) are skipped by the top-level glob. Returns (ok, failed)."""
    files = sorted(in_dir.glob("*.parquet"))
    if limit:
        files = files[:limit]
    if not files:
        log.info("baserunner: no new parquet files in %s", in_dir)
        return (0, 0)
    max_workers = max_workers or os.cpu_count()
    pd_str = str(processed_dir) if processed_dir else None
    log.info("baserunner: annotating %d files across %d workers -> %s", len(files), max_workers, out_dir)
    ok = bad = 0
    with ProcessPoolExecutor(max_workers=max_workers, mp_context=mp.get_context("spawn")) as ex:
        futs = {ex.submit(process_file, str(f), str(out_dir), pd_str): f for f in files}
        for fut in as_completed(futs):
            name, good, msg = fut.result()
            if good:
                ok += 1
            else:
                bad += 1
                log.error("baserunner FAILED %s: %s", name, msg)
    log.info("baserunner: done success=%d failed=%d", ok, bad)
    return (ok, bad)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    base = Path(__file__).resolve().parent
    run(base / "clean", base / "wbaserunners", processed_dir=base / "clean" / "processed")
