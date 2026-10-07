"""TrackMan FTP -> raw/ + ledger (plan.md §4, §8).

    python -m pipeline_v2.download                       # nightly: upload folders from the last 7 days
    python -m pipeline_v2.download --all                 # monthly full recheck / the first bulk download
    python -m pipeline_v2.download --since 2026-05-01 --until 2026-05-31
    python -m pipeline_v2.download --dry-run             # list what would download; changes nothing
    python -m pipeline_v2.download --all --notify        # also post start / hourly progress / end to Discord

Only /v3 is read (practice/ is skipped). A file is downloaded when its (remote path, size, modified
time) isn't in the ledger yet, so re-runs are cheap and a stopped run resumes where it left off. The
nightly window reaches further back than 7 days if the last successful run was longer ago.

Each file is written to <name>.part, checked against the listed size and hashed while it streams, then
renamed into raw/YYYY/MM/DD/ (its upload-date folder), so raw/ never holds a partial file. A copy
identical to one already stored (same name and sha256) isn't stored twice: its ledger row points at
the first copy, status 'duplicate'.

Failures (plan.md §6): connection problems (a dropped connection, a refused login, a lapsed session)
are retried, logging in again each time; a file that still can't be fetched stops the run (exit 1), and
the next run picks up from there. A file the server refuses (any other 5xx reply, e.g. 550) is recorded
as 'failed' and skipped.

TrackMan's FTP quirks (plan.md §2): self-signed certificate (verification off, as factory.sh did),
``OPTS MLST`` is rejected (so the default listing facts are used), and MLSD lists "." and ".." as
directories.
"""
from __future__ import annotations

import argparse
import ftplib
import hashlib
import logging
import os
import signal
import ssl
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from pipeline_v2 import config, ledger, names, notify

log = logging.getLogger("download")
RETRIES = 3


@dataclass(frozen=True)
class Remote:
    folder_date: date       # upload-date folder
    remote_path: str        # /v3/YYYY/MM/DD/CSV/<name>
    name: str
    size: int
    modify: str             # YYYYMMDDHHMMSS


# ── FTP ───────────────────────────────────────────────────────────────────────────────────────────
def connect() -> ftplib.FTP_TLS:
    """Log in. A refused login (e.g. 530 too many connections) is raised as a ConnectionError, so
    fetch() retries it like a dropped connection instead of blaming the file it was about to fetch."""
    s = config.secrets()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                  # TrackMan's certificate is self-signed
    ftp = ftplib.FTP_TLS(context=ctx, timeout=60)
    try:
        ftp.connect(config.FTP_HOST, 21)
        ftp.auth()
        ftp.login(s["FTP_USER"], s["FTP_PASS"])
        ftp.prot_p()                                 # encrypt the file transfers too
    except Exception as e:
        ftp.close()                                  # don't hold a half-open session while retrying
        if isinstance(e, ftplib.error_perm):
            raise ConnectionError(f"login refused: {e}") from e
        raise
    return ftp


def _ls(ftp: ftplib.FTP, path: str) -> list[tuple[str, dict]]:
    return [(n, f) for n, f in ftp.mlsd(path) if n not in (".", "..")]


def day_folders(ftp: ftplib.FTP, since: date, until: date) -> list[date]:
    """Upload-date folders under /v3 dated since..until (inclusive)."""
    out = []
    for y, f in _ls(ftp, config.FTP_ROOT):
        if f.get("type") != "dir" or not y.isdigit() or not since.year <= int(y) <= until.year:
            continue
        for m, f in _ls(ftp, f"{config.FTP_ROOT}/{y}"):
            if f.get("type") != "dir" or not m.isdigit():
                continue
            if not (since.year, since.month) <= (int(y), int(m)) <= (until.year, until.month):
                continue
            for d, f in _ls(ftp, f"{config.FTP_ROOT}/{y}/{m}"):
                if f.get("type") != "dir" or not d.isdigit():
                    continue
                try:
                    day = date(int(y), int(m), int(d))
                except ValueError:
                    continue
                if since <= day <= until:
                    out.append(day)
    return sorted(out)


