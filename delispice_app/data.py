"""DuckDB serving layer for delispice_app (pitcher/batter reports).

Design (per the notebook's "never load the 7M-row frame whole" principle):

* **Picker index** — one DuckDB pass per role builds a small, de-duplicated table of
  ``(Part, Level, Team, Player, Year)`` cached to ``.cache/{role}_index.parquet``. ``Level`` is the
  fine game level (D1, JUCO, D2, …) that drives the UI; ``Part`` is the physical top-level partition
  (``D1`` / ``Others``) parsed from the file path, used only to prune scans. ``Player``/``Team`` come
  from the role's columns (Pitcher/PitcherTeam or Batter/BatterTeam). ``Conference`` is added from
  ``team_acronyms.csv`` (the team's own conference — SEC = the 16 SEC schools).

* **Per-player scan** — for the selected player we read ONLY the ``(Part, Year)`` partitions the index
  says they appear in, projecting just the columns that role's report needs. Scanning both physical
  partitions a player touches keeps the result complete (a few D1 rows live under ``Others/``).
  Results are cached in process so split toggles reuse them with no re-query.
"""
from __future__ import annotations

import csv
import functools
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import polars as pl

REPO = Path(__file__).resolve().parents[1]
WBASE = REPO / "data_pipeline" / "wbaserunners"
# team_acronyms.csv lives at backend/models/ in playgroundv2 and backend/research/ in delispice —
# take whichever exists so the app runs unchanged from either repo.
_ACR_CANDIDATES = (REPO / "backend" / "models" / "team_acronyms.csv",
                   REPO / "backend" / "research" / "team_acronyms.csv")
ACR_CSV = next((p for p in _ACR_CANDIDATES if p.exists()), _ACR_CANDIDATES[0])
CACHE_DIR = Path(__file__).resolve().parent / ".cache"

GLOB_ALL = str(WBASE / "**" / "*.parquet")

# Columns each role's report needs; everything else in the 206-column schema is dropped at read time.
PITCHER_COLS = [
    "Date", "Pitcher", "PitcherId", "PitcherThrows", "PitcherTeam", "Level", "PitchUID",
    "BatterSide", "Balls", "Strikes", "PitchofPA",
    "TaggedPitchType", "PitchCall", "KorBB", "PlayResult", "OutsOnPlay",
    "RelSpeed", "SpinRate", "SpinAxis", "RelHeight", "RelSide", "Extension",
    "InducedVertBreak", "HorzBreak", "PlateLocHeight", "PlateLocSide", "ExitSpeed",
]
BATTER_COLS = [
    "Date", "Batter", "BatterId", "BatterSide", "BatterTeam", "Level", "Pitcher", "PitcherThrows", "PitchUID",
    "PitchofPA", "PitchCall", "KorBB", "PlayResult", "TaggedPitchType", "TaggedHitType",
    "PlateLocHeight", "PlateLocSide", "ExitSpeed", "Angle", "Direction", "Bearing", "Distance",
]

ROLES = {
    "pitcher": {"player": "Pitcher", "team": "PitcherTeam", "cache": "pitcher_index.parquet", "cols": PITCHER_COLS},
    "batter":  {"player": "Batter",  "team": "BatterTeam",  "cache": "batter_index.parquet",  "cols": BATTER_COLS},
}

# Proper dtypes for an EMPTY result, so report builders (which do numeric ops) never crash on the
# all-string schema an untyped empty frame would have.
_STR_COLS = {"Date", "Pitcher", "PitcherId", "PitcherThrows", "PitcherTeam", "Batter", "BatterId",
             "BatterSide", "BatterTeam", "Level", "PitchUID", "TaggedPitchType", "PitchCall",
             "KorBB", "PlayResult", "TaggedHitType"}
_INT_COLS = {"Balls", "Strikes", "PitchofPA", "OutsOnPlay"}


def _schema_for(cols) -> dict:
    s = {c: (pl.Utf8 if c in _STR_COLS else pl.Int64 if c in _INT_COLS else pl.Float64) for c in cols}
    s["Year"] = pl.Utf8
    return s


ALL = "All"


