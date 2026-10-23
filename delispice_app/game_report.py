"""Game Reports: one player's game, or the games in a date range (see game_report_decisions.md).

The data and the report pieces, no Dash: app.py lays out the tab and wires its callbacks. Everything
reads the served pitch data the same way the Pitchers tab does, so a game report's rows come with the
retags, RV, xRV and Stuff+ already attached (``data.get_rows``), then get filtered to the chosen games.

The picker narrows Level -> Conference -> Team -> date -> player: ``players_on`` lists who played in
games that started on the typed date (or range). Then ``player_games`` lists every game the picked name
appears in -> ``players`` splits that into TrackMan player IDs (one name can be several players: "Lynch,
Ryan" is three) -> ``games_for`` keeps one player's games (they feed the calendar and the file list) ->
``choose_games`` turns the typed Start / End dates (and a doubleheader pick) into the games to report ->
``game_rows`` fetches those games' pitches -> the table and figure builders below.

An uploaded file (decisions §3) takes the same path from ``choose_games`` on: ``read_upload`` cleans it
by column name, ``put_upload`` keeps it for that browser session only, and ``upload_players`` /
``upload_games`` / ``upload_rows`` stand in for the served-data lookups.
"""
from __future__ import annotations

import functools
import io
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import PurePosixPath

import plotly.graph_objects as go
import polars as pl

from . import data, report

ALL = data.ALL
SMALL_SAMPLE = 5          # a pitch type thrown fewer times than this gets blank rates in the arsenal tables
BOTH = "both"             # the doubleheader pick that reports both games

ID_COL = {"pitcher": "PitcherId", "batter": "BatterId"}          # TrackMan's player ID per role
HAND_COL = {"pitcher": "PitcherThrows", "batter": "BatterSide"}
_GAME_SCHEMA = {"GameID": pl.Utf8, "pid": pl.Utf8, "game_date": pl.Utf8, "first_time": pl.Utf8,
                "team": pl.Utf8, "hand": pl.Utf8, "home": pl.Utf8, "away": pl.Utf8, "stadium": pl.Utf8,
                "level": pl.Utf8, "source_file": pl.Utf8, "verified": pl.Boolean, "pitches": pl.Int64}


# ── The player's games ───────────────────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=64)
def player_games(role: str, player: str, level: str = ALL, team: str = ALL) -> pl.DataFrame:
    """One row per (game, TrackMan player ID) the name appears in, every season, oldest first.

    ``game_date`` is the first pitch's date (a game that ran past midnight carries two dates). ``side``
    is "vs" at home and "@" away, from the player's team in that game. Pick one player with
    ``games_for`` before using the games: a name can belong to several players."""
    r = data.ROLES[role]
    parts = data._partitions(role, player, level, team, None)
    if not parts:
        return _finish(pl.DataFrame(schema=_GAME_SCHEMA))
    glob_list = "[" + ", ".join(f"'{data.WBASE / pt / yr / '**' / '*.parquet'}'" for pt, yr in parts) + "]"
    where, params = [f"{r['player']} = ?"], [player]
    if level and level != ALL:
        frag, p = data._level_where(level)
        where.append(frag)
        params += p
    if team and team != ALL:
        where.append(f"{r['team']} = ?")
        params.append(team)
    con = data._con()
    # The game's date and start come from its FIRST pitch, whoever threw it: a reliever who only pitched
    # after midnight would otherwise move a late game to the next day (and fake a doubleheader).
    games = con.execute(f"""
        WITH mine AS (
          SELECT GameID, COALESCE({ID_COL[role]}, '') AS pid,
                 mode({r['team']}) AS team, mode({HAND_COL[role]}) AS hand,
                 any_value(HomeTeam) AS home, any_value(AwayTeam) AS away, any_value(Stadium) AS stadium,
                 any_value(Level) AS level, any_value(source_file) AS source_file,
                 bool_and(is_verified) AS verified, count(*) AS pitches
          FROM read_parquet({glob_list}) WHERE {' AND '.join(where)} GROUP BY GameID, pid),
        starts AS (
          SELECT GameID, min(Date || ' ' || COALESCE(Time, '')) AS first_pitch
          FROM read_parquet({glob_list}) WHERE GameID IN (SELECT GameID FROM mine) GROUP BY GameID)
        SELECT mine.*, substr(first_pitch, 1, 10) AS game_date, substr(first_pitch, 12) AS first_time
        FROM mine JOIN starts USING (GameID)""", params).pl()
    con.close()
    return _finish(games)


def _finish(games: pl.DataFrame) -> pl.DataFrame:
    at_home = pl.col("team") == pl.col("home")
    return (games.sort("game_date", "first_time", "GameID", nulls_last=True)
                 .with_columns(pl.when(at_home).then(pl.lit("vs")).otherwise(pl.lit("@")).alias("side"),
                               pl.when(at_home).then(pl.col("away")).otherwise(pl.col("home")).alias("opponent"),
                               pl.col("source_file").map_elements(lambda p: PurePosixPath(p).name if p else "",
                                                                  return_dtype=pl.Utf8).alias("file")))


_HAND = {"Right": "R", "Left": "L", "Switch": "S"}