def folder_files(ftp: ftplib.FTP, day: date) -> list[Remote]:
    """Every file in an upload-date folder. They all live in its CSV/ subfolder today; anything
    elsewhere is still downloaded but logged, so a new TrackMan layout doesn't go unnoticed."""
    base = f"{config.FTP_ROOT}/{day:%Y/%m/%d}"
    out = []
    for sub, f in _ls(ftp, base):
        if f.get("type") == "file":
            log.warning("file at day level: %s/%s", base, sub)
            out.append(Remote(day, f"{base}/{sub}", sub, int(f.get("size", -1)), f.get("modify", "")))
            continue
        if sub != "CSV":
            log.warning("unexpected subfolder: %s/%s", base, sub)
        for n, ff in _ls(ftp, f"{base}/{sub}"):
            if ff.get("type") == "file":
                out.append(Remote(day, f"{base}/{sub}/{n}", n, int(ff.get("size", -1)), ff.get("modify", "")))
    return out


# ── Worker side: one FTP connection per thread ────────────────────────────────────────────────────
_tls = threading.local()
_connections: list[ftplib.FTP] = []
_conn_lock = threading.Lock()


def _ftp() -> ftplib.FTP:
    if getattr(_tls, "ftp", None) is None:
        _tls.ftp = connect()
        with _conn_lock:
            _connections.append(_tls.ftp)
    return _tls.ftp


def _drop() -> None:
    ftp, _tls.ftp = getattr(_tls, "ftp", None), None
    if ftp is not None:
        try:
            ftp.close()
        except Exception:
            pass


def fetch(r: Remote) -> dict:
    """Download one file to <dest>.part, hashing as it streams. Runs in a worker thread; the main
    thread does the final rename and the ledger write."""
    dest = config.RAW_DIR / f"{r.folder_date:%Y/%m/%d}" / r.name
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        h, size = hashlib.sha256(), 0
        try:
            with open(part, "wb") as fh:
                def write(block: bytes) -> None:
                    nonlocal size
                    fh.write(block)
                    h.update(block)
                    size += len(block)
                _ftp().retrbinary(f"RETR {r.remote_path}", write)
            if size != r.size:
                raise OSError(f"got {size:,} bytes, the listing said {r.size:,}")
            return {"remote": r, "part": part, "dest": dest, "sha256": h.hexdigest()}
        except ftplib.error_perm as e:
            if not str(e).startswith("530"):         # 5xx: the server refuses this file; retrying won't help
                part.unlink(missing_ok=True)
                return {"remote": r, "error": str(e)}
            last = e                                 # 530 "not logged in": the session lapsed; log in again
            _drop()
            time.sleep(2 ** attempt)
        except (OSError, EOFError, ftplib.Error) as e:   # dropped connection, refused login, timeout, short read
            last = e
            _drop()
            time.sleep(2 ** attempt)
    part.unlink(missing_ok=True)
    raise ConnectionError(f"{r.remote_path}: {last}")


# ── Main thread: place the file and record it ─────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record(con, run_id: int, res: dict) -> str:
    r = res["remote"]
    p = names.parse(r.name)
    row = dict(remote_path=r.remote_path, ftp_size=r.size, ftp_modify=r.modify,
               folder_date=r.folder_date.isoformat(), name=r.name,
               game_id=p.game_id if p else None, kind=p.kind if p else "unknown",
               verified=int(p.verified) if p else None, run_id=run_id)
    if "error" in res:
        ledger.add_file(con, status="failed", error=res["error"], **row)
        return "failed"
    sha, part, dest = res["sha256"], res["part"], res["dest"]
    same = ledger.stored_copy(con, r.name, sha)
    if same is None and dest.exists() and _sha256(dest) == sha:     # on disk but not in the ledger
        same = {"path": ledger.rel(dest)}
    if same is not None:
        part.unlink()
        status, path = "duplicate", same["path"]
    else:
        if dest.exists():                            # same name in this folder, new contents: keep both
            dest = dest.with_name(f"{dest.stem}__{r.modify}{dest.suffix}")
        os.replace(part, dest)
        status, path = ("new" if p else "unknown"), ledger.rel(dest)
    ledger.add_file(con, status=status, path=path, sha256=sha, **row)
    return status


