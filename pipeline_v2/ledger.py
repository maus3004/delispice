"""The ledger: one SQLite file recording every file, every game's current version, and every run
(plan.md §4). A file's folder never encodes its status; this does.

    files   one row per downloaded copy (remote path + size + modified time), its local path,
            sha256, parsed name and status
    games   one row per (GameID, kind): which file is current, verified or not (filled from phase 2)
    runs    one row per job run: when, how it ended, counts
    clashes games held out of serving/ because they share PitchUIDs with a game that stays (build.py)
    serving one row per serving/ year file: what it was built from, so unchanged years are skipped

Paths are stored relative to pipeline_v2/ (``config.HERE``), so raw/ + pipeline.db can be copied
between machines as-is. WAL mode lets ``status`` and the app read while a run writes.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from pipeline_v2 import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id    INTEGER PRIMARY KEY,
    job       TEXT NOT NULL,                     -- download | load | build | models | run
    args      TEXT,                              -- json
    started   TEXT NOT NULL,                     -- UTC, ISO 8601
    finished  TEXT,
    status    TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed
    counts    TEXT,                              -- json summary
    error     TEXT,
    log_path  TEXT
);

CREATE TABLE IF NOT EXISTS files (
    file_id      INTEGER PRIMARY KEY,
    remote_path  TEXT NOT NULL,      -- /v3/2026/05/02/CSV/<name>
    ftp_size     INTEGER NOT NULL,
    ftp_modify   TEXT NOT NULL,      -- YYYYMMDDHHMMSS, from the FTP listing
    folder_date  TEXT NOT NULL,      -- upload-date folder, YYYY-MM-DD
    name         TEXT NOT NULL,
    path         TEXT,               -- local copy, relative to pipeline_v2/ (NULL if the download failed)
    sha256       TEXT,
    game_id      TEXT,               -- NULL when the name doesn't parse
    kind         TEXT NOT NULL,      -- pitches | positioning | battracking | unknown
    verified     INTEGER,            -- 1 / 0; NULL for unknown
    status       TEXT NOT NULL,      -- new | duplicate | loaded | superseded | tracked | failed | unknown | empty | held
    error        TEXT,
    rows         INTEGER,
    rows_dropped INTEGER,
    warnings     TEXT,               -- json: new category values, new columns, uncastable counts
    first_seen   TEXT NOT NULL,
    run_id       INTEGER REFERENCES runs(run_id),
    UNIQUE (remote_path, ftp_size, ftp_modify)
);
CREATE INDEX IF NOT EXISTS files_game   ON files (game_id, kind);
CREATE INDEX IF NOT EXISTS files_status ON files (status);
CREATE INDEX IF NOT EXISTS files_copy   ON files (name, sha256);

CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    current_file INTEGER REFERENCES files(file_id),
    verified     INTEGER,
    level        TEXT,
    year         INTEGER,
    game_uid     TEXT,
    rows         INTEGER,
    updated_at   TEXT,
    excluded     INTEGER NOT NULL DEFAULT 0,   -- set by hand to keep a game out of serving/
    note         TEXT,
    PRIMARY KEY (game_id, kind)
);

CREATE TABLE IF NOT EXISTS clashes (                -- rewritten by every build
    held_game  TEXT NOT NULL,                       -- left out of serving/
    kept_game  TEXT NOT NULL,                       -- the served game it shares PitchUIDs with
    shared     INTEGER NOT NULL,                    -- PitchUIDs in common
    rule       TEXT NOT NULL,                       -- why kept_game won
    run_id     INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (held_game, kept_game)
);

CREATE TABLE IF NOT EXISTS serving (
    part        TEXT NOT NULL,                      -- D1 | Others
    year        INTEGER NOT NULL,
    path        TEXT NOT NULL,                      -- relative to pipeline_v2/
    fingerprint TEXT NOT NULL,                      -- hash of its games, their files and when each was written
    games       INTEGER NOT NULL,
    rows        INTEGER NOT NULL,
    built_at    TEXT NOT NULL,
    run_id      INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (part, year)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = config.LEDGER_DB, readonly: bool = False) -> sqlite3.Connection:
    """Open the ledger (creating it on first use). ``readonly`` opens an existing file without
    creating or changing anything, for dry runs and ``status``."""
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def rel(path: Path) -> str:
    """Local path -> the form stored in the ledger (relative to pipeline_v2/)."""
    return path.resolve().relative_to(config.HERE).as_posix()


def absolute(stored: str) -> Path:
    return config.HERE / stored


# ── one job at a time ─────────────────────────────────────────────────────────────────────────────
LOCK_HELD_ENV = "PIPELINE_V2_LOCK_HELD"   # set by run.py for the steps it starts: they share its lock


class Busy(Exception):
    """Another pipeline job holds the lock."""


@contextmanager
def pipeline_lock(job: str):
    """Hold the pipeline lock for a whole job, or raise Busy at once if another job has it (plan.md
    §3: one schedule, one lock). The OS releases it when the process exits, even after a crash."""
    if os.environ.get(LOCK_HELD_ENV) == "1":
        yield
        return
    f = open(config.RUN_LOCK, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip()
        f.close()
        raise Busy(f"another pipeline job is running ({holder or 'unknown'})") from None
    try:
        f.truncate(0)
        f.write(f"{job}, pid {os.getpid()}, since {now()}\n")
        f.flush()
        yield
    finally:
        f.close()


# ── runs ──────────────────────────────────────────────────────────────────────────────────────────
def setup_logging() -> Path:
    """Log to the console and to tonight's run log, logs/runs/YYYY-MM-DD.log (every job of a night
    appends to the same file). Returns its path, for ``start_run``."""
    config.RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.RUN_LOG_DIR / f"{date.today()}.log"
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(), logging.FileHandler(log_path)],
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    return log_path


def start_run(con: sqlite3.Connection, job: str, args: dict | None = None,
              log_path: Path | None = None) -> int:
    with con:
        cur = con.execute("INSERT INTO runs (job, args, started, log_path) VALUES (?, ?, ?, ?)",
                          (job, json.dumps(args or {}, default=str), now(),
                           rel(log_path) if log_path else None))
    return cur.lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, status: str, counts: dict | None = None,
               error: str | None = None) -> None:
    with con:
        con.execute("UPDATE runs SET finished = ?, status = ?, counts = ?, error = ? WHERE run_id = ?",
                    (now(), status, json.dumps(counts or {}), error, run_id))


def last_ok_run(con: sqlite3.Connection, job: str) -> datetime | None:
    row = con.execute("SELECT MAX(started) FROM runs WHERE job = ? AND status = 'ok'", (job,)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


# ── files ─────────────────────────────────────────────────────────────────────────────────────────
def known_remote(con: sqlite3.Connection) -> set[tuple[str, int, str]]:
    """Every (remote_path, size, modified) already handled, so unchanged FTP files are skipped."""
    return {(r[0], r[1], r[2]) for r in con.execute("SELECT remote_path, ftp_size, ftp_modify FROM files")}


def stored_copy(con: sqlite3.Connection, name: str, sha256: str) -> sqlite3.Row | None:
    """An already-stored file with the same name and contents (an identical re-delivery)."""
    return con.execute("SELECT file_id, path FROM files WHERE name = ? AND sha256 = ? AND path IS NOT NULL "
                       "ORDER BY file_id LIMIT 1", (name, sha256)).fetchone()


def add_file(con: sqlite3.Connection, **row) -> int:
    row.setdefault("first_seen", now())
    cols = ", ".join(row)
    with con:
        cur = con.execute(f"INSERT INTO files ({cols}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))
    return cur.lastrowid