def _mem_limit() -> str:
    """DuckDB memory cap. ``DUCKDB_MEMORY_LIMIT`` env wins; otherwise ~65% of physical
    RAM (leaves headroom for the web layer + OS so heavy queries spill instead of OOM).
    Falls back to 10GB if RAM can't be probed (e.g. non-Unix)."""
    if override := os.environ.get("DUCKDB_MEMORY_LIMIT"):
        return override
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return f"{max(1, int(total * 0.65 // 1024 ** 3))}GB"
    except (ValueError, OSError, AttributeError):
        return "10GB"


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    # All cores for intra-query parallelism; capped memory so a heavy query spills to
    # disk instead of tripping the OOM killer. Both scale to whatever box this runs on.
    con.execute(f"PRAGMA threads={os.cpu_count() or 8}")
    con.execute(f"SET memory_limit='{_mem_limit()}'")
    return con


# ── Team acronym -> full name, and -> conference (from team_acronyms.csv) ─────────────────────────
@functools.lru_cache(maxsize=1)
def team_maps() -> tuple[dict, dict]:
    try:
        acr = pl.read_csv(ACR_CSV)
        name = {a: (a if str(n).startswith("?") else n)
                for a, n in zip(acr["acronym"], acr["team_name"])}
        conf = {a: c for a, c in zip(acr["acronym"], acr["conference"]) if c}  # blank = no D1 conf
        return name, conf
    except Exception as e:  # pragma: no cover - fall back to acronyms
        print("team_acronyms.csv not loaded, showing acronyms / no conference:", e)
        return {}, {}


def team_label(acr: str) -> str:
    return team_maps()[0].get(acr, acr)


# ── Player bio: height + birthday from data_pipeline/heights.csv (scraped by height_scraper.py) ────
# The table is keyed (Name, TrackManId). The app's manual edits are appended as Status='manual' rows
# that win on read (last row for a key wins); height_scraper skips them (status isn't a retry status).
HEIGHTS_CSV = REPO / "data_pipeline" / "heights.csv"
# Column order for a NEW file only — must match height_scraper.FIELDS. An existing file is appended
# using its own header (read below), so reads/writes stay aligned even if the schema drifts.
_HEIGHTS_FIELDS = ["Name", "TrackManId", "HeightIn", "Height", "WeightLb", "BirthDate",
                   "Status", "BRUrl", "ScrapedAt"]
_HEIGHTS_CACHE: dict = {"mtime": None, "map": {}}


def _heights_map() -> dict:
    """(Name, TrackManId) -> latest row dict, re-read whenever heights.csv changes on disk."""
    try:
        mtime = HEIGHTS_CSV.stat().st_mtime
    except FileNotFoundError:
        _HEIGHTS_CACHE.update(mtime=None, map={})
        return {}
    if _HEIGHTS_CACHE["mtime"] != mtime:
        m = {}
        with open(HEIGHTS_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):        # append-only file: a later row (manual/rescrape) wins
                m[(r["Name"], r.get("TrackManId", ""))] = r
        _HEIGHTS_CACHE.update(mtime=mtime, map=m)
    return _HEIGHTS_CACHE["map"]


def _bday(s: str | None) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date() if s and s.strip() else None
    except ValueError:
        return None


def bio_lookup(name: str, trackman_id: str | None) -> dict | None:
    """Bio for one player: {height_in, height, weight, birthdate(date|None), status}. Prefers an
    exact (name, id) match, then falls back to any row with the same name (so a row scraped under a
    blank/mismatched id still shows). None if the player isn't in the table at all."""
    m = _heights_map()
    row = m.get((name, str(trackman_id or ""))) or next((v for (n, _), v in m.items() if n == name), None)
    if row is None:
        return None
    hi, wt = str(row.get("HeightIn") or "").strip(), str(row.get("WeightLb") or "").strip()
    return {"height_in": int(hi) if hi.isdigit() else None,
            "height": row.get("Height") or "",
            "weight": int(wt) if wt.isdigit() else None,
            "birthdate": _bday(row.get("BirthDate")),
            "status": row.get("Status") or ""}