# ── Run ───────────────────────────────────────────────────────────────────────────────────────────
def _last_full_run(con) -> date | None:
    """Start of the last successful nightly or --all run (ranges and --limit runs don't count)."""
    row = con.execute(
        "SELECT MAX(started) FROM runs WHERE job = 'download' AND status = 'ok' "
        "AND json_extract(args, '$.since') IS NULL AND json_extract(args, '$.limit') IS NULL").fetchone()
    return date.fromisoformat(row[0][:10]) if row and row[0] else None


def window(con, args) -> tuple[date, date]:
    today = date.today()
    if args.all:
        return date(2000, 1, 1), today
    if args.since:
        return args.since, args.until or today
    start = today - timedelta(days=config.RECHECK_DAYS)
    last = _last_full_run(con) if con is not None else None
    if last is not None and last - timedelta(days=1) < start:
        start = last - timedelta(days=1)             # missed nights: reach back to the last good run
    return start, today


def _setup_logging(to_file: bool) -> Path | None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_path = None
    if to_file:
        config.RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = config.RUN_LOG_DIR / f"{date.today()}.log"
        handlers.append(logging.FileHandler(log_path))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    return log_path


def _hm(seconds: float) -> str:
    """3725 -> '1 h 02 m'."""
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60:02d} m" if m >= 60 else f"{m} m"


def _eta(elapsed: float, files_done: int, bytes_done: int, files_all: int, bytes_all: int) -> float:
    """Seconds left. Work is weighted by what it costs (plan.md §2: ~0.7 s per file, ~5 MB/s), so the
    large bat-tracking JSON near the end of the queue doesn't make the estimate optimistic."""
    work_done = files_done * 0.7 + bytes_done / 5e6
    work_all = files_all * 0.7 + bytes_all / 5e6
    return elapsed * (work_all - work_done) / max(work_done, 1e-6)


def _on_sigterm(signum, frame):
    raise SystemExit("killed (SIGTERM)")             # so `kill` records the run as failed and notifies