def players(games: pl.DataFrame, role: str) -> pl.DataFrame:
    """The TrackMan players behind one name, most recent first: pid, label, games, last game. Usually
    one row; "Lynch, Ryan" is three pitchers (Notre Dame LHP, Coastal Carolina RHP, North Carolina RHP)."""
    if games.height == 0:
        return pl.DataFrame(schema={"pid": pl.Utf8, "label": pl.Utf8, "games": pl.UInt32, "last": pl.Utf8})
    p = (games.sort("game_date").group_by("pid")
              .agg(pl.len().alias("games"), pl.col("game_date").min().alias("first"),
                   pl.col("game_date").max().alias("last"), pl.col("team").last(), pl.col("hand").mode().first())
              .sort("last", descending=True))
    def label(r):
        hand = _HAND.get(r["hand"] or "", "?") + ("HP" if role == "pitcher" else "")
        return (f"{data.team_label(r['team'])} · {hand} · {r['games']} game{'s' if r['games'] != 1 else ''}"
                f" ({fmt_date(r['first'])} – {fmt_date(r['last'])})")
    return p.with_columns(pl.struct(pl.all()).map_elements(label, return_dtype=pl.Utf8).alias("label")
                          ).select("pid", "label", "games", "last")


def played_between(ps: pl.DataFrame, games: pl.DataFrame, start: str, end: str | None) -> pl.DataFrame:
    """``players`` rows for the IDs with a game that started between start and end (all of them if none
    did, so the choice never comes up empty)."""
    played = games.filter(pl.col("game_date").is_between(pl.lit(start), pl.lit(end or start)))["pid"].unique()
    return ps.filter(pl.col("pid").is_in(played.to_list())) if played.len() else ps


def games_for(games: pl.DataFrame, pid: str | None) -> pl.DataFrame:
    """One player's games, with ``game_no`` 1 / 2 when that player has two games on a date (a
    doubleheader), ordered by first pitch."""
    g = games.filter(pl.col("pid") == pid) if pid is not None else games
    return g.with_columns(pl.int_range(1, pl.len() + 1).over("game_date").alias("game_no"),
                          pl.len().over("game_date").alias("games_that_day"))


@functools.lru_cache(maxsize=1)
def latest_game_date() -> str | None:
    """The newest served game's date: where the calendars open before a player is picked."""
    con = data._con()
    d = con.execute(f"SELECT max(Date) FROM read_parquet('{data.GLOB_ALL}')").fetchone()[0]
    con.close()
    return d


@functools.lru_cache(maxsize=128)
def players_on(role: str, start: str, end: str | None, level: str = ALL, team: str = ALL) -> frozenset[str]:
    """Names of the players in games that STARTED between ``start`` and ``end`` (ISO dates; the game's
    first pitch, as everywhere in the tab), under the level / team filters. It narrows the player list."""
    r = data.ROLES[role]
    end = end or start
    if end < start:
        return frozenset()
    globs = [str(d / "**" / "*.parquet") for y in range(int(start[:4]), int(end[:4]) + 1)
             for d in sorted(data.WBASE.glob(f"*/{y}")) if d.is_dir()]
    if not globs:
        return frozenset()
    glob_list = "[" + ", ".join(f"'{g}'" for g in globs) + "]"
    # Rows from the day before / after too: a game that started on the 4th can have pitches dated the 5th
    # (past midnight), and one from the 3rd can spill into the 4th; min(Date) then tells them apart.
    lo = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    hi = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    where, params = [f"{r['player']} IS NOT NULL", "Date BETWEEN ? AND ?"], [lo, hi]
    if level and level != ALL:
        frag, p = data._level_where(level)
        where.append(frag)
        params += p
    if team and team != ALL:
        where.append(f"{r['team']} = ?")
        params.append(team)
    con = data._con()
    names = con.execute(f"""
        WITH g AS (SELECT GameID FROM read_parquet({glob_list}) WHERE Date BETWEEN ? AND ?
                   GROUP BY GameID HAVING min(Date) BETWEEN ? AND ?)
        SELECT DISTINCT {r['player']} FROM read_parquet({glob_list})
        WHERE GameID IN (SELECT GameID FROM g) AND {' AND '.join(where)}""",
        [lo, hi, start, end, *params]).fetchall()
    con.close()
    return frozenset(n for (n,) in names)


def fmt_date(iso: str | None) -> str:
    """'2026-05-08' -> '05/08/2026' (the format the date fields use)."""
    return f"{iso[5:7]}/{iso[8:10]}/{iso[:4]}" if iso else ""


def vs_text(g: dict) -> str:
    """'vs Arkansas Razorbacks' / '@ Arkansas Razorbacks', or '' when the game has no opponent on file
    (an uploaded practice session)."""
    return f"{g['side']} {data.team_label(g['opponent'])}" if g.get("opponent") else ""


def game_label(g: dict) -> str:
    """'05/08/2026 · vs Arkansas · G2' for one row of ``player_games``."""
    bits = [fmt_date(g["game_date"]), vs_text(g)]
    if g["games_that_day"] > 1:
        bits.append(f"G{g['game_no']}")
    return " · ".join(b for b in bits if b)


