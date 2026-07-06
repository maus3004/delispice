"""Orchestrate the Trackman pipeline. Each stage runs as its own subprocess, so a stage crash is
isolated + logged and each stage gets its own polars thread config.

  Nightly:         python run_pipeline.py
      1. pipeline_scaffold.py   data/*.csv      -> clean/         (+ CSV     -> data/cleaned/)
      2. baserunner_state.py    clean/*.parquet -> wbaserunners/  (+ parquet -> clean/processed/)
      3. height_scraper.py      wbaserunners/   -> heights.csv    (Baseball Reference heights for
                                players not yet in the table; capped per night, resumable)

  Month-end / bulk: python run_pipeline.py --monthly
      ... stages 1-2, then:
      3. compact.py             wbaserunners/ compacted IN PLACE (prior years -> yearly,
                                completed months -> monthly; in-progress month stays daily)
      4. re_matrix.py           wbaserunners/ -> re_matrices/re288_matrix.{parquet,csv}

The first BULK migration is just `run_pipeline.py --monthly` with every CSV sitting in data/. Each
run is incremental (only new files are processed). Stage events go to logs/run_pipeline.log and each
stage's full output to logs/<stage>.out; failures are logged with a stderr tail.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOGS = BASE / "logs"

NIGHTLY = ["pipeline_scaffold.py", "baserunner_state.py", "height_scraper.py"]
MONTHLY = ["compact.py", "re_matrix.py"]


def _logger() -> logging.Logger:
    LOGS.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(LOGS / "run_pipeline.log"), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)
    return logging.getLogger("run_pipeline")


def _run_stage(log: logging.Logger, script: str) -> bool:
    log.info("START  %s", script)
    t = time.time()
    r = subprocess.run([sys.executable, str(BASE / script)], cwd=str(BASE),
                       capture_output=True, text=True)
    out = (r.stdout or "")
    if r.stderr:
        out += "\n----- STDERR -----\n" + r.stderr
    (LOGS / f"{Path(script).stem}.out").write_text(out)
    dt = time.time() - t
    if r.returncode == 0:
        log.info("OK     %s (%.1fs)", script, dt)
        return True
    log.error("FAILED %s exit=%d (%.1fs); full output in logs/%s.out", script, r.returncode, dt,
              Path(script).stem)
    for line in (r.stderr or "").strip().splitlines()[-15:]:
        log.error("  | %s", line)
    return False


def main(monthly: bool) -> None:
    log = _logger()
    log.info("=== run_pipeline start (monthly=%s) ===", monthly)
    t0 = time.time()
    failed = [s for s in NIGHTLY + (MONTHLY if monthly else []) if not _run_stage(log, s)]
    log.info("=== run_pipeline done in %.1fs; failed stages: %s ===",
             time.time() - t0, ", ".join(failed) if failed else "none")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Trackman pipeline orchestrator")
    ap.add_argument("--monthly", action="store_true",
                    help="also compact wbaserunners in-place and rebuild the RE288 matrix "
                         "(month-end; and for the initial bulk migration)")
    main(ap.parse_args().monthly)
