"""What the pipeline is doing: ``python -m pipeline_v2 status`` (plan.md §9; phase 7 completes it).

Read-only: the ledger, serving/, heights.csv and the heights service's state file.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from datetime import datetime

from pipeline_v2 import config, heights, ledger


def _age(iso: str | None) -> str:
    if not iso:
        return "never"
    t = datetime.fromisoformat(iso)
    h = (datetime.now(t.tzinfo) - t).total_seconds() / 3600
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} days ago"


def _size_gb(path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e9 if path.exists() else 0.0


def report(heights_queue: bool = True) -> str:
    con = ledger.connect(readonly=True)
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    out = [f"pipeline_v2 status · {config.data_mode()}", ""]

    out.append("Runs (last of each job):")
    for job in ("run", "download", "load", "build"):
        r = q("SELECT started, finished, status, error, counts FROM runs WHERE job = ? ORDER BY run_id DESC LIMIT 1", job)
        if not r:
            out.append(f"  {job:9} never")
            continue
        started, finished, status, error, counts = r[0]
        took = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds() / 60 if finished else None
        line = f"  {job:9} {status:7} {_age(started):>12}" + (f", took {took:.0f} min" if took is not None else "")
        problems = json.loads(counts or "{}").get("problems") if job == "run" else None
        out.append(line + (f"  ERROR: {error[:160]}" if error else "") + (f"  ({len(problems)} problems)" if problems else ""))

    served = q("SELECT COALESCE(SUM(games), 0), COALESCE(SUM(rows), 0), COUNT(*) FROM serving")[0]
    held = {r[0] for r in q("SELECT held_game FROM clashes")}
    unverified = q("SELECT game_id FROM games WHERE kind = 'pitches' AND current_file IS NOT NULL AND verified = 0")
    out += ["", f"Served: {served[0]:,} games, {served[1]:,} pitches in {served[2]} year files; "
                f"{len(held)} games held back by clashes; {sum(1 for (g,) in unverified if g not in held):,} unverified games served"]
    files = Counter({s: n for s, n in q("SELECT status, COUNT(*) FROM files WHERE kind = 'pitches' GROUP BY status")})
    out.append("Pitch files: " + ", ".join(f"{n:,} {s}" for s, n in files.most_common()))
    newest = q("SELECT MAX(folder_date) FROM files")[0][0]
    out.append(f"Newest TrackMan upload folder: {newest}")

    out += ["", "Newest game per level:"]
    per_level: dict[str, str] = {}
    for gid, level in q("SELECT game_id, level FROM games WHERE kind = 'pitches' AND current_file IS NOT NULL"):
        if gid not in held and level and gid[:8] > per_level.get(level, ""):
            per_level[level] = gid[:8]
    for level, d in sorted(per_level.items(), key=lambda kv: kv[1], reverse=True):
        out.append(f"  {level:26} {d[:4]}-{d[4:6]}-{d[6:]}")

    failed = q("SELECT game_id, error FROM files WHERE kind = 'pitches' AND status = 'failed' ORDER BY file_id DESC LIMIT 8")
    out += ["", f"Failed pitch files: {files.get('failed', 0)}" + (" (newest first):" if failed else "")]
    out += [f"  {g}: {e[:120]}" for g, e in failed]

    values, columns = Counter(), Counter()
    for (w,) in q("SELECT f.warnings FROM games g JOIN files f ON f.file_id = g.current_file "
                  "WHERE g.kind = 'pitches' AND f.warnings IS NOT NULL"):
        w = json.loads(w)
        for col, vals in w.get("unknown_values", {}).items():
            for v, n in vals.items():
                values[f"{col} = {v}"] += n
        for col in w.get("new_columns", []):
            columns[col] += 1
    out.append(f"Values outside the allowed lists (in loaded games): {len(values)}" + (":" if values else ""))
    out += [f"  {k} ×{n:,}" for k, n in values.most_common(10)]
    out.append(f"New columns TrackMan added: {', '.join(f'{c} ({n} games)' for c, n in columns.most_common()) or 'none'}")

    out += ["", "Heights:"]
    table = heights.load_table()
    out.append(f"  heights.csv: {len(table):,} players (" + ", ".join(
        f"{n:,} {s}" for s, n in Counter(s for s, _ in table.values()).most_common()) + ")")
    st = heights.load_state()
    if st:
        out.append(f"  service: {st.get('state', '?')} (updated {_age(st.get('updated'))})"
                   + (f"; last block {_age(st.get('last_block'))}" if st.get("last_block") else "")
                   + (f"; last error: {str(st.get('last_error'))[:100]}" if st.get("last_error") else ""))
    else:
        out.append("  service: no state yet (not started on this machine)")
    if heights_queue:
        _, n_new, n_retry = heights.queue()
        out.append(f"  queue: {n_new:,} new players, {n_retry:,} retries due")

    du = shutil.disk_usage(config.HERE)
    raw = q("SELECT COALESCE(SUM(ftp_size), 0) FROM files WHERE path IS NOT NULL AND status != 'duplicate'")[0][0] / 1e9
    out += ["", f"Disk: {du.used / du.total:.0%} used, {du.free / 1e9:.0f} GB free"
                + (f"  ALERT: over {config.DISK_ALERT_PCT}%" if du.used / du.total * 100 > config.DISK_ALERT_PCT else ""),
            f"  raw/ {raw:.1f} GB, games/ {_size_gb(config.GAMES_DIR):.1f} GB, serving/ {_size_gb(config.SERVING_DIR):.1f} GB"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    print(report(heights_queue="--fast" not in (argv or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