def choose_games(games: pl.DataFrame, start: str | None, end: str | None,
                 pick: str | None) -> tuple[pl.DataFrame, str | None, list[dict]]:
    """The games to report for typed Start / End dates (ISO strings), as ``(games, message, dh_options)``.

    End is optional: empty, or the same as Start, means that one date. When there's nothing to report
    yet, ``games`` is empty and ``message`` says why. A single date with two games returns the
    doubleheader choices in ``dh_options`` and waits for ``pick`` (a GameID, or BOTH)."""
    empty = games.clear()
    if not start:
        return empty, "Type a date (MM/DD/YYYY) or pick one on the calendar. End is optional.", []
    end = end or start
    if end < start:
        return empty, "The End date is before the Start date.", []
    sel = games.filter(pl.col("game_date").is_between(pl.lit(start), pl.lit(end)))
    if sel.height == 0:
        span = f"on {fmt_date(start)}" if start == end else f"between {fmt_date(start)} and {fmt_date(end)}"
        before = games.filter(pl.col("game_date") < start)["game_date"].max()
        after = games.filter(pl.col("game_date") > end)["game_date"].min()
        near = [f"before: {fmt_date(before)}" if before else "", f"after: {fmt_date(after)}" if after else ""]
        near = " · ".join(n for n in near if n)
        return empty, f"No games {span}." + (f" Nearest games, {near}." if near else ""), []
    if start == end and sel.height > 1:
        options = [{"label": f"G{g['game_no']} ({g['first_time'][:5] if g['first_time'] else '?'}"
                             + (f", {vs_text(g)})" if vs_text(g) else ")"), "value": g["GameID"]}
                   for g in sel.iter_rows(named=True)] + [{"label": "Both", "value": BOTH}]
        if pick == BOTH:
            return sel, None, options
        if pick in sel["GameID"].to_list():
            return sel.filter(pl.col("GameID") == pick), None, options
        return empty, f"{fmt_date(start)} has {sel.height} games: pick one.", options
    return sel, None, []


def game_rows(role: str, player: str, level: str, team: str, games: pl.DataFrame, pid: str | None,
              re_level: str | None = None, re_year: str | None = None) -> pl.DataFrame:
    """The chosen games' pitches by this player (by TrackMan ID, so a same-named player in the same
    game stays out), scored like the Pitchers tab's rows (retags, RV, xRV, Stuff+). ``re_level`` /
    ``re_year`` value xRV and RV in another run environment, as the Pitchers tab's picker does (None =
    each pitch's own level and season)."""
    years = sorted({d[:4] for d in games["game_date"].to_list()})
    rows = (data.get_rows(role, player, level, team, years, re_level, re_year)
                .filter(pl.col("GameID").is_in(games["GameID"].to_list())))
    if pid:
        rows = rows.filter(pl.col(ID_COL[role]) == pid)
    return rows


# ── Header pieces ────────────────────────────────────────────────────────────────────────────────
def date_span(games: pl.DataFrame) -> str:
    """'05/08/2026' for one date, '03/01/2026 – 03/31/2026 · 6 games' for a range."""
    d0, d1 = games["game_date"].min(), games["game_date"].max()
    if games.height == 1:
        return fmt_date(d0)
    span = fmt_date(d0) if d0 == d1 else f"{fmt_date(d0)} – {fmt_date(d1)}"
    return f"{span} · {games.height} games"


def unverified_note(games: pl.DataFrame) -> str | None:
    """The disclaimer when any chosen game is still its unverified version, else None."""
    n = int((~games["verified"]).sum())
    if not n:
        return None
    tail = "TrackMan hasn't sent the verified file yet. Names, pitch types and results may still be corrected."
    if games.height == 1:
        return f"Unverified game: {tail}"
    return f"{n} of {games.height} games are unverified (marked below): {tail}"


def files_table(games: pl.DataFrame, status: str | None = None) -> pl.DataFrame:
    """The file-name area: one row per chosen game, so the user can confirm it's the right game.
    ``status`` overrides Verified / Unverified (an uploaded file says "Uploaded")."""
    return pl.DataFrame([{"Date": fmt_date(g["game_date"]) + (f" · G{g['game_no']}" if g["games_that_day"] > 1 else ""),
                          "Opponent": vs_text(g),
                          "Stadium": g["stadium"] or "", "File": g["file"],
                          "Status": status or ("Verified" if g["verified"] else "Unverified")}
                         for g in games.iter_rows(named=True)],
                        schema={c: pl.Utf8 for c in ("Date", "Opponent", "Stadium", "File", "Status")})


# ── Tables ───────────────────────────────────────────────────────────────────────────────────────
LINE_COLS = ["IP", "NP", "Strike %", "Zone %", "P/PA", "TBF", "Batters Retired", "BB + HBP", "SO", "Hits",
             "Extension", "K%", "BB%"]


