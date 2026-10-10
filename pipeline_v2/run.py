"""The nightly and monthly pipeline run: the one command cron starts (plan.md §6, §8).

    python -m pipeline_v2 run               # nightly; adds the monthly steps by itself on the 1st
    python -m pipeline_v2 run --monthly     # force the monthly steps
    python -m pipeline_v2 run --no-reload   # everything but the app reload (the Mac has no service)
    python -m pipeline_v2 warm              # just refresh the app's disk caches (after a manual load)

Nightly: download (upload folders of the last 7 days) -> load -> build (changed years) -> re-score
the changed level-years with the existing models -> refresh the app's disk caches -> graceful
reload -> delete old worker logs -> Discord.
Monthly (the 1st, or the next run when one was missed): download --all (full FTP recheck), build
--re288, and instead of re-scoring, retrain every level for the current season plus the past years
whose year files changed since the last monthly run.

Failures (plan.md §6): if download, load or build fails, the run stops. Nothing new was published
(build swaps serving/ in only at its very end), the app keeps yesterday's data, and the next night
retries. After build, serving/ is live: a re-score, retrain, refresh or reload problem is reported
and the run carries on, because the app scores pitches missing from the score tables itself.

Each step runs as its own process, so memory is freed between steps and a crash can't take the run
down. The pipeline steps write tonight's run log themselves; the model trainers' results are copied
into it here.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import polars as pl

from pipeline_v2 import config, ledger, notify

log = logging.getLogger("run")

TIMEOUT_S = {"download": 6 * 3600, "load": 2 * 3600, "build": 3600, "model": 2 * 3600, "warm": 1800}
MODEL_LINES = ("saved", "SKIPPED", "FAILED", "failed for")   # trainer output worth keeping in the log
MONTHLY_GAP_DAYS = 35            # no monthly run for this long (the 1st was missed): run it now


class StepFailed(Exception):
    pass


def step(name: str, module_args: list[str], timeout: int, keep_output: bool = False) -> None:
    """Run ``python -m <module_args>`` with the lock handed down. Raises StepFailed."""
    env = {**os.environ, ledger.LOCK_HELD_ENV: "1"}
    t0 = time.time()
    log.info("%s: starting (%s)", name, " ".join(module_args))
    try:
        p = subprocess.run([sys.executable, "-m", *module_args], cwd=config.REPO, env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StepFailed(f"{name}: still running after {timeout / 3600:.1f} h, stopped") from None
    lines = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
    if keep_output:
        for ln in lines:
            if any(k in ln for k in MODEL_LINES):
                log.info("  %s", ln.strip())
    if p.returncode != 0:
        raise StepFailed(f"{name} exited with {p.returncode}: " + " | ".join(lines[-4:])[-600:])
    log.info("%s: done in %.0f s", name, time.time() - t0)


def last_run(con, job: str, since: str) -> tuple[int | None, dict]:
    """(run_id, counts) of the newest ``job`` run that started after ``since``."""
    row = con.execute("SELECT run_id, counts FROM runs WHERE job = ? AND started >= ? ORDER BY run_id DESC LIMIT 1",
                      (job, since)).fetchone()
    return (row[0], json.loads(row[1] or "{}")) if row else (None, {})


def monthly_due(con, forced: bool) -> bool:
    if forced or date.today().day == 1:
        return True
    row = con.execute("SELECT MAX(started) FROM runs WHERE job = 'run' AND status = 'ok' "
                      "AND json_extract(args, '$.monthly')").fetchone()
    last = datetime.fromisoformat(row[0]) if row and row[0] else None
    return last is not None and datetime.now(last.tzinfo) - last > timedelta(days=MONTHLY_GAP_DAYS)


# ── Models ────────────────────────────────────────────────────────────────────────────────────────
def changed_level_years(con, build_run_id: int) -> dict[str, list[str]]:
    """Level -> years, for every level in the year files this build rebuilt (P4 rides with D1)."""
    out: dict[str, set[str]] = {}
    for part, year, path in con.execute("SELECT part, year, path FROM serving WHERE run_id = ?", (build_run_id,)):
        levels = pl.scan_parquet(config.HERE / path).select("Level").unique().collect()["Level"].drop_nulls().to_list()
        for lv in levels + (["P4"] if part == "D1" else []):
            out.setdefault(lv, set()).add(str(year))
    return {lv: sorted(ys) for lv, ys in sorted(out.items())}


def rescore(level_years: dict[str, list[str]], problems: list[str]) -> int:
    """Rebuild the xRV / eye score tables of the changed level-years from the models already trained."""
    from backend.models import cq_store, eye_store
    calls = 0
    for lv, years in level_years.items():
        for module, flag, exists in (("backend.models.cq_store", "--xrv-only", cq_store.exists),
                                     ("backend.models.eye_store", "--eye-only", eye_store.exists)):
            ys = [y for y in years if exists(lv, y)]
            if not ys:
                continue
            try:
                step(f"re-score {module.rsplit('.', 1)[1]} {lv} {' '.join(ys)}",
                     [module, "--level", lv, "--years", *ys, flag], TIMEOUT_S["model"], keep_output=True)
                calls += 1
            except StepFailed as e:
                problems.append(str(e))
    return calls


def retrain_scope(con, since: str | None) -> dict[str, list[str]]:
    """Level -> years to retrain: the current season, plus past years whose year files were rebuilt
    since the last monthly run; levels as in the RE288 matrix (built just before)."""
    years = sorted(p.name for p in config.SERVING_PITCHES.glob("*/*") if p.is_dir() and p.name.isdigit())
    scope = {years[-1]} if years else set()
    if since:
        scope |= {str(r[0]) for r in con.execute("SELECT DISTINCT year FROM serving WHERE built_at >= ?", (since,))}
    m = pl.read_parquet(config.SERVING_RE288, columns=["Level", "year"]).unique().filter(pl.col("year").is_in(sorted(scope)))
    out: dict[str, list[str]] = {}
    for lv, y in m.sort("Level", "year").iter_rows():
        out.setdefault(lv, []).append(y)
    return out


def retrain(level_years: dict[str, list[str]], problems: list[str]) -> int:
    calls = 0
    for lv, years in level_years.items():
        for module, flag in (("backend.models.cq_store", "--xrv"), ("backend.models.eye_store", "--eye")):
            try:
                step(f"retrain {module.rsplit('.', 1)[1]} {lv} {' '.join(years)}",
                     [module, "--level", lv, "--years", *years, flag], TIMEOUT_S["model"], keep_output=True)
                calls += 1
            except StepFailed as e:
                problems.append(str(e))
    return calls


# ── App ───────────────────────────────────────────────────────────────────────────────────────────
def warm() -> None:
    """Rebuild the picker indexes and drop the stale percentile / leaderboard pools (they rebuild on
    first use). What the app's old "Rebuild index" button did; runs in its own process."""
    from delispice_app import data, leaderboard
    for role in ("pitcher", "batter"):
        data.get_index(role, force_rebuild=True)
    data.clear_percentile_pools()
    leaderboard.clear_pools()


