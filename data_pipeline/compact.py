"""Compact the wbaserunners parquets IN PLACE (single directory tree, no separate compact dir).

Tiering by today's calendar date:
  prior years   -> ONE file per (level, year)   {level}/{year}/{level}_{year}.parquet
  current year  -> ONE file per COMPLETED month  {level}/{year}/{month}/{level}_{year}_{month}.parquet
  current (in-progress) month & future          -> left as daily files {level}/{year}/{month}/{day}/*.parquet

Daily game files start with the YYYYMMDD date; compacted files start with the level prefix
(``d1_`` / ``others_``) -- that's how the two are told apart within the same tree.

A period is compacted only when daily files exist for it, so re-runs are idempotent and late /
backfilled files (e.g. a 2025 game arriving in 2026) get re-folded into the right partition. When a
year stops being the current year, its monthly files roll up into a single yearly file. Sources are
deleted only after a verified, atomic write, so no data is lost.

Idempotent; intended to be run ~monthly.  Usage:  python compact.py
"""
from __future__ import annotations

import glob
import logging
import os
from datetime import date
from pathlib import Path

import polars as pl

BASE = Path(__file__).resolve().parent
WB = BASE / "wbaserunners"
LEVELS = ["D1", "Others"]
log = logging.getLogger("compact")


def _norm(p) -> str:
    return os.path.normpath(str(p))


def _is_daily(path: str) -> bool:
    """Daily game files are named YYYYMMDD-...; compacted files start with the level prefix."""
    return Path(path).name[0].isdigit()


# ---- discovery -------------------------------------------------------------
def _all_parquets(level: str, year: str) -> list[str]:
    return glob.glob(str(WB / level / year / "**" / "*.parquet"), recursive=True)


def _daily_files(level: str, year: str) -> list[str]:
    return [f for f in _all_parquets(level, year) if _is_daily(f)]


def _compacted_files(level: str, year: str) -> list[str]:
    return [f for f in _all_parquets(level, year) if not _is_daily(f)]


def _month_of(daily_path: str) -> str:
    # WB/{level}/{year}/{MM}/{DD}/{game}.parquet  ->  MM
    return Path(daily_path).relative_to(WB).parts[2]


def _yearly_out(level: str, year: str) -> Path:
    return WB / level / year / f"{level.lower()}_{year}.parquet"


def _monthly_out(level: str, year: str, month: str) -> Path:
    return WB / level / year / month / f"{level.lower()}_{year}_{month}.parquet"


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


# ---- core ------------------------------------------------------------------
def compact_partition(out_path: Path, inputs: list[str]) -> int:
    """Merge all `inputs` parquets into one zstd file at out_path (atomic + row-count verified).

    Deletes every input other than out_path only after the write is verified and renamed in place.
    Streams scan->sink so even a whole year fits in memory."""
    inputs = sorted({_norm(i) for i in inputs}, key=os.path.basename)
    if not inputs:
        return 0
    n_src = pl.scan_parquet(inputs).select(pl.len()).collect().item()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.tmp")
    pl.scan_parquet(inputs).sink_parquet(tmp, compression="zstd", statistics=True)
    n_out = pl.scan_parquet(tmp).select(pl.len()).collect().item()
    if n_out != n_src:
        Path(tmp).unlink(missing_ok=True)
        raise RuntimeError(f"row mismatch for {out_path}: wrote {n_out}, expected {n_src}")
    os.replace(tmp, out_path)
    for i in inputs:
        if _norm(i) != _norm(out_path):
            os.remove(i)
    return n_src


def _ensure_yearly(level: str, year: str) -> int:
    daily = _daily_files(level, year)
    compacted = _compacted_files(level, year)
    out = _yearly_out(level, year)
    if not daily and [_norm(c) for c in compacted] == [_norm(out)]:
        return 0                                    # already a single yearly file, nothing pending
    inputs = daily + compacted
    if not inputs:
        return 0
    n = compact_partition(out, inputs)
    _prune_empty_dirs(WB / level / year)
    return n


def _ensure_monthly(level: str, year: str, cur_year: int, cur_month: int) -> int:
    daily = _daily_files(level, year)
    total = 0
    for m in sorted({_month_of(f) for f in daily}):
        if int(year) == cur_year and int(m) >= cur_month:
            continue                                # in-progress month or future -> leave daily
        m_daily = [f for f in daily if _month_of(f) == m]
        out = _monthly_out(level, year, m)
        inputs = m_daily + ([str(out)] if out.exists() else [])
        total += compact_partition(out, inputs)
        _prune_empty_dirs(WB / level / year / m)
    return total


def compact_all(today: date | None = None) -> list[tuple[str, str, int]]:
    today = today or date.today()
    cy, cm = today.year, today.month
    summary = []
    for level in LEVELS:
        ldir = WB / level
        if not ldir.exists():
            continue
        for year in sorted(p.name for p in ldir.iterdir() if p.is_dir() and p.name.isdigit()):
            if int(year) < cy:
                n = _ensure_yearly(level, year)
            elif int(year) == cy:
                n = _ensure_monthly(level, year, cy, cm)
            else:
                n = 0                               # future-dated dailies: leave as-is
            if n:
                summary.append((level, year, n))
                log.info("compacted %s %s: %d rows", level, year, n)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rows = compact_all()
    print(f"compacted {len(rows)} partitions, {sum(n for *_, n in rows):,} rows folded")
    for level, year, n in rows:
        print(f"  {level} {year}: {n:,} rows")