def _line(rows: pl.DataFrame) -> dict:
    s = report.build_summary(rows).row(0, named=True)
    located = rows.filter(report.HAS_LOC)
    zone = located.select(report.IN_ZONE.mean()).item() if located.height else None
    # Strike % = called + swinging strikes, fouls and balls in play over all pitches (report.STRIKE_CALLS,
    # the Pitchers tab's definition, so the two tabs agree).
    strike = rows.select(pl.col("PitchCall").is_in(report.STRIKE_CALLS).mean()).item() if rows.height else None
    return {"IP": s["IP"], "NP": s["NP"], "Strike %": report.pct(strike), "Zone %": report.pct(zone),
            "P/PA": report.num(s["NP"] / s["TBF"]) if s["TBF"] else "", "TBF": s["TBF"],
            "Batters Retired": s["Batters Retired"], "BB + HBP": s["BB + HBP"], "SO": s["SO"],
            "Hits": s["Hits"], "Extension": s["Extension"], "K%": s["K%"], "BB%": s["BB%"]}


# What each number needs from the file: an uploaded file without these columns gets the number blanked
# (served data always has them). Location = PlateLocSide + PlateLocHeight.
_LOC = ["PlateLocSide", "PlateLocHeight"]
LINE_NEEDS = {"IP": ["OutsOnPlay", "KorBB"], "Strike %": ["PitchCall"], "Zone %": _LOC, "P/PA": ["PitchofPA"], "TBF": ["PitchofPA"],
              "Batters Retired": ["KorBB", "PlayResult"], "BB + HBP": ["KorBB", "PitchCall"], "SO": ["KorBB"],
              "Hits": ["PlayResult"], "Extension": ["Extension"], "K%": ["KorBB", "PitchofPA"],
              "BB%": ["KorBB", "PitchofPA"]}
ARSENAL_NEEDS = {"Avg Velo": ["RelSpeed"], "Max Velo": ["RelSpeed"], "Avg Spin": ["SpinRate"],
                 "Strike %": ["PitchCall"], "Zone %": _LOC,
                 "WHIFF %": ["PitchCall"], "Vert Break": ["InducedVertBreak"], "Horz Break": ["HorzBreak"],
                 "Tilt": ["SpinAxis"], "Extension": ["Extension"], "Arm Angle": ["RelHeight", "RelSide"]}
CONTACT_NEEDS = {"Hard Hit %": ["ExitSpeed"], "Avg EV": ["ExitSpeed"]}


def _blank_missing(df: pl.DataFrame, present: set[str] | None, needs: dict[str, list[str]]) -> pl.DataFrame:
    """Blank every column whose source columns aren't in the file (``present`` None = served data)."""
    if present is None:
        return df
    gone = [c for c, need in needs.items() if c in df.columns and not set(need) <= present]
    return df.with_columns([pl.lit("").alias(c) for c in gone]) if gone else df


def line_table(rows: pl.DataFrame, present: set[str] | None = None) -> pl.DataFrame:
    """The one-row line at the top (totals, for a date range)."""
    return _blank_missing(pl.DataFrame([_line(rows)]).cast(pl.Utf8), present, LINE_NEEDS)


def per_game_table(rows: pl.DataFrame, games: pl.DataFrame, present: set[str] | None = None) -> pl.DataFrame:
    """For a date range: the same line, one row per game, oldest first."""
    out = []
    for g in games.iter_rows(named=True):
        gr = rows.filter(pl.col("GameID") == g["GameID"])
        if gr.height:
            out.append({"Date": fmt_date(g["game_date"]) + (f" · G{g['game_no']}" if g["games_that_day"] > 1 else ""),
                        "Opponent": vs_text(g) + ("" if g["verified"] else " (unverified)"),
                        **_line(gr)})
    return _blank_missing(pl.DataFrame(out).cast(pl.Utf8), present, LINE_NEEDS) if out else pl.DataFrame()


def _pitch_counts(rows: pl.DataFrame) -> dict[str, int]:
    """Display pitch name -> pitches thrown (the arsenal's rows)."""
    t = rows.filter(pl.col("TaggedPitchType").is_not_null() & ~pl.col("TaggedPitchType").is_in(report.NON_PITCHES))
    return {report.PITCH_NAMES.get(k, k): n for k, n in t.group_by("TaggedPitchType").len().iter_rows()}


def _blank_small(df: pl.DataFrame, counts: dict[str, int], cols: list[str]) -> pl.DataFrame:
    """Blank the rate columns of pitch types thrown fewer than SMALL_SAMPLE times."""
    small = [p for p, n in counts.items() if n < SMALL_SAMPLE]
    cols = [c for c in cols if c in df.columns]
    if not small or not cols:
        return df
    return df.with_columns([pl.when(pl.col("Pitch").is_in(small)).then(pl.lit("")).otherwise(pl.col(c)).alias(c)
                            for c in cols])


def arm_angles(rows: pl.DataFrame, height_in: int | None) -> dict[str, float]:
    """Display pitch name -> average arm angle (degrees from horizontal) of its pitches, the same
    estimate as the release chart: shoulder at 70% of height, atan(rise above it / |RelSide|)."""
    if not height_in:
        return {}
    sh = report.SHOULDER_FRAC * height_in / 12
    t = (rows.filter(pl.col("TaggedPitchType").is_not_null() & ~pl.col("TaggedPitchType").is_in(report.NON_PITCHES)
                     & pl.col("RelSide").is_not_null() & pl.col("RelHeight").is_not_null())
             .with_columns(pl.arctan2(pl.col("RelHeight") - sh, pl.col("RelSide").abs()).degrees().alias("arm")))
    return {report.PITCH_NAMES.get(k, k): a for k, a in t.group_by("TaggedPitchType").agg(pl.col("arm").mean()).iter_rows()}