def reload_app() -> str:
    """Graceful reload: gunicorn starts a fresh worker and lets the old one finish (no downtime, and
    no sudo: the app runs as the same user as the pipeline)."""
    if not shutil.which("systemctl"):
        return "skipped: no systemctl on this machine"
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", config.APP_SERVICE],
                         capture_output=True, text=True).stdout.strip()
    if not pid.isdigit() or pid == "0":
        raise StepFailed(f"reload: the {config.APP_SERVICE} service isn't running")
    os.kill(int(pid), signal.SIGHUP)
    return f"sent HUP to {config.APP_SERVICE} (pid {pid})"


def clean_worker_logs() -> int:
    cutoff = date.today() - timedelta(days=config.WORKER_LOG_DAYS)
    removed = 0
    for d in config.WORKER_LOG_DIR.glob("*"):
        try:
            day = date.fromisoformat(d.name)
        except ValueError:
            continue
        if d.is_dir() and day < cutoff:
            shutil.rmtree(d)
            removed += 1
    return removed


# ── Discord ───────────────────────────────────────────────────────────────────────────────────────
def in_season(day: date) -> bool:
    return config.SEASON_START <= (day.month, day.day) <= config.SEASON_END


def report(title: str, counts: dict, problems: list[str], monthly: bool) -> None:
    """Problems always; otherwise one summary a night in season, and the monthly run's summary."""
    if not problems and not monthly and not in_season(date.today()):
        return
    lines = [f"⚠ {p}"[:300] for p in problems[:8]] or ["No problems."]
    notify.send(title, "\n".join(lines), "warn" if problems else "ok",
                {k: str(v) for k, v in counts.items() if k != "problems"})


