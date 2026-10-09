"""raw/ pitch CSVs -> games/YYYY/<GameID>.parquet, one file per game, + ledger (plan.md §4-§6).

    python -m pipeline_v2.load                     # every game with a newly downloaded file
    python -m pipeline_v2.load --all               # reprocess every game from raw/ (after a rule change)
    python -m pipeline_v2.load --reload 20260502-SwellThomasStadium-1
    python -m pipeline_v2.load --reload-warned     # games loaded with new values / columns, after a fix-map update
    python -m pipeline_v2.load --retry-failed      # games with a file that failed its checks

Which file is a game's current version (plan.md §5): a verified file beats an unverified one; among
equals, the latest delivery wins. Files are tried best first, and the first one that passes its
checks becomes the game's file in games/; files ranked below it are 'superseded' without being
read. A file that fails, is empty or is held back doesn't stop the game: the next one down is tried,
so a bad re-delivery leaves the previous version in place.

Each file (plan.md §6):
    read every column as text -> cast to the 170-column schema (uncastable values become null and
    are counted) -> fix typos (fix_dictionary) and drop rows with impossible counts (counted)
  then
    fail it        unreadable; a key column missing or null; Top/Bottom not Top/Bottom; a duplicate
                   PitchUID; a GameID that isn't the file name's; more than half the rows with
                   impossible counts (a bullpen or practice session, not a game)
    'empty'        no pitches; never loaded and never replaces a game
    'held'         an unverified file with pitches sharing a position (Inning, Top/Bottom,
                   PAofInning, PitchofPA): extra tracking sessions mixed in; waits for the verified file
    load + warn    values outside the allowed lists, columns not in the schema (dropped here, kept in
                   raw/), and repeated positions in a verified file are recorded in the ledger
  and the loaded game gets the base-state columns (baserunner.py), is_verified and source_file.

When a game's file is replaced, the ledger records how many PitchUIDs carried over (plan.md §5).
Positioning and bat-tracking files are tracked in the ledger only (status 'tracked'), by the same rule.
Workers (one per CPU) read, check and write the game files; the parent records everything in the ledger.
"""
from __future__ import annotations

import os
os.environ.setdefault("POLARS_MAX_THREADS", "1")   # one thread per worker; the pool parallelizes games

import argparse
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import polars as pl

from pipeline_v2 import baserunner, config, fix_dictionary, ledger, notify
from pipeline_v2 import trackman_pandera_schema as allowed
from pipeline_v2.trackman_schema import pl_csv_schema as SCHEMA

log = logging.getLogger("load")

KEY_COLUMNS = ["PitchUID", "GameID", "Inning", "Outs", "Balls", "Strikes", "Top/Bottom"]
POSITION = ["Inning", "Top/Bottom", "PAofInning", "PitchofPA"]
ALLOWED = {                         # column -> (allowed values, may be empty)
    "PitcherThrows": (allowed.PITCHERTHROWS, True), "BatterSide": (allowed.BATTERSIDE, True),
    "CatcherThrows": (allowed.CATCHERTHROWS, True), "PitcherSet": (allowed.PITCHERSET, False),
    "TaggedPitchType": (allowed.TAGGEDPITCHTYPE, False), "AutoPitchType": (allowed.AUTOPITCHTYPE, True),
    "PitchCall": (allowed.PITCHCALL, True), "KorBB": (allowed.KORBB, False),
    "TaggedHitType": (allowed.TAGGEDHITTYPE, False), "PlayResult": (allowed.PLAYRESULT, False),
    "AutoHitType": (allowed.AUTOHITTYPE, True),
}
WARNED = ("unknown_values", "new_columns", "uncastable")   # what --reload-warned reloads for
NO_VERSION = "no version passes its checks"