def arsenal_tables(rows: pl.DataFrame, height_in: int | None,
                   present: set[str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame, list[dict]]:
    """(arsenal, Contact table, Stuff rows), same rows and order as the Pitchers tab's: the arsenal with
    the Arm Angle after Extension; rates blank for pitch types under SMALL_SAMPLE pitches."""
    counts = _pitch_counts(rows)
    ars = report.build_arsenal(rows)
    if ars.height:
        arms = arm_angles(rows, height_in)
        ars = ars.with_columns(
            pl.col("Pitch").map_elements(lambda p: f"{arms[p]:.0f}°" if p in arms else "",
                                         return_dtype=pl.Utf8).alias("Arm Angle"))
        cols = ars.columns
        at = cols.index("Extension") + 1
        ars = ars.select([*cols[:at], "Arm Angle", *[c for c in cols[at:] if c != "Arm Angle"]])
        ars = _blank_missing(_blank_small(ars, counts, ["Strike %", "Zone %", "WHIFF %", "RV/100"]),
                             present, ARSENAL_NEEDS)
    contact = _blank_missing(_blank_small(report.build_contact_table(rows), counts, ["Hard Hit %", "xRV/BBE"]),
                             present, CONTACT_NEEDS)
    return ars, contact, report.build_stuff_rows(rows)


# ── Location charts ──────────────────────────────────────────────────────────────────────────────
LOCATION_VIEWS = ["All pitches", "vs LHB", "vs RHB", "Balls in play"]


def _zone_box(fig: go.Figure) -> None:
    z = report.ZONE
    fig.add_shape(type="rect", x0=-z["h"], y0=z["b"], x1=z["h"], y1=z["t"], line=dict(color="#333", width=1.5))
    # home plate's front edge, for orientation (17 in wide)
    fig.add_shape(type="line", x0=-0.708, y0=0.62, x1=0.708, y1=0.62, line=dict(color="#999", width=2))


def location_fig(rows: pl.DataFrame, view: str, show_legend: bool = False, has_results: bool = True) -> go.Figure:
    """Where each pitch crossed the plate, pitcher's view (+ PlateLocSide is the third-base side, where a
    RHB stands), colored by pitch type. 'Balls in play' draws hits filled and outs hollow (all filled
    when the data has no results, e.g. a practice export)."""
    d = rows.filter(report.HAS_LOC & pl.col("TaggedPitchType").is_not_null()
                    & ~pl.col("TaggedPitchType").is_in(report.NON_PITCHES))
    if view == "vs LHB":
        d = d.filter(pl.col("BatterSide") == "Left")
    elif view == "vs RHB":
        d = d.filter(pl.col("BatterSide") == "Right")
    elif view == "Balls in play":
        d = d.filter(report.IN_PLAY)
    d = d.with_columns(pl.col("TaggedPitchType").replace(report.PITCH_NAMES).alias("PT"))
    fig = go.Figure()
    order = d.group_by("PT").len().sort("len", descending=True)["PT"].to_list()
    for pt in order:
        s = d.filter(pl.col("PT") == pt)
        color = report.PITCH_COLORS.get(pt, "#8c8c8c")
        if view == "Balls in play":
            hit = s["PlayResult"].is_in(report.HIT_RESULTS).to_list() if has_results else [True] * s.height
            symbols = ["circle" if h else "circle-open" for h in hit]
            text = [f"{r or ''} · {'%.0f' % ev if ev is not None else '–'} mph, {'%.0f°' % la if la is not None else '–'}"
                    for r, ev, la in zip(s["PlayResult"].to_list(), s["ExitSpeed"].to_list(), s["Angle"].to_list())]
            marker = dict(color=color, size=9, symbol=symbols, line=dict(color=color, width=2))
        else:
            text = [f"{v:.1f} mph · {c}" if v is not None else (c or "")
                    for v, c in zip(s["RelSpeed"].to_list(), s["PitchCall"].to_list())]
            marker = dict(color=color, size=7, opacity=0.75)
        fig.add_trace(go.Scatter(x=s["PlateLocSide"].to_list(), y=s["PlateLocHeight"].to_list(), mode="markers",
                                 name=pt, legendgroup=pt, showlegend=show_legend, marker=marker, text=text,
                                 hovertemplate=f"<b>{pt}</b><br>%{{text}}<extra></extra>"))
    _zone_box(fig)
    fig.update_layout(width=270, height=330 if not show_legend else 380, template="plotly_white",
                      title=dict(text=f"{view} · {d.height:,}", x=0.5, font=dict(size=13)),
                      margin=dict(l=8, r=8, t=36, b=8), showlegend=show_legend,
                      legend=dict(orientation="h", yanchor="top", y=-0.02, x=0, font=dict(size=10)),
                      xaxis=dict(range=list(report.XR), showticklabels=False, zeroline=False, fixedrange=True),
                      yaxis=dict(range=list(report.YR), showticklabels=False, zeroline=False, fixedrange=True,
                                 scaleanchor="x", scaleratio=1))
    if view == "All pitches":
        fig.add_annotation(text="Pitcher's view", x=1.0, y=1.0, xref="paper", yref="paper", xanchor="right",
                           yanchor="top", showarrow=False, font=dict(size=10, color="#888"))
    return fig


def location_figs(rows: pl.DataFrame, has_results: bool = True) -> list[go.Figure]:
    """The four location charts, left to right; the first carries the pitch-type legend."""
    return [location_fig(rows, v, show_legend=(i == 0), has_results=has_results) for i, v in enumerate(LOCATION_VIEWS)]


# ── Uploaded files (decisions §3): session only, read by column name ─────────────────────────────
UPLOAD_MAX_MB = 70                      # Cloudflare refuses requests over 100 MB; base64 adds a third
UPLOAD_MAX_BYTES = UPLOAD_MAX_MB * 1024 * 1024
UPLOAD_MAX_ROWS = 1_000_000             # a small parquet can expand to many GB in memory
UPLOAD_TTL_S = 2 * 3600                 # an upload left idle this long is dropped
UPLOAD_BUDGET_BYTES = 2 * 1024 ** 3     # every session's uploads together; the oldest are dropped first
# Practice / live BP exports name a few columns differently from game files (Matt is checking the list).
UPLOAD_RENAMES = {"HitType": "TaggedHitType"}
# Columns our own parquet (an export of served data) carries beyond the TrackMan schema.
_EXTRA = {"re288_state": pl.Utf8, "next_re288_state": pl.Utf8, "half_complete": pl.Boolean,
          "is_verified": pl.Boolean, "source_file": pl.Utf8}
# A full game file: count, inning and result columns, so the line, rates and run value all work.
GAME_COLUMNS = ["PitchCall", "KorBB", "PlayResult", "PitchofPA", "PAofInning", "Inning", "Top/Bottom",
                "Outs", "Balls", "Strikes", "OutsOnPlay"]


class UploadError(ValueError):
    """An upload we won't read; the message is shown to the user."""


def _cast(col: str, have: pl.DataType, want: pl.DataType) -> pl.Expr:
    """Read one column as the schema's type; anything that doesn't read becomes null."""
    if want == pl.Boolean and have == pl.Utf8:       # text true/false (a CSV export of served data)
        return pl.col(col).str.to_lowercase().replace_strict({"true": True, "false": False}, default=None,
                                                              return_dtype=pl.Boolean)
    if have == pl.Utf8 and want != pl.Utf8:
        return pl.col(col).str.strip_chars().cast(want, strict=False)
    return pl.col(col).cast(want, strict=False)


def read_upload(filename: str, content: bytes) -> dict:
    """An uploaded .csv / .parquet -> the cleaned rows plus what the report needs to know about them.

    Every column is matched BY NAME against TrackMan's 170-column schema (order doesn't matter):
    unknown columns are ignored, missing ones become blank (and the numbers needing them are blanked),
    and values that don't read as their type become blank and are counted. Then the same typo fixes as
    the pipeline (fix_dictionary), base states for a full game file (so it gets run value), the same
    xRV / RV scoring as served rows (blank without a Level), and Stuff+ on the D1 scale whatever the Level."""
    from pipeline_v2 import baserunner, fix_dictionary
    from pipeline_v2.trackman_schema import pl_csv_schema as schema
    name = PurePosixPath(filename or "upload").name
    ext = PurePosixPath(name).suffix.lower()
    if ext not in (".csv", ".parquet"):
        raise UploadError("Only .csv and .parquet files can be uploaded.")
    if len(content) > UPLOAD_MAX_BYTES:
        raise UploadError(f"The file is {len(content) / 2 ** 20:.1f} MB; the limit is {UPLOAD_MAX_MB} MB.")
    try:
        raw = (pl.read_csv(io.BytesIO(content), infer_schema_length=0, null_values=[""], encoding="utf8-lossy",
                           n_rows=UPLOAD_MAX_ROWS + 1) if ext == ".csv"
               else pl.read_parquet(io.BytesIO(content), n_rows=UPLOAD_MAX_ROWS + 1))
    except Exception as e:
        raise UploadError(f"Couldn't read the file ({type(e).__name__}).")
    if raw.height == 0:
        raise UploadError("The file has no rows.")
    if raw.height > UPLOAD_MAX_ROWS:
        raise UploadError(f"The file has more than {UPLOAD_MAX_ROWS:,} rows.")
    raw = raw.rename({a: b for a, b in UPLOAD_RENAMES.items() if a in raw.columns and b not in raw.columns})
    types = {**schema, **_EXTRA}
    known = [c for c in raw.columns if c in types]
    if "Pitcher" not in known and "Batter" not in known:
        raise UploadError("No Pitcher or Batter column: this doesn't look like a TrackMan file.")

    # Cast by name, counting values that were there but don't read as the column's type.
    cast = {c: _cast(c, raw.schema[c], t) for c, t in types.items() if c in raw.columns}
    lost = {c: n for c, n in raw.select((pl.col(c).is_not_null() & e.is_null()).sum().alias(c)
                                        for c, e in cast.items()).row(0, named=True).items() if n}
    df = raw.select(cast[c].alias(c) if c in cast else pl.lit(None, dtype=t).alias(c) for c, t in types.items())
    present = {c for c in known if df[c].null_count() < df.height}
    rows_in = df.height
    df = fix_dictionary.apply_fixes(df)           # typo fixes; rows with impossible counts dropped
    dropped = rows_in - df.height
    if df.height == 0:
        raise UploadError("Every row has impossible counts (Outs, Balls, Strikes or PitchofPA).")

    stem = PurePosixPath(name).stem
    df = df.with_columns(
        pl.when(pl.col("PitchUID").is_null()).then(pl.lit(f"upload:{stem}:") + pl.int_range(pl.len()).cast(pl.Utf8))
          .otherwise(pl.col("PitchUID")).alias("PitchUID"),
        # no GameID (a practice export): one "game" per date in the file
        pl.coalesce(pl.col("GameID"), pl.lit(f"{stem}:") + pl.col("Date").fill_null("")).alias("GameID"),
        pl.col("source_file").fill_null(name),
        pl.col("Date").str.slice(0, 4).alias("Year"),
    )
    notes, kind = [], ("game" if set(GAME_COLUMNS) <= present else "pitch data")
    if kind != "game" and "ExitSpeed" in present and "PitchCall" not in present:
        # A practice export has exit velocities but no pitch call: count every tracked exit velocity
        # as a batted ball, so Avg EV and Hard Hit % work (fouls with an exit velocity count too).
        df = df.with_columns(pl.when(pl.col("ExitSpeed").is_not_null()).then(pl.lit("InPlay")).alias("PitchCall"))
        notes.append("No PitchCall column: every pitch with an exit velocity counts as a batted ball "
                     "(fouls included), and whiff rates can't be computed.")
    if kind == "game" and not {"re288_state", "next_re288_state", "half_complete"} <= present:
        try:                                      # base states per game, as load.py does, for run value
            df = pl.concat([baserunner.annotate_game(g) for g in df.partition_by("GameID")], how="vertical_relaxed")
        except Exception:
            notes.append("Couldn't rebuild the base states, so run value is blank.")
    for step in (data._with_xrv, data._with_rv):
        try:
            df = step(df)
        except Exception:
            pass
    # Stuff+ has one model, trained on D1, and _with_stuff grades D1 pitches only. An upload is graded on
    # that D1 scale whatever its Level says (none, as in a practice / live BP export, or another level),
    # and the report says so; database games at other levels still keep Stuff+ blank.
    not_d1 = df.filter(pl.col("Level").is_null() | (pl.col("Level") != "D1"))
    stuff_scale = "D1" if not_d1.height else None
    try:
        df = (data._with_stuff(df.with_columns(pl.col("Level").alias("_level"), pl.lit("D1").alias("Level")))
                  .with_columns(pl.col("_level").alias("Level")).drop("_level"))
    except Exception:
        pass
    for c in ("xRV", "RV", *data.STUFF_COLS.values()):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))

    missing = [c for c in GAME_COLUMNS if c not in present]
    if missing:
        notes.insert(0, "Not in this file: " + ", ".join(missing) + ". The numbers that need them are blank.")
    if "Level" not in present:
        notes.append("No Level column: Stuff+ is graded on the D1 scale (the model's only scale). RV and "
                     "xRV are blank unless you pick a run environment (⚙ under the name), which gives "
                     "xRV against that level.")
    elif stuff_scale:
        others = sorted(not_d1["Level"].drop_nulls().unique().to_list())
        notes.append(f"Level {', '.join(others) or 'blank'}{' (not D1)' if others else ''}: Stuff+ is graded "
                     "on the D1 scale (the model's only scale).")
    if lost:
        notes.append("Values that couldn't be read (left blank): "
                     + ", ".join(f"{c} ×{n}" for c, n in sorted(lost.items(), key=lambda t: -t[1])[:8]) + ".")
    if dropped:
        notes.append(f"{dropped:,} row(s) with impossible counts were dropped.")
    ignored = [c for c in raw.columns if c not in types]
    return {"df": df, "name": name, "kind": kind, "present": present, "notes": notes, "stuff_scale": stuff_scale,
            "recognized": len(known), "columns": raw.width, "ignored": ignored, "bytes": len(content)}