# ── Run ───────────────────────────────────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> int:
    con = ledger.connect()
    started = ledger.now()
    monthly = monthly_due(con, args.monthly)
    last_monthly = con.execute("SELECT MAX(started) FROM runs WHERE job = 'run' AND status = 'ok' "
                               "AND json_extract(args, '$.monthly')").fetchone()[0]
    run_id = ledger.start_run(con, "run", {"monthly": monthly, "no_reload": args.no_reload}, args.log_path)
    kind = "Monthly run" if monthly else "Nightly run"
    counts: dict = {}
    problems: list[str] = []
    t0 = time.time()
    log.info("%s: started (%s)", kind, config.data_mode())
    try:
        # 1-3: download, load, build. A failure here stops the run with nothing new published.
        try:
            step("download", ["pipeline_v2.download", *(["--all"] if monthly else [])], TIMEOUT_S["download"])
            c = last_run(con, "download", started)[1]
            counts["new files"] = c.get("new", 0)
            step("load", ["pipeline_v2.load", "--notify"], TIMEOUT_S["load"])
            c = last_run(con, "load", started)[1]
            counts["games written"] = c.get("game written", 0)
            counts["files failed"] = c.get("file failed", 0)
            step("build", ["pipeline_v2.build", *(["--re288"] if monthly else [])], TIMEOUT_S["build"])
            build_id, c = last_run(con, "build", started)
            counts["year files rebuilt"] = c.get("year files built", 0)
        except StepFailed as e:
            log.error("%s stopped: %s", kind, e)
            counts["took"] = f"{(time.time() - t0) / 60:.0f} min"
            ledger.finish_run(con, run_id, "failed", counts, error=str(e))
            notify.send(f"{kind} FAILED", f"{e}\n\nNothing new was published; the site keeps yesterday's data. "
                        "The next run retries.", "error", {k: str(v) for k, v in counts.items()})
            return 1

        # 4: models. serving/ is live from here on, so problems are reported, not fatal.
        changed = changed_level_years(con, build_id) if build_id else {}
        if monthly:
            scope = retrain_scope(con, last_monthly)
            counts["models retrained"] = retrain(scope, problems)
        else:
            counts["score tables rebuilt"] = rescore(changed, problems)

        # 5-6: refresh the app, when anything it shows changed.
        if changed or monthly:
            try:
                step("warm", ["pipeline_v2", "warm"], TIMEOUT_S["warm"])
            except StepFailed as e:
                problems.append(str(e))
            if args.no_reload:
                counts["reload"] = "skipped (--no-reload)"
            else:
                try:
                    counts["reload"] = reload_app()
                except (StepFailed, OSError) as e:
                    problems.append(f"reload: {e}")
            log.info("app: %s", counts.get("reload", "reload failed"))
        else:
            counts["reload"] = "not needed (nothing changed)"

        counts["worker log folders removed"] = clean_worker_logs()
        counts["took"] = f"{(time.time() - t0) / 60:.0f} min"
        counts["problems"] = problems
        ledger.finish_run(con, run_id, "ok", counts)
        for p in problems:
            log.warning("problem: %s", p)
        log.info("%s: done in %s: %s", kind, counts["took"], {k: v for k, v in counts.items() if k != "problems"})
        report(f"{kind}: {counts['games written']} games, {counts['year files rebuilt']} year files"
               + (" (with problems)" if problems else ""), counts, problems, monthly)
        return 0
    except BaseException as e:                       # a bug in this file, or Ctrl-C / kill
        log.exception("%s crashed", kind)
        ledger.finish_run(con, run_id, "failed", counts, error=f"{type(e).__name__}: {e}")
        notify.send(f"{kind} CRASHED", f"{type(e).__name__}: {e}", "error")
        return 1


def _stop(*_):
    raise KeyboardInterrupt("SIGTERM")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The nightly / monthly pipeline run")
    ap.add_argument("--monthly", action="store_true", help="also do the monthly steps (automatic on the 1st)")
    ap.add_argument("--no-reload", action="store_true", help="don't reload the app (machines without the service)")
    args = ap.parse_args(argv)
    if not config.V2:
        print("run: refusing to run with DELISPICE_DATA=old (it would train into the old artifacts)", file=sys.stderr)
        return 2
    args.log_path = ledger.setup_logging()
    signal.signal(signal.SIGTERM, _stop)          # `kill`: record the run as failed, like Ctrl-C
    try:
        with ledger.pipeline_lock("run"):
            return run(args)
    except ledger.Busy as e:
        log.error("run: %s; not starting a second one", e)
        notify.send("Nightly run SKIPPED", f"{e}. Tonight's run didn't start.", "warn")
        return 2


def warm_main(argv: list[str] | None = None) -> int:
    t0 = time.time()
    warm()
    print(f"warm: picker indexes rebuilt, pools cleared ({time.time() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
