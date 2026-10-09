"""Every path and setting shared by pipeline_v2 and the app, in one place (plan.md §3).

The app and backend/models import the "read side" paths below instead of hardcoding
``data_pipeline/...``, so moving the data is a one-file change. Until cutover (plan.md phase 5)
those point at today's locations, so importing this module changes nothing for the live app.

    from pipeline_v2 import config
    config.PITCHES_DIR      # what the app reads today: data_pipeline/wbaserunners

**Trying v2 before cutover.** Start a program with ``DELISPICE_DATA=v2`` and it reads v2's data,
RE288 matrix and model artifacts instead, with its own app cache. The setting belongs to each
running program, so the live app (started without it) and a v2 training run can share one checkout:

    DELISPICE_DATA=v2 .venv/bin/python -m backend.models.cq_store --level D1 --years 2025
    DELISPICE_DATA=v2 .venv/bin/python delispice_app/app.py

Anything other than exactly "v2", or no setting at all, means today's paths. Cutover makes v2 the
default. The pipeline's own scripts (download, load, build) always use the v2 folders above.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERE = REPO / "pipeline_v2"

# ── v2 data (all git-ignored) ─────────────────────────────────────────────────────────────────────
RAW_DIR         = HERE / "raw"                     # exact FTP copy, never moved: raw/YYYY/MM/DD/<name>
GAMES_DIR       = HERE / "games"                   # one parquet per game: games/YYYY/<GameID>.parquet
SERVING_DIR     = HERE / "serving"
SERVING_PITCHES = SERVING_DIR / "pitches"          # pitches/{D1,Others}/YYYY/<level>_YYYY.parquet
SERVING_RE288   = SERVING_DIR / "re288_matrix.parquet"
LEDGER_DB       = HERE / "pipeline.db"             # the ledger (SQLite): files, games, runs
RUN_LOG_DIR     = HERE / "logs" / "runs"           # one log per run, kept
WORKER_LOG_DIR  = HERE / "logs" / "workers"        # per-worker logs, deleted after WORKER_LOG_DAYS
SECRETS_ENV     = HERE / ".env"                    # FTP login + Discord webhook; never committed

# ── What the app and models read ──────────────────────────────────────────────────────────────────
# Today's locations unless the program was started with DELISPICE_DATA=v2 (see above). Heights move
# to HERE / "heights.csv" at cutover, not with the switch; user data is the same in both modes.
V2 = os.environ.get("DELISPICE_DATA") == "v2"
_OLD = REPO / "data_pipeline"
PITCHES_DIR   = SERVING_PITCHES      if V2 else _OLD / "wbaserunners"
RE288_PATH    = SERVING_RE288        if V2 else _OLD / "re_matrices" / "re288_matrix.parquet"
ARTIFACTS_DIR = HERE / "artifacts"   if V2 else REPO / "backend" / "models" / "artifacts"
APP_CACHE_DIR = REPO / "delispice_app" / (".cache_v2" if V2 else ".cache")   # derived, safe to delete
HEIGHTS_CSV   = _OLD / "heights.csv"
TEAM_ACRONYMS = REPO / "backend" / "models" / "team_acronyms.csv"
APP_STATE_DIR = REPO / "delispice_app" / "state"              # retags.json, autocluster.json (user data)


def data_mode() -> str:
    """One line saying which data this program reads, for start-up messages."""
    return (f"data: {'v2 (DELISPICE_DATA=v2)' if V2 else 'current (data_pipeline/)'}; "
            f"pitches {PITCHES_DIR.relative_to(REPO)}, RE288 {RE288_PATH.relative_to(REPO)}, "
            f"models {ARTIFACTS_DIR.relative_to(REPO)}")

# ── FTP ───────────────────────────────────────────────────────────────────────────────────────────
FTP_HOST          = "ftp.trackmanbaseball.com"
FTP_ROOT          = "/v3"   # practice/ is skipped for now (plan.md §16)
RECHECK_DAYS      = 7       # nightly: re-list upload-date folders from the last N days; full recheck on the 1st
BACKFILL_WORKERS  = 3       # FTP connections for --all (3 tested); nightly uses 1 (~0.7 s fixed cost per file)

# ── Schedule and tracking ─────────────────────────────────────────────────────────────────────────
SEASON_START    = (2, 1)  # (month, day): the zero-new-files check only runs inside this window
SEASON_END      = (8, 31)
WORKER_LOG_DAYS = 365
DISK_ALERT_PCT  = 80
APP_SERVICE     = "delispice"   # systemd unit the pipeline gracefully reloads (HUP to its MainPID)
NOTIFY_PROGRESS_S = 3600        # download --notify: a Discord progress message this often (plan.md §17)

# ── Baserunner model ──────────────────────────────────────────────────────────────────────────────
# Levels that start each extra half-inning (10th+) with a runner on 2nd (plan.md §18). Checked against
# the data with `python -m pipeline_v2.check_baserunner --ghost`; every other level plays it straight.
GHOST_RUNNER_LEVELS = {"NWL", "CPL", "Cape Cod Baseball League", "Cali Collegiate", "NECBL"}

# ── Heights ───────────────────────────────────────────────────────────────────────────────────────
HEIGHTS_RATE_S = 6.5      # seconds between Baseball Reference requests; never below ~4 (IP bans)


def secrets() -> dict[str, str]:
    """``KEY=value`` pairs from ``SECRETS_ENV`` (FTP_USER, FTP_PASS; later the Discord webhook).
    Never log or print these."""
    out = {}
    for line in SECRETS_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out
