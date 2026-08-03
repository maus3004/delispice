"""Population-wide player lookup — the leaderboard behind the Lookup tab.

Same discipline as ``data.percentile_pool``: never scan the parquet tree at request time. One DuckDB
pass per (role, level, years) builds a pool of RAW COUNTS AND SUMS, disk-cached to
``.cache/lb_{role}_{level}_{years}.parquet``; every question the UI asks is then answered by Polars
group-bys over a few tens of thousands of cached rows, which is instant.

* **Pool grain is (Player, Team, PitchType)** — never rates. Storing ``sum_velo``/``n_velo`` instead
  of ``avg_velo`` is what makes the pitch-type scoping work: rolling any subset of pitch types up to
  a player is a plain sum, so "his fastball" and "everything he throws" come off the same cached
  table with no re-query. Rates are divided out at the very end, in :func:`_rates`.

* **Filters are scoped predicates.** "Possesses a fastball with >18in IVB" rolls the pool up over
  the Fastballs scope only, tests the condition there, and keeps the players that pass; the row that
  displays is still the player's overall line. Several conditions with different scopes intersect.

* **xRV comes from the trained artifacts**, never from a fit here — ``cq_store.load(level, year)``
  per the offline-train/serve-artifact contract in ``backend/models/cq_store.py``. Balls are scored
  once at pool-build time and their run values summed into the cached parquet, so a leaderboard
  query costs no k-NN work at all. Levels with no trained artifact get null xRV (not a substituted
  model — a D1 run environment would misprice a JUCO batted ball).

Legacy tag note: 2022-23 files call every foul ``FoulBall``; 2024 is mixed; 2025+ use the
``FoulBall{Not,}Fieldable`` pair. All three count as swings/strikes here, and — since the fix that
followed from building this module — in ``report.SWING_CALLS`` and ``data._SWINGS_SQL`` too, so the
Lookup tab and the reports agree on every season.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import polars as pl

from . import data
from .data import ALL, CACHE_DIR, WBASE

# Pitch-call vocabularies. All three foul spellings are included on purpose (see module docstring);
# must stay in step with report.FOUL_CALLS and data._SWINGS_SQL.
_FOULS = ("FoulBall", "FoulBallNotFieldable", "FoulBallFieldable")
_SWINGS = ("StrikeSwinging", *_FOULS, "InPlay")
_STRIKES = ("StrikeCalled", *_SWINGS)
HARD_HIT_MPH = 95                                    # matches report.HARD_HIT_MPH
ZONE_SQL = "ABS(PlateLocSide) <= 0.83 AND PlateLocHeight BETWEEN 1.5 AND 3.3775"

DEFAULT_MIN_PITCHES = 200                            # pitches thrown / seen to appear at all
DEFAULT_MIN_SCOPED = 25                              # pitches within a scoped condition's pitch type


def _q(vals) -> str:
    return "(" + ", ".join(f"'{v}'" for v in vals) + ")"


# ── Pitch-type scopes ────────────────────────────────────────────────────────────────────────────
# Families mirror report.PITCH_FAMILY so a scope means the same thing here as on the batter table.
FAMILIES = {
    "Fastballs": ["Fastball", "FourSeamFastBall", "TwoSeamFastBall", "OneSeamFastBall", "Sinker"],
    "Breaking": ["Slider", "Sweeper", "Curveball", "Cutter", "Slurve"],
    "Offspeed": ["ChangeUp", "Splitter"],
}
SPECIFIC_TYPES = ["Fastball", "FourSeamFastBall", "TwoSeamFastBall", "Sinker", "Cutter", "Slider",
                  "Sweeper", "Curveball", "ChangeUp", "Splitter", "Knuckleball"]
SCOPE_ANY = "Any"
SCOPE_OPTIONS = [SCOPE_ANY, *FAMILIES, *SPECIFIC_TYPES]


def scope_types(scope: str) -> list[str] | None:
    """The pitch types a scope covers; None for 'Any' (meaning: don't filter)."""
    if not scope or scope == SCOPE_ANY:
        return None
    return FAMILIES.get(scope, [scope])


# ── The cached pool ──────────────────────────────────────────────────────────────────────────────
# Every column is a count or a sum, so summing across pitch types is always the correct roll-up.
# (Maxes are roll-up-safe too: max of maxes.) Rates never live here — see _rates().
_AGG_SQL = f"""
  count(*)::BIGINT                                            AS n_pitches,
  count(velo)::BIGINT AS n_velo, sum(velo)                    AS sum_velo,
  max(velo)                                                   AS max_velo,
  count(spin)::BIGINT AS n_spin, sum(spin)                    AS sum_spin,
  count(ivb)::BIGINT  AS n_ivb,  sum(ivb)                     AS sum_ivb,
  count(hb)::BIGINT   AS n_hb,   sum(hb)                      AS sum_hb,
                                 sum(hb_arm)                  AS sum_hb_arm,
  count(ext)::BIGINT  AS n_ext,  sum(ext)                     AS sum_ext,
  count(relh)::BIGINT AS n_relh, sum(relh)                    AS sum_relh,
  sum(is_strike)::BIGINT       AS strikes,
  sum(is_called)::BIGINT       AS called_strikes,
  sum(is_swing)::BIGINT        AS swings,
  sum(is_whiff)::BIGINT        AS whiffs,
  sum(1 - is_swing)::BIGINT    AS takes,
  sum(has_loc)::BIGINT         AS loc_n,
  sum(in_zone)::BIGINT         AS zone_n,
  sum(is_oz)::BIGINT           AS oz_n,
  sum(CASE WHEN is_oz = 1     AND is_swing = 1 THEN 1 ELSE 0 END)::BIGINT AS oz_swings,
  sum(CASE WHEN in_zone = 1   AND is_swing = 1 THEN 1 ELSE 0 END)::BIGINT AS iz_swings,
  sum(CASE WHEN in_zone = 1   AND is_whiff = 1 THEN 1 ELSE 0 END)::BIGINT AS iz_whiffs,
  sum(is_pa)::BIGINT           AS pa,
  sum(is_k)::BIGINT            AS k,
  sum(is_bb)::BIGINT           AS bb,
  sum(is_hbp)::BIGINT          AS hbp,
  sum(is_sac)::BIGINT          AS sac,
  sum(is_1b)::BIGINT           AS singles,
  sum(is_2b)::BIGINT           AS doubles,
  sum(is_3b)::BIGINT           AS triples,
  sum(is_hr)::BIGINT           AS hr,
  sum(outs_play)::BIGINT       AS outs_play,
  count(ev)::BIGINT   AS n_ev,   sum(ev)                      AS sum_ev,
  max(ev)                                                     AS max_ev,
  count(la)::BIGINT   AS n_la,   sum(la)                      AS sum_la,
  sum(is_hardhit)::BIGINT      AS hardhit,
  sum(is_barrel)::BIGINT       AS barrels,
  sum(is_bip)::BIGINT          AS bip,
  sum(is_gb)::BIGINT           AS gb,
  sum(is_ld)::BIGINT           AS ld,
  sum(is_fb)::BIGINT           AS fb
"""


def _key_sql(role: str) -> str:
    """The pool's grouping key: player, team, and effective pitch type. Shared by the aggregate
    pass and the xRV pass so both group on exactly the same expressions."""
    r = data.ROLES[role]
    return f"""
    {r['player']} AS Player, {r['team']} AS Team,
    -- Effective pitch type: the tag, falling back to AutoPitchType (normalized to the raw
    -- vocabulary) when the tag is missing/Undefined — same intent as report._eff_type_expr.
    CASE WHEN TaggedPitchType IS NOT NULL AND TaggedPitchType NOT IN ('Undefined', 'Other', '')
              THEN TaggedPitchType
         WHEN AutoPitchType = 'Four-Seam' THEN 'FourSeamFastBall'
         WHEN AutoPitchType = 'Changeup'  THEN 'ChangeUp'
         WHEN AutoPitchType IS NOT NULL AND AutoPitchType NOT IN ('Undefined', 'Other', '')
              THEN AutoPitchType
         ELSE 'Undefined' END AS PitchType"""


def _flags_sql(role: str) -> str:
    """Per-pitch 0/1 flags and nullable measures, before the group-by. Identical for both roles —
    only the grouping key differs, so a pitcher's 'Hard Hit %' is contact allowed and a batter's is
    contact produced, off one definition."""
    return f"""{_key_sql(role)},
    RelSpeed AS velo, SpinRate AS spin, InducedVertBreak AS ivb, HorzBreak AS hb,
    -- Arm-side break: HorzBreak is signed in field coordinates, so a LHP's arm-side run is negative.
    -- Flipping lefties makes "gets 15in of arm-side run" one threshold for both hands.
    CASE WHEN PitcherThrows = 'Left' THEN -HorzBreak ELSE HorzBreak END AS hb_arm,
    Extension AS ext, RelHeight AS relh,
    CASE WHEN PitchCall IN {_q(_STRIKES)} THEN 1 ELSE 0 END AS is_strike,
    CASE WHEN PitchCall = 'StrikeCalled'   THEN 1 ELSE 0 END AS is_called,
    CASE WHEN PitchCall IN {_q(_SWINGS)}   THEN 1 ELSE 0 END AS is_swing,
    CASE WHEN PitchCall = 'StrikeSwinging' THEN 1 ELSE 0 END AS is_whiff,
    CASE WHEN PlateLocSide IS NOT NULL AND PlateLocHeight IS NOT NULL THEN 1 ELSE 0 END AS has_loc,
    CASE WHEN PlateLocSide IS NOT NULL AND PlateLocHeight IS NOT NULL
              AND {ZONE_SQL} THEN 1 ELSE 0 END AS in_zone,
    CASE WHEN PlateLocSide IS NOT NULL AND PlateLocHeight IS NOT NULL
              AND NOT ({ZONE_SQL}) THEN 1 ELSE 0 END AS is_oz,
    CASE WHEN PitchofPA = 1 THEN 1 ELSE 0 END AS is_pa,
    CASE WHEN KorBB = 'Strikeout' THEN 1 ELSE 0 END AS is_k,
    CASE WHEN KorBB = 'Walk' THEN 1 ELSE 0 END AS is_bb,
    CASE WHEN PitchCall = 'HitByPitch' THEN 1 ELSE 0 END AS is_hbp,
    CASE WHEN PlayResult = 'Sacrifice' THEN 1 ELSE 0 END AS is_sac,
    CASE WHEN PlayResult = 'Single'  THEN 1 ELSE 0 END AS is_1b,
    CASE WHEN PlayResult = 'Double'  THEN 1 ELSE 0 END AS is_2b,
    CASE WHEN PlayResult = 'Triple'  THEN 1 ELSE 0 END AS is_3b,
    CASE WHEN PlayResult = 'HomeRun' THEN 1 ELSE 0 END AS is_hr,
    COALESCE(OutsOnPlay, 0) AS outs_play,
    CASE WHEN PitchCall = 'InPlay' AND ExitSpeed IS NOT NULL THEN ExitSpeed END AS ev,
    CASE WHEN PitchCall = 'InPlay' AND Angle IS NOT NULL THEN Angle END AS la,
    CASE WHEN PitchCall = 'InPlay' AND ExitSpeed >= {HARD_HIT_MPH} THEN 1 ELSE 0 END AS is_hardhit,
    -- Statcast barrel definition, identical to data.percentile_pool's.
    CASE WHEN PitchCall = 'InPlay' AND ExitSpeed >= 98 AND Angle IS NOT NULL
              AND Angle >= GREATEST(8, 26 - (ExitSpeed - 98))
              AND Angle <= LEAST(50, 30 + (ExitSpeed - 98) * 20.0 / 18.0)
         THEN 1 ELSE 0 END AS is_barrel,
    CASE WHEN PitchCall = 'InPlay'
              AND TaggedHitType IN ('GroundBall', 'FlyBall', 'LineDrive', 'Popup')
         THEN 1 ELSE 0 END AS is_bip,
    CASE WHEN PitchCall = 'InPlay' AND TaggedHitType = 'GroundBall' THEN 1 ELSE 0 END AS is_gb,
    CASE WHEN PitchCall = 'InPlay' AND TaggedHitType = 'LineDrive'  THEN 1 ELSE 0 END AS is_ld,
    CASE WHEN PitchCall = 'InPlay' AND TaggedHitType IN ('FlyBall', 'Popup') THEN 1 ELSE 0 END AS is_fb
"""


def _rv_join_sql(level: str, years_key: tuple[str, ...]) -> tuple[str, str]:
    """(JOIN clauses, aggregate exprs) computing REALIZED run value inside the aggregate pass.

    Three joins: the season state-transition artifacts (unioned — PitchUID is globally unique), then
    the 288-row RE matrix twice, for the state before and the state after. The matrix is joined on
    ``(Level, year)`` so every pitch is valued in THIS pool's level and its own season, matching the
    reports' Auto behaviour. Doing it in DuckDB keeps ~2M transition rows out of Python entirely.
    Emits null columns when no artifact is built yet."""
    from backend.models import rv_store
    yrs = years_key or tuple(data.years("pitcher"))
    paths = [str(rv_store._state_path(y)) for y in yrs if rv_store.state_exists(y)]
    lvl = (level or "").replace("'", "''")
    if not paths or not lvl or lvl == ALL:
        return "", "NULL::DOUBLE AS sum_rv, 0::BIGINT AS n_rv"
    lst = "[" + ", ".join(f"'{p}'" for p in paths) + "]"
    rem = str(rv_store.REM_PATH).replace("'", "''")
    joins = (
        f"LEFT JOIN read_parquet({lst}) AS st ON st.PitchUID = p.PitchUID "
        f"LEFT JOIN read_parquet('{rem}') AS reb "
        f"  ON reb.re288_state = st.re288_state AND reb.Level = '{lvl}' AND reb.year = p.Year "
        f"LEFT JOIN read_parquet('{rem}') AS rea "
        f"  ON rea.re288_state = st.next_re288_state AND rea.Level = '{lvl}' AND rea.year = p.Year")
    # COALESCE on the AFTER state only: a missing next state means the half-inning ended (RE=0),
    # whereas a missing BEFORE state means this pool's level has no matrix and RV is genuinely
    # unknown — count(reb...) then leaves n_rv at 0 instead of inventing a value.
    agg = ("sum(st.RunsScored + COALESCE(rea.run_expectancy, 0) - reb.run_expectancy) AS sum_rv, "
           "count(reb.run_expectancy)::BIGINT AS n_rv")
    return joins, agg


def _eye_join_sql(level: str, years_key: tuple[str, ...]) -> tuple[str, str]:
    """(JOIN clause, aggregate exprs) summing per-pitch EYE (swing/take decision runs) in the
    aggregate pass.

    Far simpler than RV's three joins: an eye score is already finished in the artifact, because its
    run environment is baked in at build time (see backend/models/eye_store). So this is one join on
    PitchUID. Only THIS level's artifacts are unioned — a D1 pitch is scored in both eye_D1_* and
    eye_P4_*, and mixing them would double-count it against two different league policies. Emits
    null columns when no artifact is built yet."""
    from backend.models import eye_store
    yrs = years_key or tuple(data.years("pitcher"))
    lvl = level or ""
    if not lvl or lvl == ALL:
        return "", "NULL::DOUBLE AS sum_eye, 0::BIGINT AS n_eye"
    paths = [str(eye_store._eye_path(lvl, y)) for y in yrs if eye_store.eye_exists(lvl, y)]
    if not paths:
        return "", "NULL::DOUBLE AS sum_eye, 0::BIGINT AS n_eye"
    lst = "[" + ", ".join(f"'{p}'" for p in paths) + "]"
    joins = f"LEFT JOIN read_parquet({lst}) AS ey ON ey.PitchUID = p.PitchUID"
    # count(ey.eye), not count(*): pitches with no competitive decision (HBP, intentional balls)
    # miss the join and must stay out of the denominator rather than counting as a 0.00 decision.
    agg = "sum(ey.eye) AS sum_eye, count(ey.eye)::BIGINT AS n_eye"
    return joins, agg


# Bump when the pool's COLUMN SET changes: a cached parquet written by an older build would be
# missing the new counters and every metric reading them would blow up at query time. The version
# lives in the filename so stale pools are simply never opened (and clear_pools still globs them).
_POOL_SCHEMA_VERSION = 2      # v2: + sum_eye / n_eye (batter eye metric)


def _pool_path(role: str, level: str, years_key: tuple[str, ...]) -> Path:
    yk = "-".join(years_key) if years_key else "all"
    return (CACHE_DIR /
            f"lb_{role}_{(level or ALL).replace(' ', '')}_{yk}_v{_POOL_SCHEMA_VERSION}.parquet")


def _scan_target(role: str, level: str, years_key: tuple[str, ...]) -> tuple[str, list[str], list]:
    """(glob list literal, WHERE fragments, bind params) for one level (+years), pruned to the
    partitions the picker index says actually hold those rows."""
    d = data.get_index(role)
    if level and level != ALL:
        d = data._filter_level(d, level)
    if years_key:
        d = d.filter(pl.col("Year").is_in(list(years_key)))
    parts = list(d.select("Part", "Year").unique().iter_rows())
    if not parts:
        return "", [], []
    globs = [str(WBASE / pt / yr / "**" / "*.parquet") for pt, yr in parts]
    glob_list = "[" + ", ".join("'" + g + "'" for g in globs) + "]"
    where, params = [], []
    if level and level != ALL:
        frag, p = data._level_where(level)
        where.append(frag)
        params += p
    if years_key:
        where.append("substr(Date, 1, 4) IN (" + ", ".join("?" * len(years_key)) + ")")
        params += list(years_key)
    return glob_list, where, params


def _xrv_sums(role: str, level: str, years_key: tuple[str, ...], glob_list: str,
              where: list[str], params: list) -> pl.DataFrame:
    """Per (Player, Team, PitchType) xRV totals for the pool.

    Resolved ONCE here, at build time — so no k-NN work ever happens on a leaderboard query. Values
    come from the cached ``xrv_{level}_{year}.parquet`` when it exists and are scored live otherwise
    (see ``data._xrv_for``). Balls use their own season's model for this level; a (level, year) with
    no trained artifact contributes nothing rather than borrowing another level's run environment."""
    r = data.ROLES[role]
    con = data._con()
    bb = con.execute(f"""
        SELECT {_key_sql(role)},
               PitchUID, substr(Date, 1, 4) AS Year, ExitSpeed, Angle, Direction
        FROM read_parquet({glob_list})
        WHERE PitchCall = 'InPlay' AND ExitSpeed IS NOT NULL AND Angle IS NOT NULL
              AND Direction IS NOT NULL AND PitchUID IS NOT NULL AND {r['player']} IS NOT NULL
              {(' AND ' + ' AND '.join(where)) if where else ''}
    """, params).pl()
    con.close()
    if bb.height == 0:
        return pl.DataFrame(schema={"Player": pl.Utf8, "Team": pl.Utf8, "PitchType": pl.Utf8,
                                    "n_xrv": pl.Int64, "sum_xrv": pl.Float64})
    scored = []
    for (yr,), grp in bb.partition_by(["Year"], as_dict=True).items():
        got = data._xrv_for(grp, level, yr)           # cache join, live-scoring only the misses
        if got is None:                               # model not trained for this (level, year)
            continue
        scored.append(got.select("Player", "Team", "PitchType", "xrv"))
    if not scored:
        return pl.DataFrame(schema={"Player": pl.Utf8, "Team": pl.Utf8, "PitchType": pl.Utf8,
                                    "n_xrv": pl.Int64, "sum_xrv": pl.Float64})
    return (pl.concat(scored).group_by(["Player", "Team", "PitchType"])
              .agg(pl.len().cast(pl.Int64).alias("n_xrv"), pl.col("xrv").sum().alias("sum_xrv")))


@functools.lru_cache(maxsize=16)
def pool(role: str, level: str, years_key: tuple[str, ...] = ()) -> pl.DataFrame:
    """Cached (Player, Team, PitchType) raw-count pool for one role + level (+years)."""
    path = _pool_path(role, level, years_key)
    if path.exists():
        return pl.read_parquet(path)
    glob_list, where, params = _scan_target(role, level, years_key)
    if not glob_list:
        return pl.DataFrame()
    r = data.ROLES[role]
    full_where = [f"{r['player']} IS NOT NULL", *where]
    rv_join, rv_agg = _rv_join_sql(level, years_key)
    eye_join, eye_agg = _eye_join_sql(level, years_key)
    con = data._con()
    df = con.execute(f"""
        WITH p AS (SELECT {_flags_sql(role)}, PitchUID, substr(Date, 1, 4) AS Year
                   FROM read_parquet({glob_list})
                   WHERE {' AND '.join(full_where)})
        SELECT Player, Team, PitchType, {_AGG_SQL}, {rv_agg}, {eye_agg}
        FROM p {rv_join} {eye_join}
        GROUP BY Player, Team, PitchType
    """, params).pl()
    con.close()
    if df.height == 0:
        return df
    df = df.join(_xrv_sums(role, level, years_key, glob_list, where, params),
                 on=["Player", "Team", "PitchType"], how="left")
    df = df.with_columns(pl.col("n_xrv").fill_null(0).cast(pl.Int64),
                         pl.col("sum_xrv").fill_null(0.0),
                         pl.col("n_eye").fill_null(0).cast(pl.Int64),
                         pl.col("sum_eye").fill_null(0.0),
                         pl.lit(level).alias("Level"))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    return df


def clear_pools() -> None:
    """Drop cached leaderboard pools (⟳ Rebuild index, so new games flow into the lookup)."""
    pool.cache_clear()
    for f in CACHE_DIR.glob("lb_*.parquet"):
        f.unlink(missing_ok=True)


# ── Metrics ──────────────────────────────────────────────────────────────────────────────────────
def _safe(num: str, den: str) -> pl.Expr:
    """num/den, null when the denominator is 0 — so an empty sample reads '—', never 0.0."""
    return pl.when(pl.col(den) > 0).then(pl.col(num) / pl.col(den)).otherwise(None)


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    expr: Callable[[], pl.Expr]
    fmt: str = "2f"                     # int | 0f | 1f | 2f | 3f | pct1 | avg3
    scoped: bool = True                 # meaningful when restricted to one pitch type?
    denom: str = "n_pitches"            # sample-size column gating a scoped condition
    roles: tuple[str, ...] = ("pitcher", "batter")
    blabel: str = ""                    # batter-facing label when it differs from the pitcher's
    group: str = "Results"

    def label_for(self, role: str) -> str:
        return self.blabel if (role == "batter" and self.blabel) else self.label


_M: list[Metric] = [
    # Sample size
    Metric("n_pitches", "Pitches", lambda: pl.col("n_pitches"), "int", denom="n_pitches",
           blabel="Pitches Seen", group="Sample"),
    Metric("usage", "Usage %", lambda: _safe("n_pitches", "total_pitches"), "pct1",
           blabel="Seen %", group="Sample"),
    Metric("pa", "Batters Faced", lambda: pl.col("pa"), "int", scoped=False, denom="pa",
           blabel="PA", group="Sample"),
    Metric("bbe", "Batted Balls", lambda: pl.col("n_ev"), "int", denom="n_ev", group="Sample"),

    # Pitch characteristics — for a batter these describe the pitches they saw
    Metric("velo", "Avg Velo", lambda: _safe("sum_velo", "n_velo"), "1f", denom="n_velo",
           group="Stuff"),
    Metric("max_velo", "Max Velo", lambda: pl.col("max_velo"), "1f", denom="n_velo", group="Stuff"),
    Metric("spin", "Avg Spin", lambda: _safe("sum_spin", "n_spin"), "0f", denom="n_spin",
           group="Stuff"),
    Metric("ivb", "Vert Break (IVB)", lambda: _safe("sum_ivb", "n_ivb"), "1f", denom="n_ivb",
           group="Stuff"),
    Metric("hb", "Horz Break", lambda: _safe("sum_hb", "n_hb"), "1f", denom="n_hb", group="Stuff"),
    Metric("hb_arm", "Arm-Side Break", lambda: _safe("sum_hb_arm", "n_hb"), "1f", denom="n_hb",
           group="Stuff"),
    Metric("ext", "Extension", lambda: _safe("sum_ext", "n_ext"), "2f", denom="n_ext",
           group="Stuff"),
    Metric("relh", "Release Height", lambda: _safe("sum_relh", "n_relh"), "2f", denom="n_relh",
           group="Stuff"),

    # Plate-appearance results — attributed to the pitch that ended the PA, so they are NOT
    # scope-able: PA is counted on the first pitch of the PA and the K on the last.
    Metric("k_pct", "K %", lambda: _safe("k", "pa"), "pct1", scoped=False, denom="pa"),
    Metric("bb_pct", "BB %", lambda: _safe("bb", "pa"), "pct1", scoped=False, denom="pa"),
    Metric("avg", "AVG", lambda: _safe("h", "ab"), "avg3", scoped=False, denom="ab",
           blabel="AVG", group="Slash"),
    Metric("obp", "OBP", lambda: _safe("ob", "ob_den"), "avg3", scoped=False, denom="ab",
           group="Slash"),
    Metric("slg", "SLG", lambda: _safe("tb", "ab"), "avg3", scoped=False, denom="ab",
           group="Slash"),
    Metric("ops", "OPS", lambda: (_safe("ob", "ob_den") + _safe("tb", "ab")), "avg3",
           scoped=False, denom="ab", group="Slash"),

    # Pitch-level rates — all scope-able
    Metric("strike_pct", "Strike %", lambda: _safe("strikes", "n_pitches"), "pct1"),
    Metric("cs_pct", "Called Strike %", lambda: _safe("called_strikes", "n_pitches"), "pct1"),
    Metric("cs_take_pct", "Called Strike % (of takes)",
           lambda: _safe("called_strikes", "takes"), "pct1", denom="takes"),
    Metric("csw_pct", "CSW %",
           lambda: pl.when(pl.col("n_pitches") > 0)
                     .then((pl.col("called_strikes") + pl.col("whiffs")) / pl.col("n_pitches"))
                     .otherwise(None), "pct1"),
    Metric("zone_pct", "Zone %", lambda: _safe("zone_n", "loc_n"), "pct1", denom="loc_n"),
    Metric("swing_pct", "Swing %", lambda: _safe("swings", "n_pitches"), "pct1"),
    Metric("whiff_pct", "Whiff %", lambda: _safe("whiffs", "swings"), "pct1", denom="swings"),
    Metric("contact_pct", "Contact %",
           lambda: pl.when(pl.col("swings") > 0)
                     .then((pl.col("swings") - pl.col("whiffs")) / pl.col("swings"))
                     .otherwise(None), "pct1", denom="swings"),
    Metric("chase_pct", "Chase %", lambda: _safe("oz_swings", "oz_n"), "pct1", denom="oz_n"),
    Metric("iz_whiff_pct", "In-Zone Whiff %", lambda: _safe("iz_whiffs", "iz_swings"), "pct1",
           denom="iz_swings"),
    # Swing/take DECISION value: runs added versus the league's average decision on the same pitch
    # in the same count. Batter-only — it is a hitter's plate-discipline skill, and a pitcher-facing
    # reading ("how well did hitters choose against me") would be a different metric.
    # Deliberately contact-agnostic: every swing is valued at the LEAGUE-average result of swinging
    # there, so this measures the eye, never the bat — that is what makes it complement xRV/BBE
    # rather than duplicate it.
    Metric("eye100", "Eye / 100 pitches",
           lambda: pl.when(pl.col("n_eye") > 0)
                     .then(pl.col("sum_eye") / pl.col("n_eye") * 100).otherwise(None),
           "signed2", denom="n_eye", roles=("batter",), blabel="Eye / 100 pitches"),

    # Contact quality
    Metric("ev", "Avg EV", lambda: _safe("sum_ev", "n_ev"), "1f", denom="n_ev", group="Contact"),
    Metric("max_ev", "Max EV", lambda: pl.col("max_ev"), "1f", denom="n_ev", group="Contact"),
    Metric("la", "Avg LA", lambda: _safe("sum_la", "n_la"), "1f", denom="n_la", group="Contact"),
    Metric("hardhit_pct", "Hard Hit %", lambda: _safe("hardhit", "n_ev"), "pct1", denom="n_ev",
           group="Contact"),
    Metric("barrel_pct", "Barrel %", lambda: _safe("barrels", "n_ev"), "pct1", denom="n_ev",
           group="Contact"),
    Metric("gb_pct", "GB %", lambda: _safe("gb", "bip"), "pct1", denom="bip", group="Contact"),
    Metric("ld_pct", "LD %", lambda: _safe("ld", "bip"), "pct1", denom="bip", group="Contact"),
    Metric("fb_pct", "FB %", lambda: _safe("fb", "bip"), "pct1", denom="bip", group="Contact"),
    # Realized run value, offense-positive for both roles (matches xRV, so the pair reads as
    # actual-vs-expected). A pitcher WANTS this negative.
    Metric("rv100", "RV / 100 pitches",
           lambda: pl.when(pl.col("n_rv") > 0)
                     .then(pl.col("sum_rv") / pl.col("n_rv") * 100).otherwise(None),
           "signed2", denom="n_rv", group="Contact"),
    Metric("rv", "RV (total runs)", lambda: pl.col("sum_rv"), "signed1",
           denom="n_rv", group="Contact"),
    Metric("xrv", "xRV / BBE", lambda: _safe("sum_xrv", "n_xrv"), "3f", denom="n_xrv",
           group="Contact"),
]

METRICS: dict[str, Metric] = {m.key: m for m in _M}

def metric_options(role: str) -> list[dict]:
    """Dropdown options grouped by section, in registry order."""
    return [{"label": f"{m.group} · {m.label_for(role)}", "value": m.key}
            for m in _M if role in m.roles]


def scoped_metric_keys() -> set[str]:
    return {m.key for m in _M if m.scoped}


# ── Roll-up + rates ──────────────────────────────────────────────────────────────────────────────
_SUM_SKIP = {"Player", "Team", "PitchType", "Level", "max_velo", "max_ev"}


def _rollup(p: pl.DataFrame) -> pl.DataFrame:
    """Sum a (Player, Team, PitchType) pool to one row per player. Team becomes the team the player
    threw/saw the most pitches for, so a transfer shows their primary school instead of duplicating."""
    if p.height == 0:
        return p
    sums = [pl.col(c).sum().alias(c) for c in p.columns if c not in _SUM_SKIP]
    maxes = [pl.col(c).max().alias(c) for c in ("max_velo", "max_ev") if c in p.columns]
    by_team = (p.group_by(["Player", "Team"]).agg(pl.col("n_pitches").sum().alias("_n"))
                 .sort(["Player", "_n"], descending=[False, True])
                 .group_by("Player", maintain_order=True).first().select("Player", "Team"))
    return p.group_by("Player").agg(*sums, *maxes).join(by_team, on="Player", how="left")


def _rates(df: pl.DataFrame, keys, role: str) -> pl.DataFrame:
    """Divide the requested rate metrics out of the summed raw columns."""
    if df.height == 0:
        return df
    df = df.with_columns(
        (pl.col("singles") + pl.col("doubles") + pl.col("triples") + pl.col("hr")).alias("h"),
        (pl.col("pa") - pl.col("bb") - pl.col("hbp") - pl.col("sac")).alias("ab"),
        (pl.col("singles") + 2 * pl.col("doubles") + 3 * pl.col("triples")
         + 4 * pl.col("hr")).alias("tb"),
    )
    # AB/OBP denominators follow report.build_batter_summary exactly, so a player's Lookup line
    # and their report agree.
    df = df.with_columns((pl.col("h") + pl.col("bb") + pl.col("hbp")).alias("ob"),
                         (pl.col("ab") + pl.col("bb") + pl.col("hbp") + pl.col("sac")).alias("ob_den"))
    want = [k for k in keys if k in METRICS and role in METRICS[k].roles]
    return df.with_columns([METRICS[k].expr().alias(k) for k in want])


def _scoped_frame(p: pl.DataFrame, scope: str, role: str, keys) -> pl.DataFrame:
    """Roll the pool up over one pitch-type scope and compute ``keys`` there. ``total_pitches`` is
    carried across so Usage% (scope share of the player's total) is computable."""
    totals = p.group_by("Player").agg(pl.col("n_pitches").sum().alias("total_pitches"))
    types = scope_types(scope)
    sub = p if types is None else p.filter(pl.col("PitchType").is_in(types))
    out = _rollup(sub).join(totals, on="Player", how="left")
    return _rates(out, keys, role)


@dataclass
class Condition:
    metric: str
    op: str                     # ">" | "<" | ">=" | "<=" | "between"
    value: float | None
    value2: float | None = None
    scope: str = SCOPE_ANY
    min_n: int = DEFAULT_MIN_SCOPED

    def label(self, role: str) -> str:
        m = METRICS.get(self.metric)
        if m is None:
            return ""
        where = "" if self.scope == SCOPE_ANY else f" [{self.scope}]"
        v = f"{self.value}–{self.value2}" if self.op == "between" else f"{self.op} {self.value}"
        return f"{m.label_for(role)}{where} {v}"

    def column(self) -> str:
        """Column name this condition contributes to the results table when it is scoped."""
        return f"{self.metric}__{self.scope}"


def scale_input(metric_key: str, v):
    """UI number -> engine units. Percent metrics are typed as ``70`` and compared as ``0.70``, so
    the form never asks anyone to enter a decimal fraction."""
    m = METRICS.get(metric_key)
    if m is None or v is None:
        return v
    try:
        return float(v) / 100.0 if m.fmt == "pct1" else float(v)
    except (TypeError, ValueError):
        return None


def _predicate(c: Condition) -> pl.Expr:
    col = pl.col(c.metric)
    if c.op == "between":
        lo, hi = sorted([c.value, c.value2])
        return col.is_between(lo, hi)
    return {">": col > c.value, "<": col < c.value,
            ">=": col >= c.value, "<=": col <= c.value}[c.op]


@dataclass
class Result:
    frame: pl.DataFrame
    total: int
    columns: list[str]                          # metric keys, in display order
    labels: dict[str, str] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


def query(role: str, levels, years_sel=None, conf=ALL, team=ALL,
          min_pitches: int = DEFAULT_MIN_PITCHES, conditions: list[Condition] | None = None,
          display: list[str] | None = None, sort_by: str | None = None, descending: bool = True,
          limit: int = 100) -> Result:
    """Run a leaderboard query. Every level's pool is read from cache (built on first use), the
    pools are concatenated, and all filtering happens in Polars."""
    conditions = [c for c in (conditions or []) if c.metric in METRICS and c.value is not None]
    # A metric that can't be scoped (K%, slash line — PA-denominated) always evaluates over
    # everything, whatever the form happens to have left in its scope box.
    conditions = [c if METRICS[c.metric].scoped else Condition(c.metric, c.op, c.value, c.value2,
                                                               SCOPE_ANY, c.min_n)
                  for c in conditions]
    years_key = tuple(sorted(years_sel)) if years_sel else ()
    levels = [l for l in (levels or []) if l]
    parts = [p for p in (pool(role, lv, years_key) for lv in levels) if p.height]
    if not parts:
        return Result(pl.DataFrame(), 0, [], {}, ["No data for the selected level(s) and year(s)."])
    p = pl.concat(parts, how="vertical_relaxed")

    diagnostics: list[str] = []
    # Conference / team filters come off the team acronym map, the same source the picker uses.
    if conf and conf != ALL:
        conf_map = data.team_maps()[1]
        keep = [a for a, c in conf_map.items() if c == conf]
        p = p.filter(pl.col("Team").is_in(keep))
    if team and team != ALL:
        p = p.filter(pl.col("Team") == team)
    if p.height == 0:
        return Result(pl.DataFrame(), 0, [], {}, ["No players in that conference/team selection."])

    display = list(display or default_columns(role))
    base_keys = set(display) | {c.metric for c in conditions if c.scope == SCOPE_ANY} | {"n_pitches"}
    if sort_by:
        base_keys.add(sort_by)
    board = _scoped_frame(p, SCOPE_ANY, role, base_keys)
    board = board.filter(pl.col("n_pitches") >= (min_pitches or 0))
    if board.height == 0:
        return Result(pl.DataFrame(), 0, [], {},
                      [f"No {role}s reach {min_pitches:,} pitches in this selection."])

    # Each condition is evaluated on its own scope's roll-up, then intersected on Player. Scoped
    # conditions also contribute their column to the table, so a row shows WHY it qualified.
    extra_cols: list[str] = []
    labels: dict[str, str] = {}
    for c in conditions:
        sf = _scoped_frame(p, c.scope, role, {c.metric})
        m = METRICS[c.metric]
        sf = sf.filter(pl.col(m.denom) >= (c.min_n or 0)) if c.scope != SCOPE_ANY else sf
        passed = sf.filter(_predicate(c))
        eligible = board.select("Player")            # who was still standing before this condition
        if c.scope == SCOPE_ANY:
            board = board.join(passed.select("Player"), on="Player", how="semi")
        else:
            col = c.column()
            board = board.join(passed.select("Player", pl.col(c.metric).alias(col)),
                               on="Player", how="inner")
            if col not in extra_cols:
                extra_cols.append(col)
                labels[col] = f"{m.label_for(role)} ({c.scope})"
        if board.height == 0:
            # Report the range that was actually ACHIEVABLE here — restricted to the players who
            # survived the qualifier and every earlier condition. Quoting the unfiltered range would
            # advertise a threshold that a 12-pitch sample reached and nobody real can.
            vals = (sf.join(eligible, on="Player", how="semi")[c.metric].drop_nulls()
                    if c.metric in sf.columns else pl.Series([], dtype=pl.Float64))
            hint = (f" Best among the {eligible.height:,} who qualify: "
                    f"{fmt_value(vals.max(), m.fmt)} (median {fmt_value(vals.median(), m.fmt)}, "
                    f"low {fmt_value(vals.min(), m.fmt)})." if vals.len() else "")
            diagnostics.append(f"No one clears “{c.label(role)}”.{hint}")
            return Result(pl.DataFrame(), 0, [], {}, diagnostics)

    total = board.height
    cols = display + [c for c in extra_cols if c not in display]
    sort_col = sort_by if (sort_by and sort_by in board.columns) else "n_pitches"
    board = board.sort(sort_col, descending=descending, nulls_last=True).head(limit)
    labels.update({k: METRICS[k].label_for(role) for k in cols if k in METRICS})
    return Result(board, total, cols, labels, diagnostics)


DEFAULT_PITCHER_COLS = ["n_pitches", "velo", "ivb", "hb_arm", "csw_pct", "whiff_pct", "chase_pct",
                        "k_pct", "bb_pct", "hardhit_pct", "ev", "rv100", "xrv"]
DEFAULT_BATTER_COLS = ["n_pitches", "pa", "avg", "obp", "slg", "ops", "k_pct", "bb_pct",
                       "swing_pct", "whiff_pct", "chase_pct", "hardhit_pct", "ev", "rv100",
                       "eye100", "xrv"]


def default_columns(role: str) -> list[str]:
    return list(DEFAULT_BATTER_COLS if role == "batter" else DEFAULT_PITCHER_COLS)


# ── Formatting ───────────────────────────────────────────────────────────────────────────────────
def fmt_value(v, fmt: str) -> str:
    if v is None:
        return "—"
    try:
        if fmt == "int":
            return f"{int(v):,}"
        if fmt == "pct1":
            return f"{v * 100:.1f}%"
        if fmt.startswith("signed"):            # run value: the sign IS the reading
            return f"{v:+.{int(fmt[-1])}f}"
        if fmt == "avg3":
            s = f"{v:.3f}"
            return s[1:] if s.startswith("0.") else s          # .312, not 0.312
        return f"{v:.{int(fmt[0])}f}"
    except (TypeError, ValueError):
        return "—"


def format_board(res: Result, role: str) -> pl.DataFrame:
    """Result frame -> a display frame of strings (Rank, Player, School, then the metric columns)."""
    if res.frame.height == 0:
        return pl.DataFrame()
    out = {"#": [str(i + 1) for i in range(res.frame.height)],
           "Player": [(v or "").strip() for v in res.frame["Player"]],
           "School": [data.team_label(v) for v in res.frame["Team"]]}
    for key in res.columns:
        base = key.split("__")[0]
        m = METRICS.get(base)
        if key not in res.frame.columns or m is None:
            continue
        out[res.labels.get(key, m.label_for(role))] = [fmt_value(v, m.fmt) for v in res.frame[key]]
    return pl.DataFrame(out)