def upload_summary(up: dict, role: str) -> str:
    """One line describing the upload under the upload box."""
    df = up["df"]
    col = data.ROLES[role]["player"]
    dates = df["Date"].drop_nulls()
    span = (fmt_date(dates.min()) if dates.min() == dates.max() else f"{fmt_date(dates.min())} – {fmt_date(dates.max())}") if dates.len() else "no dates"
    kind = "TrackMan game file" if up["kind"] == "game" else "pitch data only (e.g. a practice export)"
    return (f"{up['name']} · {df.height:,} pitches · {df[col].n_unique():,} {role}s · {span} · {kind} · "
            f"{up['recognized']} of {up['columns']} columns recognized")


_UPLOADS: dict[str, dict] = {}
_UPLOADS_LOCK = threading.Lock()


def _evict_locked(now: float) -> None:
    for k in [k for k, v in _UPLOADS.items() if now - v["at"] > UPLOAD_TTL_S]:
        del _UPLOADS[k]
    total = sum(v["size"] for v in _UPLOADS.values())
    for k in sorted(_UPLOADS, key=lambda k: _UPLOADS[k]["at"]):
        if total <= UPLOAD_BUDGET_BYTES:
            break
        total -= _UPLOADS.pop(k)["size"]


def put_upload(up: dict, replaces: str | None = None) -> str:
    """Keep an upload in this server's memory (never on disk, never shared) and return its key; the
    browser session holds the key. A new upload replaces that session's previous one."""
    key = uuid.uuid4().hex
    with _UPLOADS_LOCK:
        if replaces:
            _UPLOADS.pop(replaces, None)
        _UPLOADS[key] = {"up": up, "at": time.time(), "size": up["df"].estimated_size()}
        _evict_locked(time.time())
    return key


