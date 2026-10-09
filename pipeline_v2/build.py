"""games/ -> serving/, the only data the app reads (plan.md §3-§5, §8).

    python -m pipeline_v2.build             # rebuild the year files whose games changed
    python -m pipeline_v2.build --all       # rebuild every year file
    python -m pipeline_v2.build --re288     # also rebuild the RE288 matrix (monthly, with retraining)

serving/pitches/{D1,Others}/YYYY/{d1,others}_YYYY.parquet: one file per level group and year, the
same layout as today's compacted wbaserunners/, so the app's globs stay as they are (plan.md §4).

One pitch, one served game (plan.md §5). A PitchUID may appear in only one served game. Games that
share PitchUIDs are ranked: verified before unverified; among verified games, the most recently
delivered; among unverified ones, the one with more pitches (the one that contains the other), then
the most recent. Going down that ranking, a game that shares a PitchUID with one already kept is
held back. Every held game is recorded in the ledger's ``clashes`` table with the game it lost to.
Games set ``excluded`` in the ledger by hand are left out before any of this.

Only what changed is rebuilt: each year file has a fingerprint in the ledger (its games, their
current files and when each was last written), and a file is rebuilt when that changes (a game
added, replaced, reloaded, held back or excluded) or the file is missing. A level group and year
left with no games has its file removed.

Nothing partial is published (plan.md §6): every file is first written under a name that doesn't
end in .parquet; only when all of them (and RE288) are written are they renamed into place, each in
one step, and the ledger updated. If anything fails, the temporary files are deleted and serving/
is exactly as it was.

RE288 (plan.md §8): run expectancy per (Level, year) plus P4, from verified served games and
complete half-innings only. Extra innings count at every level: the ghost runner is now placed only
where it's used (plan.md §18), so those states are real. Same columns as the old matrix.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import polars as pl

from pipeline_v2 import config, ledger

log = logging.getLogger("build")

PARTS = ("D1", "Others")
P4_LEAGUES = ["SEC", "ACC", "BIG10", "BIG12"]       # Power-4 conferences, pooled as Level "P4"
RE_COLUMNS = ["Level", "year", "re288_state", "base_state", "Outs", "Balls", "Strikes", "n_obs", "run_expectancy"]


def game_file(game_id: str) -> Path:
    return config.GAMES_DIR / game_id[:4] / f"{game_id}.parquet"


def serving_file(part: str, year: int) -> Path:
    return config.SERVING_PITCHES / part / str(year) / f"{part.lower()}_{year}.parquet"


def stage(lf: pl.LazyFrame, dest: Path) -> tuple[Path, int]:
    """Stream ``lf`` into a temporary file next to ``dest`` (without holding it in memory); returns
    (temporary file, rows). ``run`` renames every staged file into place only once all succeeded."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    lf.sink_parquet(tmp)
    return tmp, pl.scan_parquet(tmp).select(pl.len()).collect().item()


# ── Which games are served ────────────────────────────────────────────────────────────────────────
def candidates(con) -> list[dict]:
    """Games with a loaded pitch file that nobody excluded by hand, and whose file is on disk."""
    rows = [dict(r) for r in con.execute(
        "SELECT g.game_id, g.current_file, g.verified, g.level, g.year, g.rows, g.updated_at, "
        "f.folder_date, f.ftp_modify FROM games g JOIN files f ON f.file_id = g.current_file "
        "WHERE g.kind = 'pitches' AND g.excluded = 0")]
    missing = {r["game_id"] for r in rows if not game_file(r["game_id"]).exists()}
    if missing:
        log.warning("%d games are in the ledger but not in games/ (rerun load): %s", len(missing), sorted(missing)[:5])
    return [r for r in rows if r["game_id"] not in missing]


def rank(g: dict) -> tuple:
    """Higher wins a clash: verified first; then, for verified games, the latest delivery; for
    unverified ones, more pitches, then the latest delivery."""
    delivery = (g["folder_date"], g["ftp_modify"])
    return (g["verified"], delivery if g["verified"] else ("", ""), 0 if g["verified"] else g["rows"], delivery)


def resolve_clashes(games: list[dict]) -> list[tuple[str, str, int, str]]:
    """(held game, kept game, PitchUIDs shared, rule) for every game held back."""
    uids = (pl.scan_parquet([game_file(g["game_id"]) for g in games]).select("PitchUID", "GameID")
              .unique().collect())
    shared = uids.filter(pl.col("PitchUID").is_duplicated())
    pairs = (shared.join(shared, on="PitchUID", suffix="_b").filter(pl.col("GameID") < pl.col("GameID_b"))
                   .group_by("GameID", "GameID_b").agg(pl.len().alias("n")))
    overlap: dict[str, dict[str, int]] = defaultdict(dict)
    for a, b, n in pairs.iter_rows():
        overlap[a][b] = overlap[b][a] = n
    by_id = {g["game_id"]: g for g in games}
    kept: set[str] = set()
    out = []
    for gid in sorted(overlap, key=lambda x: rank(by_id[x]), reverse=True):
        winners = {o: n for o, n in overlap[gid].items() if o in kept}
        if not winners:
            kept.add(gid)
            continue
        for o, n in winners.items():
            vk, vh = by_id[o]["verified"], by_id[gid]["verified"]
            rule = ("verified beats unverified" if vk and not vh else
                    "more recent verified delivery" if vk else "unverified game with more pitches")
            out.append((gid, o, n, rule))
    return out


def fingerprint(games: list[dict]) -> str:
    h = hashlib.sha1()
    for g in sorted(games, key=lambda g: g["game_id"]):
        h.update(f"{g['game_id']}|{g['current_file']}|{g['updated_at']}\n".encode())
    return h.hexdigest()