class Reject(Exception):
    """A file that can't be loaded: ``status`` is 'failed', 'empty' or 'held'."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


# ── One file ──────────────────────────────────────────────────────────────────────────────────────
def transform(path: Path, game_id: str, verified: bool, source_file: str) -> tuple[pl.DataFrame, dict]:
    """One raw pitch CSV -> (the game's rows, ready to write; warnings). Raises Reject."""
    warnings: dict = {}
    try:
        raw = pl.read_csv(path, infer_schema_length=0, null_values=[""], encoding="utf8-lossy")
    except Exception as e:
        raise Reject("failed", f"unreadable: {type(e).__name__}: {e}"[:300])
    if raw.height == 0:
        raise Reject("empty", "no pitches")
    new_cols = [c for c in raw.columns if c not in SCHEMA]
    if new_cols:
        warnings["new_columns"] = new_cols
    missing_keys = [c for c in KEY_COLUMNS if c not in raw.columns]
    if missing_keys:
        raise Reject("failed", f"missing key column(s): {missing_keys}")

    # Cast every schema column, counting values that were there but couldn't be read as the type.
    typed = [(c, t) for c, t in SCHEMA.items() if c in raw.columns and t != pl.Utf8]
    lost = raw.select((pl.col(c).is_not_null() & pl.col(c).cast(t, strict=False).is_null()).sum().alias(c)
                      for c, t in typed).row(0, named=True) if typed else {}
    if any(lost.values()):
        warnings["uncastable"] = {c: n for c, n in lost.items() if n}
    df = raw.select(pl.col(c).cast(t, strict=False).alias(c) if c in raw.columns else pl.lit(None, dtype=t).alias(c)
                    for c, t in SCHEMA.items())

    rows_in = df.height
    df = fix_dictionary.apply_fixes(df)             # typos -> canonical values; impossible counts dropped
    rows_dropped = rows_in - df.height
    if rows_dropped * 2 > rows_in:                  # e.g. a bullpen session: one "at-bat" of 900+ pitches
        raise Reject("failed", f"{rows_dropped} of {rows_in} rows have impossible counts "
                               f"(PitchofPA, Outs, Balls or Strikes out of range): not a game")

    for c in KEY_COLUMNS:
        n = df[c].null_count()
        if n:
            raise Reject("failed", f"{c} is empty in {n} of {df.height} rows")
    bad_half = df.filter(~pl.col("Top/Bottom").is_in(allowed.TOP_BOTTOM))["Top/Bottom"].unique().to_list()
    if bad_half:
        raise Reject("failed", f"Top/Bottom values {bad_half[:5]}")
    dup = df.height - df["PitchUID"].n_unique()
    if dup:
        raise Reject("failed", f"{dup} duplicate PitchUID(s)")
    ids = df["GameID"].unique().to_list()
    if ids != [game_id]:
        raise Reject("failed", f"GameID column {ids[:3]} doesn't match the file name ({game_id})")

    repeated = df.height - df.unique(POSITION).height
    if repeated:
        if not verified:
            raise Reject("held", f"{repeated} of {df.height} pitches share a position with another pitch")
        warnings["repeated_positions"] = repeated

    unknown = {}
    for c, (values, nullable) in ALLOWED.items():
        bad = df.filter(~pl.col(c).is_in(values) | (pl.col(c).is_null() if not nullable else pl.lit(False)))
        if bad.height:
            unknown[c] = {str(v): n for v, n in bad[c].fill_null("(empty)").value_counts().iter_rows()}
    if unknown:
        warnings["unknown_values"] = unknown

    df = baserunner.annotate_game(df).with_columns(
        pl.lit(verified).alias("is_verified"), pl.lit(source_file).alias("source_file"))
    return df, {"rows": df.height, "rows_dropped": rows_dropped, "warnings": warnings}


def game_path(game_id: str) -> Path:
    return config.GAMES_DIR / game_id[:4] / f"{game_id}.parquet"


def write_atomic(df: pl.DataFrame, dest: Path) -> None:
    """Write next to ``dest`` under a name that doesn't end in .parquet (so nothing globbing the
    folder can pick up a half-written file), then rename over the old file in one step."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, dest)


# ── One game (runs in a worker) ───────────────────────────────────────────────────────────────────
def load_game(task: dict) -> dict:
    """Try the game's candidate files best first; write the first that passes. Returns what to record."""
    game_id, current, redo = task["game_id"], task["current"], task["redo"]
    out = {"game_id": game_id, "files": {}, "chosen": None, "changed": False}
    dest = game_path(game_id)
    for cand in task["candidates"]:
        fid = cand["file_id"]
        if not redo:
            if fid == current and dest.exists():
                out["chosen"] = fid                      # nothing better arrived: keep the current file
                break
            if fid != current and cand["status"] != "new":
                continue                                 # already decided on an earlier run
        try:
            df, info = transform(config.HERE / cand["path"], game_id, bool(cand["verified"]), cand["path"])
        except Reject as e:
            out["files"][fid] = {"status": e.status, "error": str(e)}
            if e.status == "failed":
                wlog.warning("%s: %s failed: %s", game_id, cand["path"], e)
            continue
        except Exception as e:                           # a bug or data nobody expected: flag this file only
            wlog.exception("%s: %s crashed", game_id, cand["path"])
            out["files"][fid] = {"status": "failed", "error": f"internal error: {type(e).__name__}: {e}"[:300]}
            continue
        if dest.exists() and fid != current:             # what the replacement does to PitchUIDs
            before = set(pl.read_parquet(dest, columns=["PitchUID"])["PitchUID"])
            after = set(df["PitchUID"])
            info["warnings"]["pitchuid_change"] = {"previous_file": current, "kept": len(before & after),
                                                   "added": len(after - before), "removed": len(before - after)}
        write_atomic(df, dest)
        out["files"][fid] = {"status": "loaded", **info}
        out["chosen"] = fid
        out["changed"] = True
        out["game"] = {"level": _first(df["Level"]), "game_uid": _first(df["GameUID"]),
                       "rows": df.height, "verified": int(cand["verified"])}
        break
    if out["chosen"] is None and (dest.exists() or current is not None):   # no version passes any more
        dest.unlink(missing_ok=True)                     # take the game out of games/ and the ledger
        out["changed"] = True
    # Below the chosen file, anything waiting (new, held) or the old version is now superseded.
    # Failed and empty files keep their status: it describes the file.
    seen = False
    for cand in task["candidates"]:
        if cand["file_id"] == out["chosen"]:
            seen = True
        elif seen and cand["file_id"] not in out["files"] and cand["status"] in ("new", "loaded", "held"):
            out["files"][cand["file_id"]] = {"status": "superseded"}
    return out


def _first(s: pl.Series):
    v = s.drop_nulls()
    return v[0] if len(v) else None


wlog = logging.getLogger("load.worker")


def _init_worker(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(Path(log_dir) / f"load-{os.getpid()}.log")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    wlog.addHandler(h)
    wlog.setLevel(logging.INFO)
    wlog.propagate = False


# ── Parent: choose the work, record the results ───────────────────────────────────────────────────
def ranked(files: list[dict]) -> list[dict]:
    """Best first: verified, then the latest delivery (upload folder, then modified time)."""
    return sorted(files, key=lambda f: (f["verified"] or 0, f["folder_date"], f["ftp_modify"], f["file_id"]), reverse=True)


def plan_tasks(con, args) -> list[dict]:
    files = [dict(r) for r in con.execute(
        "SELECT file_id, game_id, path, verified, status, folder_date, ftp_modify, warnings FROM files "
        "WHERE kind = 'pitches' AND path IS NOT NULL AND game_id IS NOT NULL AND status != 'duplicate'")]
    by_game = defaultdict(list)
    for f in files:
        by_game[f["game_id"]].append(f)
    current = dict(con.execute("SELECT game_id, current_file FROM games WHERE kind = 'pitches'").fetchall())
    by_id = {f["file_id"]: f for f in files}

    if args.all:
        targets, redo = set(by_game), True
    elif args.reload:
        targets, redo = set(args.reload) & set(by_game), True
        for g in set(args.reload) - set(by_game):
            log.warning("--reload %s: no pitch file in the ledger", g)
    elif args.reload_warned:
        warned = lambda f: f and f["warnings"] and any(k in json.loads(f["warnings"]) for k in WARNED)
        targets, redo = {g for g, fid in current.items() if warned(by_id.get(fid))}, True
    elif args.retry_failed:
        targets, redo = {f["game_id"] for f in files if f["status"] == "failed"}, True
    else:
        targets, redo = {f["game_id"] for f in files if f["status"] == "new"}, False
    tasks = [{"game_id": g, "current": current.get(g), "redo": redo,
              "candidates": ranked(by_game[g])} for g in sorted(targets)]
    return tasks[:args.limit] if args.limit else tasks


def record(con, res: dict, counts: Counter, problems: dict) -> None:
    now = ledger.now()
    with con:
        for fid, f in res["files"].items():
            con.execute("UPDATE files SET status = ?, error = ?, rows = ?, rows_dropped = ?, warnings = ? WHERE file_id = ?",
                        (f["status"], f.get("error"), f.get("rows"), f.get("rows_dropped"),
                         json.dumps(f["warnings"]) if f.get("warnings") else None, fid))
            counts[f"file {f['status']}"] += 1
            if f["status"] == "failed":
                problems["failed"].append((res["game_id"], f["error"]))
            for c, vals in (f.get("warnings") or {}).get("unknown_values", {}).items():
                for v, n in vals.items():
                    problems["values"][(c, v)][0] += n
                    problems["values"][(c, v)][1].add(res["game_id"])
            for c in (f.get("warnings") or {}).get("new_columns", []):
                problems["columns"][c].add(res["game_id"])
        if not res["changed"]:
            counts["game kept" if res["chosen"] else "game not loaded"] += 1
            return
        g = res.get("game")
        if g:
            counts["game written"] += 1
            con.execute("INSERT INTO games (game_id, kind, current_file, verified, level, year, game_uid, rows, updated_at, note) "
                        "VALUES (?, 'pitches', ?, ?, ?, ?, ?, ?, ?, NULL) ON CONFLICT (game_id, kind) DO UPDATE SET "
                        "current_file = excluded.current_file, verified = excluded.verified, level = excluded.level, "
                        "year = excluded.year, game_uid = excluded.game_uid, rows = excluded.rows, "
                        "updated_at = excluded.updated_at, note = CASE WHEN games.note = ? THEN NULL ELSE games.note END",
                        (res["game_id"], res["chosen"], g["verified"], g["level"], int(res["game_id"][:4]),
                         g["game_uid"], g["rows"], now, NO_VERSION))
        else:
            counts["game removed"] += 1
            con.execute("UPDATE games SET current_file = NULL, rows = NULL, updated_at = ?, note = ? "
                        "WHERE game_id = ? AND kind = 'pitches'", (now, NO_VERSION, res["game_id"]))


def track_other_kinds(con, redo: bool, counts: Counter) -> None:
    """Positioning and bat-tracking files: ledger only. The best file per game is 'tracked'."""
    rows = [dict(r) for r in con.execute(
        "SELECT file_id, game_id, kind, verified, status, folder_date, ftp_modify FROM files "
        "WHERE kind IN ('positioning', 'battracking') AND path IS NOT NULL AND game_id IS NOT NULL "
        "AND status IN ('new', 'tracked', 'superseded')")]
    groups = defaultdict(list)
    for r in rows:
        groups[(r["game_id"], r["kind"])].append(r)
    now = ledger.now()
    with con:
        for (game_id, kind), fs in groups.items():
            if not redo and not any(f["status"] == "new" for f in fs):
                continue
            best = ranked(fs)[0]
            for f in fs:
                status = "tracked" if f is best else "superseded"
                if f["status"] != status:
                    con.execute("UPDATE files SET status = ? WHERE file_id = ?", (status, f["file_id"]))
            con.execute("INSERT INTO games (game_id, kind, current_file, verified, year, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (game_id, kind) DO UPDATE SET current_file = excluded.current_file, "
                        "verified = excluded.verified, year = excluded.year, updated_at = excluded.updated_at "
                        "WHERE games.current_file IS NOT excluded.current_file",
                        (game_id, kind, best["file_id"], best["verified"], int(game_id[:4]), now))
            counts[f"tracked {kind}"] += 1


def alert(problems: dict, counts: Counter) -> None:
    """One Discord message when something needs a look: failed files, new values, new columns."""
    lines = []
    if problems["values"]:
        lines.append("**New values** (loaded and kept; add them to the allowed lists or the fix map, then `--reload-warned`):")
        for (c, v), (n, games) in sorted(problems["values"].items(), key=lambda kv: -kv[1][0])[:12]:
            lines.append(f"`{c}` = `{v}` ×{n:,} in {len(games)} game(s), e.g. {min(games)}")
    if problems["columns"]:
        lines.append("**New columns** (dropped from games/, kept in raw/):")
        lines += [f"`{c}` in {len(g)} game(s), e.g. {min(g)}" for c, g in sorted(problems["columns"].items())[:10]]
    if problems["failed"]:
        lines.append(f"**Failed files** ({len(problems['failed'])}):")
        lines += [f"{g}: {e[:90]}" for g, e in problems["failed"][:10]]
    if lines:
        notify.send("Load: needs a look", "\n".join(lines), "warn",
                    {k: f"{v:,}" for k, v in counts.items() if k.startswith(("game", "file"))})


def _setup_logging() -> Path:
    config.RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.RUN_LOG_DIR / f"{date.today()}.log"
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(), logging.FileHandler(log_path)],
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    return log_path


def run(args: argparse.Namespace) -> int:
    log_path = _setup_logging()
    con = ledger.connect()
    run_id = ledger.start_run(con, "load", vars(args), log_path)
    counts: Counter = Counter()
    problems = {"failed": [], "values": defaultdict(lambda: [0, set()]), "columns": defaultdict(set)}
    try:
        tasks = plan_tasks(con, args)
        log.info("%d games to load (%s)", len(tasks), "redo" if tasks and tasks[0]["redo"] else "new files only")
        t0 = time.time()
        workers = args.workers or os.cpu_count() or 1
        with ProcessPoolExecutor(workers, initializer=_init_worker,
                                 initargs=(str(config.WORKER_LOG_DIR / str(date.today())),)) as pool:
            for i, res in enumerate(pool.map(load_game, tasks, chunksize=20), 1):
                record(con, res, counts, problems)
                if i % 2000 == 0 or i == len(tasks):
                    log.info("loaded %d/%d games (%.0f/s) %s", i, len(tasks), i / max(time.time() - t0, 1e-6), dict(counts))
        track_other_kinds(con, redo=bool(args.all), counts=counts)
        ledger.finish_run(con, run_id, "ok", dict(counts))
        log.info("done: %s", dict(counts))
        for g, e in problems["failed"][:20]:
            log.warning("failed: %s: %s", g, e)
        for (c, v), (n, games) in sorted(problems["values"].items(), key=lambda kv: -kv[1][0])[:20]:
            log.warning("new value: %s = %r x%d in %d game(s), e.g. %s", c, v, n, len(games), min(games))
        for c, games in sorted(problems["columns"].items()):
            log.warning("new column: %s in %d game(s), e.g. %s", c, len(games), min(games))
        if args.notify:
            alert(problems, counts)
        return 0
    except BaseException as e:
        log.error("load failed: %s: %s", type(e).__name__, e)
        ledger.finish_run(con, run_id, "failed", dict(counts), error=f"{type(e).__name__}: {e}")
        if args.notify:
            notify.send("Load STOPPED", f"{type(e).__name__}: {e}", "error")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="raw/ pitch CSVs -> games/ + ledger")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="reprocess every game from raw/")
    which.add_argument("--reload", nargs="+", metavar="GAME_ID", help="reprocess these games")
    which.add_argument("--reload-warned", action="store_true", help="reprocess games loaded with new values/columns")
    which.add_argument("--retry-failed", action="store_true", help="reprocess games with a failed file")
    ap.add_argument("--workers", type=int, help="worker processes (default: one per CPU)")
    ap.add_argument("--limit", type=int, help="load at most N games (testing)")
    ap.add_argument("--notify", action="store_true", help="post failed files / new values / new columns to Discord")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