def save_manual_bio(name: str, trackman_id: str | None, height_in: int | None,
                    birthdate: date | None) -> None:
    """Append a Status='manual' bio row from the app's edit form, then bust the read cache. Aligns
    columns to the file's existing header if present, else bootstraps with _HEIGHTS_FIELDS."""
    row = {"Name": name, "TrackManId": str(trackman_id or ""),
           "HeightIn": height_in if height_in else "",
           "Height": f"{height_in // 12}-{height_in % 12}" if height_in else "", "WeightLb": "",
           "BirthDate": birthdate.strftime("%Y-%m-%d") if birthdate else "",
           "Status": "manual", "BRUrl": "",
           "ScrapedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    exists = HEIGHTS_CSV.exists()
    fields = _HEIGHTS_FIELDS
    if exists:
        with open(HEIGHTS_CSV, newline="", encoding="utf-8") as f:
            hdr = f.readline().strip()
        if hdr:
            fields = hdr.split(",")
    HEIGHTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(HEIGHTS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)
    _HEIGHTS_CACHE["mtime"] = None      # force a re-read on the next lookup


# ── Picker index (cached, per role) ───────────────────────────────────────────────────────────────
_INDEX_MEM: dict[str, pl.DataFrame] = {}


def get_index(role: str = "pitcher", force_rebuild: bool = False) -> pl.DataFrame:
    """The small selection index for a role, with a ``Conference`` column mapped from team acronyms."""
    if role in _INDEX_MEM and not force_rebuild:
        return _INDEX_MEM[role]
    path = CACHE_DIR / ROLES[role]["cache"]
    if force_rebuild or not path.exists():
        _build_index_parquet(role)
    df = pl.read_parquet(path)
    if "Player" not in df.columns:            # stale cache from an older schema -> rebuild once
        _build_index_parquet(role)
        df = pl.read_parquet(path)
    _, conf = team_maps()
    df = df.with_columns(pl.col("Team").replace_strict(conf, default=None).alias("Conference"))
    _INDEX_MEM[role] = df
    return df


def _build_index_parquet(role: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    r = ROLES[role]
    path = CACHE_DIR / r["cache"]
    con = _con()
    con.execute(f"""
        COPY (
          SELECT DISTINCT
            regexp_extract(filename, 'wbaserunners/([^/]+)/', 1) AS Part,
            Level, {r['team']} AS Team, {r['player']} AS Player, substr(Date, 1, 4) AS Year
          FROM read_parquet('{GLOB_ALL}', filename = true)
          WHERE {r['player']} IS NOT NULL AND Date IS NOT NULL AND length(Date) >= 4
        ) TO '{path}' (FORMAT PARQUET)
    """)
    con.close()


def index_stats(role: str = "pitcher") -> dict:
    idx = get_index(role)
    return {"combos": idx.height, "players": idx["Player"].n_unique(),
            "teams": idx["Team"].n_unique(), "levels": idx["Level"].n_unique()}


# ── Cascading-picker option helpers (all off the in-memory index — fast) ──────────────────────────
def _opts(frame: pl.DataFrame, col: str) -> list[str]:
    return sorted(v for v in frame[col].unique().to_list() if v is not None)


def years(role: str = "pitcher") -> list[str]:
    return sorted((v for v in get_index(role)["Year"].unique().to_list() if v), reverse=True)


def levels(role: str = "pitcher") -> list[str]:
    return _opts(get_index(role), "Level")


def scope(role: str, years_sel=None, level=ALL, conf=ALL, team=ALL, depth=3) -> pl.DataFrame:
    """Index filtered by selected Year(s) (always) + the first ``depth`` of [Level, Conference, Team]."""
    d = get_index(role)
    if years_sel:
        d = d.filter(pl.col("Year").is_in(years_sel))
    if depth >= 1 and level and level != ALL:
        d = d.filter(pl.col("Level") == level)
    if depth >= 2 and conf and conf != ALL:
        d = d.filter(pl.col("Conference") == conf)
    if depth >= 3 and team and team != ALL:
        d = d.filter(pl.col("Team") == team)
    return d


def conference_options(role: str, years_sel=None, level=ALL) -> list[str]:
    return [ALL] + _opts(scope(role, years_sel, level, depth=1), "Conference")


def team_options(role: str, years_sel=None, level=ALL, conf=ALL) -> list[dict]:
    """[{label: full team name, value: acronym}] sorted by name, with 'All' first."""
    frame = scope(role, years_sel, level, conf, depth=2)
    pairs = sorted(((team_label(a), a) for a in _opts(frame, "Team")), key=lambda t: t[0].lower())
    return [{"label": ALL, "value": ALL}] + [{"label": name, "value": acr} for name, acr in pairs]


def player_options(role: str, years_sel=None, level=ALL, conf=ALL, team=ALL) -> list[str]:
    return _opts(scope(role, years_sel, level, conf, team, depth=3), "Player")


# ── Per-player data (targeted, column-projected, cached) ──────────────────────────────────────────
def _partitions(role: str, player: str, level=ALL, team=ALL, years_sel=None) -> list[tuple[str, str]]:
    """Distinct (Part, Year) the player's selected rows physically live in."""
    d = get_index(role).filter(pl.col("Player") == player)
    if level and level != ALL:
        d = d.filter(pl.col("Level") == level)
    if team and team != ALL:
        d = d.filter(pl.col("Team") == team)
    if years_sel:
        d = d.filter(pl.col("Year").is_in(years_sel))
    return list(d.select("Part", "Year").unique().iter_rows())


@functools.lru_cache(maxsize=64)
def _rows_cached(role: str, player: str, level: str, team: str, years_key: tuple[str, ...]) -> pl.DataFrame:
    r = ROLES[role]
    years_sel = list(years_key) if years_key else None
    parts = _partitions(role, player, level, team, years_sel)
    if not parts:
        return pl.DataFrame(schema=_schema_for(r["cols"]))
    globs = [str(WBASE / pt / yr / "**" / "*.parquet") for pt, yr in parts]
    glob_list = "[" + ", ".join("'" + g + "'" for g in globs) + "]"
    where, params = [f"{r['player']} = ?"], [player]
    if level and level != ALL:
        where.append("Level = ?"); params.append(level)
    if team and team != ALL:
        where.append(f"{r['team']} = ?"); params.append(team)
    if years_sel:
        where.append("substr(Date, 1, 4) IN (" + ", ".join("?" * len(years_sel)) + ")")
        params += years_sel
    con = _con()
    df = con.execute(f"SELECT {', '.join(r['cols'])} FROM read_parquet({glob_list}) "
                     f"WHERE {' AND '.join(where)}", params).pl()
    con.close()
    return apply_retags(df.with_columns(pl.col("Date").str.slice(0, 4).alias("Year")))


def get_rows(role: str, player: str, level=ALL, team=ALL, years_sel=None) -> pl.DataFrame:
    """The selected player's rows — only needed columns, only the partitions they appear in."""
    years_key = tuple(sorted(years_sel)) if years_sel else ()
    return _rows_cached(role, player, level or ALL, team or ALL, years_key)


# ── Pitch retagging: a non-destructive override layer applied at read time ────────────────────────
#   * global[old] = new                          -> remap a tag for every pitcher
#   * pitcher_tag[pitcher][old] = new            -> remap a tag for one pitcher
#   * pitch[PitchUID] = {"t": new, "p": pitcher} -> retag individual pitches (from the movement lasso)
# Precedence: global, then per-pitcher tag, then per-pitch (most specific wins). Both reports read
# through this, so an edit shows up everywhere and is fully reversible.
RETAG_PATH = CACHE_DIR / "retags.json"
_RETAGS: dict = {}
_RETAGS_LOADED = [False]
_SEP = "\x01"


def load_retags() -> dict:
    if not _RETAGS_LOADED[0]:
        try:
            _RETAGS.update(json.loads(RETAG_PATH.read_text()) if RETAG_PATH.exists() else {})
        except Exception:
            pass
        _RETAGS_LOADED[0] = True
    for k in ("global", "pitcher_tag", "pitch"):
        _RETAGS.setdefault(k, {})
    return _RETAGS


def _save_retags() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RETAG_PATH.write_text(json.dumps(_RETAGS))
    _rows_cached.cache_clear()          # bust per-player cache so reports re-read with the new tags


def set_global_rule(old: str, new: str | None) -> None:
    d = load_retags()
    if new:
        d["global"][old] = new
    else:
        d["global"].pop(old, None)
    _save_retags()


def set_pitcher_tag(pitcher: str, old: str, new: str | None) -> None:
    d = load_retags()
    m = d["pitcher_tag"].setdefault(pitcher, {})
    if new:
        m[old] = new
    else:
        m.pop(old, None)
    _save_retags()


def set_pitch_overrides(uids, new: str | None, pitcher: str) -> None:
    d = load_retags()
    for u in uids:
        if new:
            d["pitch"][u] = {"t": new, "p": pitcher}
        else:
            d["pitch"].pop(u, None)
    _save_retags()


def clear_retags(pitcher: str | None = None) -> None:
    d = load_retags()
    if pitcher is None:
        d["global"].clear(); d["pitcher_tag"].clear(); d["pitch"].clear()
    else:
        d["pitcher_tag"].pop(pitcher, None)
        d["pitch"] = {u: v for u, v in d["pitch"].items() if v.get("p") != pitcher}
    _save_retags()


def retag_summary(pitcher: str | None = None) -> dict:
    d = load_retags()
    pt = d["pitcher_tag"].get(pitcher, {}) if pitcher else {}
    pitches = sum(1 for v in d["pitch"].values() if not pitcher or v.get("p") == pitcher)
    return {"global": dict(d["global"]), "pitcher_tag": dict(pt), "pitches": pitches}


def apply_retags(df: pl.DataFrame) -> pl.DataFrame:
    d = load_retags()
    if df.height == 0 or not (d["global"] or d["pitcher_tag"] or d["pitch"]):
        return df
    new = pl.col("TaggedPitchType")
    if d["global"]:
        new = new.replace(d["global"])
    if d["pitcher_tag"] and "Pitcher" in df.columns:
        pt_map = {f"{p}{_SEP}{o}": v for p, tags in d["pitcher_tag"].items() for o, v in tags.items()}
        if pt_map:
            key = pl.col("Pitcher") + pl.lit(_SEP) + new
            new = pl.when(key.is_in(list(pt_map))).then(key.replace(pt_map)).otherwise(new)
    if d["pitch"] and "PitchUID" in df.columns:
        uid_map = {u: v["t"] for u, v in d["pitch"].items()}
        new = pl.when(pl.col("PitchUID").is_in(list(uid_map))) \
                .then(pl.col("PitchUID").replace(uid_map)).otherwise(new)
    return df.with_columns(new.alias("TaggedPitchType"))


def get_pitches(pitcher: str, level=ALL, team=ALL, years_sel=None) -> pl.DataFrame:
    """Back-compat helper for the pitcher role."""
    return get_rows("pitcher", pitcher, level, team, years_sel)


# ── AutoCluster: GMM cluster assignments as their OWN store, separate from retags ─────────────────
# ``autocluster.json``: { pitcher: {assign: {PitchUID: int}, names: {"0": rename|None}, k, n,
# n_unclustered, features} }. Kept apart from retags.json so clustering can be reverted wholesale
# without touching manual retags. While a pitcher has an entry, the PITCHER report shows cluster
# labels ("Cluster 0", … or their renames) instead of TaggedPitchType; revert deletes the entry.
AUTOCLUSTER_PATH = CACHE_DIR / "autocluster.json"
UNCLUSTERED = "Unclustered"
_ACLUSTER: dict = {}
_ACLUSTER_LOADED = [False]

# The GMM adapter lives with the models (backend/ is not a package -> load it by file path).
CLUSTER_PY = REPO / "backend" / "models" / "cluster.py"
_CLUSTER_MOD = [None]


def _cluster_mod():
    if _CLUSTER_MOD[0] is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("delispice_cluster", CLUSTER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CLUSTER_MOD[0] = mod
    return _CLUSTER_MOD[0]


def _load_autocluster() -> dict:
    if not _ACLUSTER_LOADED[0]:
        try:
            _ACLUSTER.update(json.loads(AUTOCLUSTER_PATH.read_text()) if AUTOCLUSTER_PATH.exists() else {})
        except Exception:
            pass
        _ACLUSTER_LOADED[0] = True
    return _ACLUSTER


def _save_autocluster() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    AUTOCLUSTER_PATH.write_text(json.dumps(_ACLUSTER))


def cluster_state(pitcher: str) -> dict | None:
    return _load_autocluster().get(pitcher)


def run_autocluster(pitcher: str, df: pl.DataFrame, use_release: bool = False) -> dict:
    """Run the GMM on this pitcher's (current selection of) pitches and store the assignment."""
    res = _cluster_mod().run_gmm(df, use_release=use_release)
    res["names"] = {str(i): None for i in range(res["k"])}
    _load_autocluster()[pitcher] = res
    _save_autocluster()
    return res


def set_cluster_name(pitcher: str, idx: int, name: str | None) -> None:
    ent = cluster_state(pitcher)
    if ent is None:
        return
    ent["names"][str(idx)] = name or None
    _save_autocluster()


def clear_autocluster(pitcher: str | None = None) -> None:
    d = _load_autocluster()
    if pitcher is None:
        d.clear()
    else:
        d.pop(pitcher, None)
    _save_autocluster()


def cluster_label(ent: dict, idx: int) -> str:
    return ent["names"].get(str(idx)) or f"Cluster {idx}"


def cluster_view(df: pl.DataFrame, pitcher: str) -> pl.DataFrame:
    """Replace TaggedPitchType with this pitcher's cluster labels (renames win); rows the model
    couldn't cluster (missing features / not in the clustered selection) become "Unclustered"."""
    ent = cluster_state(pitcher)
    if not ent or df.height == 0 or "PitchUID" not in df.columns:
        return df
    m = {u: cluster_label(ent, c) for u, c in ent["assign"].items()}
    new = (pl.when(pl.col("PitchUID").is_in(list(m)))
             .then(pl.col("PitchUID").replace(m))
             .otherwise(pl.lit(UNCLUSTERED)))
    return df.with_columns(new.alias("TaggedPitchType"))


def download_frame(pitcher: str, level=ALL, team=ALL, years_sel=None) -> pl.DataFrame:
    """The pitcher's FULL raw rows (all original columns) + a ``ClusterTag`` column appended at the
    very end holding the (renamed) cluster labels — for the CSV/parquet export."""
    parts = _partitions("pitcher", pitcher, level, team, years_sel)
    if not parts:
        return pl.DataFrame()
    globs = [str(WBASE / pt / yr / "**" / "*.parquet") for pt, yr in parts]
    glob_list = "[" + ", ".join("'" + g + "'" for g in globs) + "]"
    where, params = ["Pitcher = ?"], [pitcher]
    if level and level != ALL:
        where.append("Level = ?"); params.append(level)
    if team and team != ALL:
        where.append("PitcherTeam = ?"); params.append(team)
    if years_sel:
        where.append("substr(Date, 1, 4) IN (" + ", ".join("?" * len(years_sel)) + ")")
        params += years_sel
    con = _con()
    raw = con.execute(f"SELECT * FROM read_parquet({glob_list}, union_by_name = true) "
                      f"WHERE {' AND '.join(where)}", params).pl()
    con.close()
    ent = cluster_state(pitcher)
    if ent:
        m = {u: cluster_label(ent, c) for u, c in ent["assign"].items()}
        tag = (pl.when(pl.col("PitchUID").is_in(list(m)))
                 .then(pl.col("PitchUID").replace(m))
                 .otherwise(pl.lit(UNCLUSTERED)))
    else:
        tag = pl.lit(UNCLUSTERED)
    return raw.with_columns(tag.alias("ClusterTag"))     # with_columns appends -> last column


# ── Percentile pool: per-pitcher aggregates for the Savant-style slider chart ─────────────────────
# One DuckDB pass over the selected Level (+Years) computes every pitcher's metrics; the result is
# disk-cached per (level, years) so only the first look at a combo pays the scan. "Qualified" =
# QUAL_OUTS recorded outs (10 IP); percentiles are taken against the qualified pool only.
QUAL_OUTS = 60                                            # 10 IP — the qualification threshold
FASTBALL_TAGS = ("Fastball", "FourSeamFastBall", "TwoSeamFastBall", "OneSeamFastBall", "Sinker")
_SWINGS_SQL = "('StrikeSwinging','FoulBallNotFieldable','FoulBallFieldable','InPlay')"


def _pool_path(level: str, years_key: tuple[str, ...]) -> Path:
    yk = "-".join(years_key) if years_key else "all"
    return CACHE_DIR / f"pctpool_{(level or ALL).replace(' ', '')}_{yk}.parquet"


@functools.lru_cache(maxsize=16)
def percentile_pool(level: str, years_key: tuple[str, ...] = ()) -> pl.DataFrame:
    """Per-pitcher metric aggregates for one Level (+Years): bf, k/bb rates, outs, fastball velo,
    extension, avg EV, hard-hit%, barrel% (Statcast definition), GB%, whiff%, chase%."""
    path = _pool_path(level, years_key)
    if path.exists():
        return pl.read_parquet(path)
    # partitions holding this level (+years), via the picker index
    d = get_index("pitcher")
    if level and level != ALL:
        d = d.filter(pl.col("Level") == level)
    if years_key:
        d = d.filter(pl.col("Year").is_in(list(years_key)))
    parts = list(d.select("Part", "Year").unique().iter_rows())
    if not parts:
        return pl.DataFrame()
    globs = [str(WBASE / pt / yr / "**" / "*.parquet") for pt, yr in parts]
    glob_list = "[" + ", ".join("'" + g + "'" for g in globs) + "]"
    where, params = ["Pitcher IS NOT NULL"], []
    if level and level != ALL:
        where.append("Level = ?"); params.append(level)
    if years_key:
        where.append("substr(Date, 1, 4) IN (" + ", ".join("?" * len(years_key)) + ")")
        params += list(years_key)
    fb = "('" + "','".join(FASTBALL_TAGS) + "')"
    con = _con()
    df = con.execute(f"""
        WITH p AS (
          SELECT Pitcher,
            CASE WHEN PitchofPA = 1 THEN 1 ELSE 0 END AS is_pa,
            CASE WHEN KorBB = 'Strikeout' THEN 1 ELSE 0 END AS is_k,
            CASE WHEN KorBB = 'Walk' THEN 1 ELSE 0 END AS is_bb,
            COALESCE(OutsOnPlay, 0) AS outs_play,
            CASE WHEN TaggedPitchType IN {fb} THEN RelSpeed END AS fb_velo,
            Extension AS ext,
            CASE WHEN PitchCall IN {_SWINGS_SQL} THEN 1 ELSE 0 END AS is_swing,
            CASE WHEN PitchCall = 'StrikeSwinging' THEN 1 ELSE 0 END AS is_whiff,
            CASE WHEN PlateLocSide IS NOT NULL AND PlateLocHeight IS NOT NULL
                      AND NOT (ABS(PlateLocSide) <= 0.83 AND PlateLocHeight BETWEEN 1.5 AND 3.3775)
                 THEN 1 ELSE 0 END AS is_oz,
            CASE WHEN PitchCall = 'InPlay' AND ExitSpeed IS NOT NULL THEN ExitSpeed END AS bbe_ev,
            CASE WHEN PitchCall = 'InPlay' AND ExitSpeed >= 98 AND Angle IS NOT NULL
                      AND Angle >= GREATEST(8, 26 - (ExitSpeed - 98))
                      AND Angle <= LEAST(50, 30 + (ExitSpeed - 98) * 20.0 / 18.0)
                 THEN 1 ELSE 0 END AS is_barrel,
            CASE WHEN PitchCall = 'InPlay'
                      AND TaggedHitType IN ('GroundBall','FlyBall','LineDrive','Popup')
                 THEN 1 ELSE 0 END AS is_bip,
            CASE WHEN PitchCall = 'InPlay' AND TaggedHitType = 'GroundBall' THEN 1 ELSE 0 END AS is_gb
          FROM read_parquet({glob_list})
          WHERE {' AND '.join(where)}
        )
        SELECT Pitcher,
          count(*)::BIGINT AS n_pitches,
          sum(is_pa)::BIGINT AS bf, sum(is_k)::BIGINT AS k, sum(is_bb)::BIGINT AS bb,
          (sum(outs_play) + sum(is_k))::BIGINT AS outs,
          avg(fb_velo) AS fb_velo, avg(ext) AS ext, avg(bbe_ev) AS avg_ev,
          sum(CASE WHEN bbe_ev >= 95 THEN 1 ELSE 0 END)::DOUBLE / NULLIF(count(bbe_ev), 0) AS hardhit,
          sum(is_barrel)::DOUBLE / NULLIF(count(bbe_ev), 0) AS barrel,
          sum(is_gb)::DOUBLE / NULLIF(sum(is_bip), 0) AS gb,
          sum(is_whiff)::DOUBLE / NULLIF(sum(is_swing), 0) AS whiff,
          sum(CASE WHEN is_oz = 1 AND is_swing = 1 THEN 1 ELSE 0 END)::DOUBLE
              / NULLIF(sum(is_oz), 0) AS chase
        FROM p GROUP BY Pitcher
    """, params).pl()
    con.close()
    bf_ok = pl.col("bf") > 0
    df = df.with_columns(
        pl.when(bf_ok).then(pl.col("k") / pl.col("bf")).alias("k_pct"),
        pl.when(bf_ok).then(pl.col("bb") / pl.col("bf")).alias("bb_pct"),
        (pl.col("outs") >= QUAL_OUTS).alias("qualified"),
    )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    return df


def clear_percentile_pools() -> None:
    """Drop the cached pools (used by ⟳ Rebuild index, so new games flow into the percentiles)."""
    percentile_pool.cache_clear()
    for f in CACHE_DIR.glob("pctpool_*.parquet"):
        f.unlink(missing_ok=True)