# ── RE288 ─────────────────────────────────────────────────────────────────────────────────────────
def re288(paths: list[Path]) -> pl.DataFrame:
    """Mean runs from each state to the end of its half-inning, per (Level, year) and P4."""
    base = (pl.scan_parquet(paths)
              .select("GameID", "Level", "League", "Inning", "Top/Bottom", "base_state", "Outs", "Balls",
                      "Strikes", "RunsScored", "half_complete")
              .filter(pl.col("half_complete"))
              .with_columns(pl.col("RunsScored").fill_null(0).cum_sum(reverse=True)
                              .over("GameID", "Inning", "Top/Bottom").alias("runs_to_end"),
                            pl.col("GameID").str.slice(0, 4).alias("year")))
    keys = ["Level", "year", "base_state", "Outs", "Balls", "Strikes"]
    agg = lambda lf: lf.group_by(keys).agg(pl.len().alias("n_obs"), pl.col("runs_to_end").mean().alias("run_expectancy"))
    per_level, p4 = pl.collect_all([
        agg(base),
        agg(base.filter((pl.col("Level") == "D1") & pl.col("League").is_in(P4_LEAGUES)).with_columns(pl.lit("P4").alias("Level"))),
    ])
    return (pl.concat([per_level, p4])
              .with_columns((pl.col("base_state") + "|" + pl.col("Outs").cast(pl.Utf8) + "|" + pl.col("Balls").cast(pl.Utf8)
                             + "-" + pl.col("Strikes").cast(pl.Utf8)).alias("re288_state"))
              .select(RE_COLUMNS).sort("Level", "year", "base_state", "Outs", "Balls", "Strikes"))


# ── Run ───────────────────────────────────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> int:
    log_path = ledger.setup_logging()
    con = ledger.connect()
    run_id = ledger.start_run(con, "build", vars(args), log_path)
    counts: Counter = Counter()
    try:
        t0 = time.time()
        games = candidates(con)
        clashes = resolve_clashes(games)
        held = {c[0] for c in clashes}
        kept = [g for g in games if g["game_id"] not in held]
        counts.update({"games served": len(kept), "games held (clash)": len(held),
                       "games excluded by hand": con.execute("SELECT COUNT(*) FROM games WHERE kind = 'pitches' "
                                                             "AND excluded = 1").fetchone()[0]})
        log.info("%d games served, %d held back by clashes (%s)", len(kept), len(held),
                 dict(Counter(c[3] for c in clashes)))

        groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for g in kept:
            groups[("D1" if g["level"] == "D1" else "Others", g["year"])].append(g)
        built = {(r["part"], r["year"]): dict(r) for r in con.execute("SELECT * FROM serving")}
        staged: list[tuple[Path, Path, tuple]] = []       # (temporary file, destination, ledger row)
        try:
            for key in sorted(groups):
                part, year = key
                fp, dest = fingerprint(groups[key]), serving_file(part, year)
                if not args.all and built.get(key, {}).get("fingerprint") == fp and dest.exists():
                    counts["year files unchanged"] += 1
                    continue
                paths = [game_file(g["game_id"]) for g in sorted(groups[key], key=lambda g: g["game_id"])]
                tmp, rows = stage(pl.scan_parquet(paths), dest)
                staged.append((tmp, dest, (part, year, ledger.rel(dest), fp, len(paths), rows)))
                log.info("staged %s: %d games, %d pitches", ledger.rel(dest), len(paths), rows)
            if args.re288:
                m = re288([game_file(g["game_id"]) for g in kept if g["verified"]])
                tmp, _ = stage(m.lazy(), config.SERVING_RE288)
                staged.append((tmp, config.SERVING_RE288, None))
                counts["re288 rows"] = m.height
                log.info("RE288: %d rows, %d (Level, year) tables", m.height, m.select("Level", "year").unique().height)
        except BaseException:
            for tmp, _, _ in staged:
                tmp.unlink(missing_ok=True)
            raise

        # Publish: swap every staged file in, remove emptied years, then record it all in the ledger.
        for tmp, dest, _ in staged:
            os.replace(tmp, dest)
        gone = sorted(set(built) - set(groups))           # a level group and year with no games left
        for key in gone:
            old = config.HERE / built[key]["path"]
            old.unlink(missing_ok=True)
            if old.parent.exists() and not any(old.parent.iterdir()):
                old.parent.rmdir()                       # the app treats an existing year folder as a year with data
            log.info("removed %s: no games left", built[key]["path"])
        now = ledger.now()
        with con:
            con.execute("DELETE FROM clashes")
            con.executemany("INSERT INTO clashes (held_game, kept_game, shared, rule, run_id) VALUES (?, ?, ?, ?, ?)",
                            [(*c, run_id) for c in clashes])
            con.executemany("INSERT OR REPLACE INTO serving (part, year, path, fingerprint, games, rows, built_at, run_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(*row, now, run_id) for _, _, row in staged if row])
            con.executemany("DELETE FROM serving WHERE part = ? AND year = ?", gone)
        counts["year files built"] = sum(1 for *_, row in staged if row)
        counts["year files removed"] = len(gone)
        ledger.finish_run(con, run_id, "ok", dict(counts))
        log.info("done in %.0f s: %s", time.time() - t0, dict(counts))
        return 0
    except BaseException as e:
        log.error("build failed: %s: %s", type(e).__name__, e)
        ledger.finish_run(con, run_id, "failed", dict(counts), error=f"{type(e).__name__}: {e}")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="games/ -> serving/")
    ap.add_argument("--all", action="store_true", help="rebuild every year file")
    ap.add_argument("--re288", action="store_true", help="also rebuild the RE288 matrix (verified games)")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