def get_upload(key: str | None) -> dict | None:
    """The session's upload, or None once it has expired (or the app was reloaded)."""
    if not key:
        return None
    with _UPLOADS_LOCK:
        hit = _UPLOADS.get(key)
        if hit:
            hit["at"] = time.time()
        return hit["up"] if hit else None


def drop_upload(key: str | None) -> None:
    with _UPLOADS_LOCK:
        _UPLOADS.pop(key or "", None)


def _upload_games_frame(df: pl.DataFrame) -> pl.DataFrame:
    """Per game key: its date (first pitch), start time, teams and stadium."""
    first = pl.when(pl.col("Date").is_not_null()).then(
        pl.concat_str([pl.col("Date"), pl.col("Time").fill_null("")], separator=" "))
    return (df.group_by("GameID").agg(first.min().alias("first_pitch"), pl.col("HomeTeam").drop_nulls().first().alias("home"),
                                      pl.col("AwayTeam").drop_nulls().first().alias("away"),
                                      pl.col("Stadium").drop_nulls().first().alias("stadium"))
              .with_columns(pl.col("first_pitch").str.slice(0, 10).alias("game_date"),
                            pl.col("first_pitch").str.slice(11).alias("first_time")))


def upload_players(up: dict, role: str, start: str | None, end: str | None) -> list[str]:
    """The upload's players, narrowed to games that started between start and end when a date is typed."""
    col = data.ROLES[role]["player"]
    df = up["df"].filter(pl.col(col).is_not_null())
    if start:
        g = _upload_games_frame(df).filter(pl.col("game_date").is_between(pl.lit(start), pl.lit(end or start)))
        df = df.filter(pl.col("GameID").is_in(g["GameID"].to_list()))
    return sorted(df[col].unique().to_list())


