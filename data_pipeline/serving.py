"""All wbaserunners parquet files (daily + in-place compacted) -- one directory tree.

After compaction, `wbaserunners/` holds a mix of compacted files
  {level}/{year}/{level}_{year}.parquet              (prior years)
  {level}/{year}/{month}/{level}_{year}_{month}.parquet  (current-year completed months)
and uncompacted daily files
  {level}/{year}/{month}/{day}/{game}.parquet        (in-progress month / not yet compacted)
Compacted files are named with the level prefix; daily game files start with the YYYYMMDD date.
Readers use serving_files() and parse level/year from the path.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
WBASERUNNERS = BASE / "wbaserunners"

_LV_YR = re.compile(r"/wbaserunners/([^/]+)/(\d{4})/")


def path_level_year(path: str) -> tuple[str | None, str | None]:
    """Extract (level, year) from any wbaserunners path (daily or compacted)."""
    m = _LV_YR.search(path)
    return (m.group(1), m.group(2)) if m else (None, None)


def serving_files(level: str | None = None, year: str | None = None) -> list[str]:
    """Every parquet under wbaserunners/ (compacted + daily). Optionally filter by level/year."""
    files = glob.glob(str(WBASERUNNERS / "**" / "*.parquet"), recursive=True)
    if level is None and year is None:
        return files
    out = []
    for f in files:
        lv, yr = path_level_year(f)
        if level is not None and lv != level:
            continue
        if year is not None and yr != year:
            continue
        out.append(f)
    return out
