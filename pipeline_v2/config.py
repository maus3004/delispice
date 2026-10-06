"""Every path and setting shared by pipeline_v2 and the app, in one place (plan.md §3).

The app and backend/models import the "read side" paths below instead of hardcoding
``data_pipeline/...``, so moving the data is a one-file change. Until cutover (plan.md phase 5)
those point at today's locations, so importing this module changes nothing for the live app.

    from pipeline_v2 import config
    config.PITCHES_DIR      # what the app reads today: data_pipeline/wbaserunners
"""
from __future__ import annotations

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
# Phase 0: today's locations (no behavior change). Cutover flips the first three to the v2 values
# noted on the right.
_OLD = REPO / "data_pipeline"
PITCHES_DIR   = _OLD / "wbaserunners"                         # -> SERVING_PITCHES
RE288_PATH    = _OLD / "re_matrices" / "re288_matrix.parquet" # -> SERVING_RE288
HEIGHTS_CSV   = _OLD / "heights.csv"                          # -> HERE / "heights.csv"
TEAM_ACRONYMS = REPO / "backend" / "models" / "team_acronyms.csv"
ARTIFACTS_DIR = REPO / "backend" / "models" / "artifacts"
APP_STATE_DIR = REPO / "delispice_app" / "state"              # retags.json, autocluster.json (user data)

# ── FTP ───────────────────────────────────────────────────────────────────────────────────────────
FTP_HOST     = "ftp.trackmanbaseball.com"
FTP_ROOT     = "/v3"      # practice/ is skipped for now (plan.md §16)
RECHECK_DAYS = 7          # nightly: re-list the last N upload-date folders; full recheck on the 1st

# ── Schedule and tracking ─────────────────────────────────────────────────────────────────────────
SEASON_START    = (2, 1)  # (month, day): the zero-new-files check only runs inside this window
SEASON_END      = (8, 31)
WORKER_LOG_DAYS = 365
DISK_ALERT_PCT  = 80
APP_SERVICE     = "delispice"   # systemd unit the pipeline gracefully reloads (HUP to its MainPID)

# ── Heights ───────────────────────────────────────────────────────────────────────────────────────
HEIGHTS_RATE_S = 6.5      # seconds between Baseball Reference requests; never below ~4 (IP bans)