def upload_latest_date(up: dict) -> str | None:
    return up["df"]["Date"].max()


def upload_games(up: dict, role: str, player: str) -> pl.DataFrame:
    """The player's games in the upload, shaped like ``games_for`` (so ``choose_games`` and the tables
    work unchanged). A file without home / away teams has no opponent."""
    r = data.ROLES[role]
    df = up["df"].filter(pl.col(r["player"]) == player)
    per = (df.group_by("GameID").agg(pl.col(r["team"]).drop_nulls().mode().first().alias("team"),
                                     pl.col("Level").drop_nulls().first().alias("level"),
                                     pl.col("is_verified").drop_nulls().first().fill_null(True).alias("verified"),
                                     pl.len().alias("pitches"))
              .join(_upload_games_frame(up["df"]), on="GameID", how="left")
              .with_columns(pl.lit("").alias("pid"), pl.lit(None, dtype=pl.Utf8).alias("hand"),
                            pl.lit(up["name"]).alias("source_file"), pl.col("verified").fill_null(True)))
    g = _finish(per.select(list(_GAME_SCHEMA)))
    g = g.with_columns(pl.when(pl.col("home").is_null()).then(pl.lit("")).otherwise(pl.col("side")).alias("side"),
                       pl.when(pl.col("home").is_null()).then(pl.lit(None, dtype=pl.Utf8))
                         .otherwise(pl.col("opponent")).alias("opponent"))
    return games_for(g, None)


def upload_rows(up: dict, role: str, player: str, games: pl.DataFrame,
                re_level: str | None = None, re_year: str | None = None) -> pl.DataFrame:
    """The chosen games' rows for the player, from the upload. A run environment re-scores xRV and RV
    against that (level, year): for a file with no Level (a practice export) it's the only way to get
    xRV; RV still needs a game file's base states."""
    col = data.ROLES[role]["player"]
    rows = up["df"].filter((pl.col(col) == player) & pl.col("GameID").is_in(games["GameID"].to_list()))
    if re_level or re_year:
        try:
            rows = data._with_rv(data._with_xrv(rows, re_level=re_level, re_year=re_year),
                                 re_level=re_level, re_year=re_year)
        except Exception:
            pass                                  # keep the upload's own scores
    return rows