def run(args: argparse.Namespace) -> int:
    workers = args.workers or (config.BACKFILL_WORKERS if args.all else 1)
    log_path = _setup_logging(to_file=not args.dry_run)
    signal.signal(signal.SIGTERM, _on_sigterm)
    send = notify.send if args.notify and not args.dry_run else (lambda *a, **k: False)
    if args.dry_run:
        con = ledger.connect(readonly=True) if config.LEDGER_DB.exists() else None
    else:
        con = ledger.connect()
    since, until = window(con, args)
    what = "Bulk download" if args.all else f"Download {since}..{until}"
    run_id = None
    if not args.dry_run:
        run_id = ledger.start_run(con, "download", {"since": args.since, "until": args.until, "all": args.all,
                                                    "limit": args.limit, "workers": workers}, log_path)
    counts: Counter = Counter()
    pool, t0, failed = None, None, []
    files_all = bytes_all = files_done = bytes_done = 0
    try:
        ftp = connect()
        folders = day_folders(ftp, since, until)
        known = ledger.known_remote(con) if con is not None else set()
        todo: list[Remote] = []
        for i, day in enumerate(folders, 1):
            todo += [r for r in folder_files(ftp, day) if (r.remote_path, r.size, r.modify) not in known]
            if i % 100 == 0:
                log.info("listed %d/%d folders, %d new files so far", i, len(folders), len(todo))
        ftp.quit()
        counts["folders"], counts["listed_new"] = len(folders), len(todo)
        log.info("window %s..%s: %d upload folders, %d new files (%.2f GB)",
                 since, until, len(folders), len(todo), sum(r.size for r in todo) / 1e9)

        if args.dry_run:
            by_kind = Counter()
            for r in todo:
                p = names.parse(r.name)
                by_kind[(p.kind, "verified" if p.verified else "unverified") if p else ("unknown", "")] += 1
            for (kind, ver), n in sorted(by_kind.items()):
                print(f"  would download {n:>6,}  {kind} {ver}")
            return 0

        if args.limit:
            todo = todo[:args.limit]
        files_all, bytes_all = len(todo), sum(r.size for r in todo)
        send(f"{what} started", f"{files_all:,} files ({bytes_all / 1e9:.1f} GB) in {len(folders):,} upload folders",
             "info", {"Connections": workers})
        t0 = last_note = time.time()
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = [pool.submit(fetch, r) for r in todo]
        for fut in as_completed(futures):
            res = fut.result()                       # a ConnectionError here stops the run
            status = record(con, run_id, res)
            counts[status] += 1
            files_done += 1
            bytes_done += res["remote"].size
            if status == "failed":
                failed.append((res["remote"].name, res["error"]))
            if files_done % 250 == 0 or files_done == files_all:
                left = _eta(time.time() - t0, files_done, bytes_done, files_all, bytes_all)
                log.info("downloaded %d/%d (%.2f GB, ~%s left) %s", files_done, files_all, bytes_done / 1e9,
                         _hm(left), dict(counts))
            if time.time() - last_note >= config.NOTIFY_PROGRESS_S and files_done < files_all:
                last_note = time.time()
                left = _eta(last_note - t0, files_done, bytes_done, files_all, bytes_all)
                send(f"{what}: {files_done / files_all:.0%} of files",
                     f"{counts['new']:,} new · {counts['duplicate']:,} duplicates · {counts['failed']:,} failed",
                     "info", {"Files": f"{files_done:,} / {files_all:,}",
                              "GB": f"{bytes_done / 1e9:.1f} / {bytes_all / 1e9:.1f}", "Left": f"~{_hm(left)}"})
        pool.shutdown()
        ledger.finish_run(con, run_id, "ok", dict(counts))
        log.info("done: %s", dict(counts))
        msg = f"{files_done:,} files · {bytes_done / 1e9:.1f} GB in {_hm(time.time() - t0)}"
        if failed:                                   # listed so they can be checked; the run still counts as ok
            msg += "\n\nThe server refused:\n" + "\n".join(f"`{n}`: {err[:80]}" for n, err in failed[:15])
            if len(failed) > 15:
                msg += f"\n…and {len(failed) - 15:,} more (ledger rows with status 'failed')"
        send(f"{what} finished", msg, "warn" if failed else "ok",
             {"New": f"{counts['new']:,}", "Duplicates": f"{counts['duplicate']:,}",
              "Failed": f"{counts['failed']:,}", "Unknown names": f"{counts['unknown']:,}"})
        return 0
    except BaseException as e:                       # includes Ctrl-C and kill: record the run as failed
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        log.error("download failed: %s", reason)
        if run_id is not None:
            ledger.finish_run(con, run_id, "failed", dict(counts), error=reason)
        where = ("while listing the FTP folders" if t0 is None else
                 f"after {files_done:,} / {files_all:,} files ({bytes_done / 1e9:.1f} GB)")
        send(f"{what} STOPPED", f"{reason}\nStopped {where}. Re-run the same command to resume.", "error")
        return 1
    finally:
        for c in _connections:
            try:
                c.quit()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TrackMan FTP -> raw/ + ledger")
    ap.add_argument("--all", action="store_true", help="every upload folder (monthly recheck / first bulk download)")
    ap.add_argument("--since", type=date.fromisoformat, help="first upload-date folder, YYYY-MM-DD")
    ap.add_argument("--until", type=date.fromisoformat, help="last upload-date folder (default today)")
    ap.add_argument("--workers", type=int, help=f"FTP connections (default 1, or {config.BACKFILL_WORKERS} with --all)")
    ap.add_argument("--limit", type=int, help="download at most N files (testing)")
    ap.add_argument("--dry-run", action="store_true", help="list what would download; change nothing")
    ap.add_argument("--notify", action="store_true", help="post start / hourly progress / stopped / finished to Discord")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
