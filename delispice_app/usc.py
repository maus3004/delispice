"""USC tab logic (the layout and callbacks are in local_tabs.py, the decisions in usc_decisions.md). On the site,
only the signed-in owner sees the tab.

A chart file is a hand-charted CSV (ChartPitcher, ChartBatter, ChartPitchType, PitchCall, IntendedLocation,
PlayResult, Notes, ...) kept alongside a TrackMan file (a practice export or a game). The two should line up
row for row, but TrackMan misses pitches, so they are paired plate appearance by plate appearance:

  1. TrackMan rows: Live throws only in a practice export (PitchSession), every row in a game file.
  2. Chart names map to TrackMan's, allowing for spelling (Gonzales = Gonzalez): full names ("Last, First",
     as charts are written now) to full names, or last names to last names for a chart that has only those.
  3. Plate appearances (runs of the same pitcher + batter) are lined up in order. A PA with the same pitch
     count in both pairs pitch by pitch; one whose counts differ is lined up by pitch type and contact
     (a charted ball in play or foul vs a TrackMan exit velocity).
  4. Chart pitches TrackMan missed stay in as "chart only" rows, TrackMan pitches the chart missed as
     "TrackMan only" rows, and the summary lists every PA that didn't line up exactly.

Before pairing, the chart's values are put in TrackMan's form (map_values): Foul -> FoulBallNotFieldable,
SO -> KorBB Strikeout, Flyout -> PlayResult Out + TaggedHitType FlyBall, and so on.

The arsenal's rate colors read a D1 league reference (build_league). Build or rebuild it from the repo folder
(about a second; rerun after new games or for another season with --year):

    .venv/bin/python -m delispice_app.usc --build
    .venv/bin/python -m delispice_app.usc --build --year 2027

The same command builds the "Why this grade" comparisons (build_why, about 30 s): rerun it after a Stuff+ retrain.
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date
from functools import lru_cache
from math import factorial
from pathlib import Path, PurePosixPath

import numpy as np
import polars as pl

from . import data, game_report, pitch_lab, report
from pipeline_v2 import config

TEAM = "SOU_TRO"                     # USC in TrackMan
PAIRED, CHART_ONLY, TM_ONLY = "paired", "chart only", "TrackMan only"
NAME_MATCH = 0.8                     # difflib ratio for a chart name to count as a TrackMan name
CHART_MARK = "ChartPitcher"          # a chart file has this column; a TrackMan file has Pitcher instead

# Pitch types grouped so the chart's calls and TrackMan's tags compare (a charted splitter is often a
# tagged changeup; any fastball matches any fastball).
TYPE_GROUP = {"fourseamfastball": "FB", "fastball": "FB", "twoseamfastball": "FB", "sinker": "FB",
              "cutter": "CT", "slider": "SL", "sweeper": "SL", "curveball": "CB", "knucklecurve": "CB",
              "changeup": "CH", "splitter": "CH"}


class PairError(ValueError):
    """A file or pair we won't use; the message is shown to the user."""


# ── Reading ────────────────────────────────────────────────────────────────────────────────────────────
def is_chart(content: bytes) -> bool:
    head = pl.read_csv(io.BytesIO(content), n_rows=0, encoding="utf8-lossy").columns
    return CHART_MARK in head


def read_chart(name: str, content: bytes) -> tuple[pl.DataFrame, list[str]]:
    """A chart CSV, every column as text, with its values in TrackMan's form (map_values), plus the values
    it didn't recognize. Columns are renamed to start with "Chart" (PitchCall -> ChartPitchCall), so they
    never collide with TrackMan's own columns."""
    if PurePosixPath(name).suffix.lower() != ".csv":
        raise PairError(f"{name}: a chart file must be a .csv.")
    df = pl.read_csv(io.BytesIO(content), infer_schema_length=0, null_values=[""], encoding="utf8-lossy")
    for need in ("ChartPitcher", "ChartBatter"):
        if need not in df.columns:
            raise PairError(f"{name}: no {need} column, so its plate appearances can't be lined up.")
    if df.height == 0:
        raise PairError(f"{name}: the file has no rows.")
    df = df.rename({c: c if c.startswith("Chart") else f"Chart{c}" for c in df.columns})
    return map_values(df.with_columns(pl.col(pl.Utf8).str.strip_chars()))


# ── Chart values -> TrackMan's ─────────────────────────────────────────────────────────────────────────
# Keys are values lowercased with everything but letters dropped, so "Foul", "foul " and "Called Strike" all
# match. A value already in TrackMan's form maps to itself.
U = "Undefined"
CALLS = {"ballcalled": "BallCalled", "strikecalled": "StrikeCalled", "calledstrike": "StrikeCalled",
         "strikeswinging": "StrikeSwinging", "inplay": "InPlay", "hitbypitch": "HitByPitch",
         # a chart can't tell a fieldable foul from one that isn't: NotFieldable is 98.5% of 2026 D1 fouls,
         # and every delispice calculation counts the three foul calls the same
         "foul": "FoulBallNotFieldable", "foulball": "FoulBallNotFieldable",
         "foulballnotfieldable": "FoulBallNotFieldable", "foulballfieldable": "FoulBallFieldable"}
# PlayResult -> (KorBB, PlayResult, TaggedHitType): TrackMan splits the chart's one result column across
# three, and a hit by pitch is a PitchCall (HBP below).
RESULTS = {"so": ("Strikeout", U, U), "strikeout": ("Strikeout", U, U), "walk": ("Walk", U, U),
           "hbp": (U, U, U), "hitbypitch": (U, U, U),
           "single": (U, "Single", U), "double": (U, "Double", U), "triple": (U, "Triple", U),
           "hr": (U, "HomeRun", U), "homerun": (U, "HomeRun", U),
           "out": (U, "Out", U), "flyout": (U, "Out", "FlyBall"), "groundout": (U, "Out", "GroundBall"),
           "popout": (U, "Out", "Popup"), "lineout": (U, "Out", "LineDrive"),
           "fc": (U, "FieldersChoice", U), "fielderschoice": (U, "FieldersChoice", U), "error": (U, "Error", U),
           "sac": (U, "Sacrifice", U), "sacrifice": (U, "Sacrifice", U), "sacfly": (U, "Sacrifice", "FlyBall"),
           "sacbunt": (U, "Sacrifice", "Bunt")}
HBP = ["hbp", "hitbypitch"]
RESULT_NOTES = ["goodloc", "goodlocation"]      # notes written in the PlayResult column: moved to Notes
# TrackMan's pitch type names (the schema's): FourSeamFastball -> FourSeamFastBall. Others stay as written.
PITCH_TYPES = {t.lower(): t for t in ["Fastball", "Sinker", "Cutter", "Slider", "Sweeper", "Curveball", "ChangeUp",
                                      "Splitter", "Knuckleball", "FourSeamFastBall", "TwoSeamFastBall",
                                      "OneSeamFastBall", "Other", "Undefined"]}


def map_values(df: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    """The chart's calls, results and pitch types in TrackMan's values: PlayResult becomes ChartKorBB +
    ChartPlayResult + ChartTaggedHitType, an HBP result becomes PitchCall HitByPitch, a blank call or result
    becomes Undefined. Also returns the values it didn't recognize ("PitchCall 'Fowl' (2 rows)"), which are
    set to Undefined too."""
    key = lambda c: pl.col(c).str.to_lowercase().str.replace_all("[^a-z]", "")
    unknown = []
    for col, known in (("ChartPitchCall", list(CALLS)), ("ChartPlayResult", list(RESULTS) + RESULT_NOTES)):
        if col in df.columns:
            bad = df.filter(pl.col(col).is_not_null() & ~key(col).is_in(known))[col].value_counts(sort=True)
            unknown += [f"{col.removeprefix('Chart')} '{v}' ({n} row{'s' * (n != 1)})" for v, n in bad.iter_rows()]
    has_res = "ChartPlayResult" in df.columns
    res = key("ChartPlayResult") if has_res else pl.lit(None, pl.Utf8)
    exprs = []                           # all read the columns as charted
    if "ChartPitchType" in df.columns:
        exprs.append(key("ChartPitchType").replace_strict(PITCH_TYPES, default=pl.col("ChartPitchType")).alias("ChartPitchType"))
    if "ChartPitchCall" in df.columns:
        exprs.append(pl.when(res.is_in(HBP)).then(pl.lit("HitByPitch"))
                       .otherwise(key("ChartPitchCall").replace_strict(CALLS, default=U)).fill_null(U).alias("ChartPitchCall"))
    if has_res:
        exprs += [res.replace_strict({k: v[i] for k, v in RESULTS.items()}, default=U).fill_null(U).alias(name)
                  for i, name in enumerate(["ChartKorBB", "ChartPlayResult", "ChartTaggedHitType"])]
        moved = pl.when(res.is_in(RESULT_NOTES)).then(pl.col("ChartPlayResult"))
        exprs.append(pl.when(moved.is_null()).then(pl.col("ChartNotes"))
                       .otherwise(pl.concat_str([pl.col("ChartNotes"), moved], separator="; ", ignore_nulls=True))
                       .alias("ChartNotes") if "ChartNotes" in df.columns else moved.alias("ChartNotes"))
    df = df.with_columns(exprs)
    if has_res:                          # the two new columns go next to PlayResult
        cols = [c for c in df.columns if c not in ("ChartKorBB", "ChartTaggedHitType")]
        i = cols.index("ChartPlayResult")
        df = df.select(cols[:i] + ["ChartKorBB", "ChartPlayResult", "ChartTaggedHitType"] + cols[i + 1:])
    return df, unknown


def chart_date(name: str, year: int | None = None) -> date | None:
    """The date in a chart file's name: "scoutday 10:30.csv" (macOS shows a '/' in a file name as ':'),
    10-30, 10_30 or 10/30/26. Without a year in the name, ``year`` (the TrackMan file's) is used."""
    m = re.search(r"(?<!\d)(\d{1,2})[:/\-_.](\d{1,2})(?:[:/\-_.](\d{2,4}))?(?!\d)", PurePosixPath(name).stem)
    if not m:
        return None
    mo, d, y = int(m[1]), int(m[2]), m[3]
    y = (2000 + int(y) if len(y) == 2 else int(y)) if y else (year or date.today().year)
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# ── Pairing ────────────────────────────────────────────────────────────────────────────────────────────
def _last(name: str | None) -> str:
    return (name or "").split(",")[0].strip()


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _canon(chart_names: list[str], tm_names: list[str]) -> dict[str, str]:
    """Chart name -> the TrackMan name it means: the same letters, else the closest within NAME_MATCH."""
    by_norm = {_norm(t): t for t in tm_names if t}
    out = {}
    for c in chart_names:
        n = _norm(c)
        if n in by_norm:
            out[c] = by_norm[n]
            continue
        best = max(by_norm, key=lambda t: difflib.SequenceMatcher(a=n, b=t).ratio(), default=None)
        out[c] = by_norm[best] if best and difflib.SequenceMatcher(a=n, b=best).ratio() >= NAME_MATCH else c
    return out


def _type(t: str | None) -> str:
    return TYPE_GROUP.get(_norm(t), _norm(t) or "?")


def _pas(keys: list[tuple]) -> list[tuple[tuple, int, int]]:
    """Runs of the same (pitcher, batter): [(key, first row, row count)]."""
    out = []
    for i, k in enumerate(keys):
        if out and out[-1][0] == k:
            out[-1] = (k, out[-1][1], out[-1][2] + 1)
        else:
            out.append((k, i, 1))
    return out


CONTACT_CALLS = {"inplay", "foulball", "foulballfieldable", "foulballnotfieldable"}
GAP = -2                             # leaving a pitch unpaired, in _pair_pa's scoring


def _fit(c: tuple[str, bool], t: tuple[str, bool]) -> int:
    """How well a charted pitch (type, contact called) fits a TrackMan pitch (type, has exit velocity).
    An exit velocity is strong evidence of contact; a charted contact without one is only weak evidence
    against (TrackMan misses some batted balls)."""
    s = 2 if c[0] == t[0] else -1
    if t[1]:
        s += 2 if c[1] else -3
    return s


def _pair_pa(ct: list[tuple[str, bool]], tt: list[tuple[str, bool]]) -> list[tuple[int | None, int | None]]:
    """One PA's pitches: (chart offset, TrackMan offset) pairs, None on the side that has no pitch.
    Same counts pair in order. Otherwise the best-scoring alignment (_fit per pair, GAP per unpaired
    pitch) decides which pitches one file is missing; on a tie, the earliest pitch is left unpaired."""
    n, m = len(ct), len(tt)
    if n == m:
        return list(zip(range(n), range(m)))
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        best[i][0] = i * GAP
    for j in range(1, m + 1):
        best[0][j] = j * GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best[i][j] = max(best[i - 1][j - 1] + _fit(ct[i - 1], tt[j - 1]), best[i - 1][j] + GAP, best[i][j - 1] + GAP)
    out, i, j = [], n, m
    while i or j:                        # walk back, preferring a pair over a gap
        if i and j and best[i][j] == best[i - 1][j - 1] + _fit(ct[i - 1], tt[j - 1]):
            out.append((i - 1, j - 1)); i -= 1; j -= 1
        elif i and best[i][j] == best[i - 1][j] + GAP:
            out.append((i - 1, None)); i -= 1
        else:
            out.append((None, j - 1)); j -= 1
    return out[::-1]


def live_rows(tm: pl.DataFrame) -> pl.DataFrame:
    """The TrackMan rows a chart covers: Live throws in a practice export, everything in a game file."""
    if "PitchSession" in tm.columns and (tm["PitchSession"] == "Live").any():
        return tm.filter(pl.col("PitchSession") == "Live")
    return tm


def pair(tm: pl.DataFrame, chart: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """TrackMan rows + chart rows -> one frame (TrackMan columns, then the Chart* columns, then ``Pair``),
    in pitch order, plus a summary of what lined up."""
    warmups = tm.height
    tm = live_rows(tm).with_row_index("_tm")
    warmups -= tm.height
    chart = chart.with_row_index("_chart")

    def names(col: str) -> list[str]:
        """TrackMan's names to compare the chart's with: full names when every chart name in the column is
        one ("Last, First"), else last names (a chart written with last names only)."""
        full = chart[f"Chart{col}"].drop_nulls().str.contains(",").all()
        return [(x or "").strip() if full else _last(x) for x in tm[col].to_list()]

    tm_p, tm_b = names("Pitcher"), names("Batter")
    pmap = _canon(chart["ChartPitcher"].unique().drop_nulls().to_list(), sorted(set(tm_p)))
    bmap = _canon(chart["ChartBatter"].unique().drop_nulls().to_list(), sorted(set(tm_b)))
    ch_p = [pmap.get(x, x) for x in chart["ChartPitcher"].to_list()]
    ch_b = [bmap.get(x, x) for x in chart["ChartBatter"].to_list()]
    col = lambda name: chart[name].to_list() if name in chart.columns else [None] * chart.height
    tm_t = [(_type(x), ev is not None) for x, ev in zip(tm["TaggedPitchType"].to_list(), tm["ExitSpeed"].to_list())]
    ch_t = [(_type(x), _norm(call) in CONTACT_CALLS) for x, call in zip(col("ChartPitchType"), col("ChartPitchCall"))]

    cpa, tpa = _pas(list(zip(ch_p, ch_b))), _pas(list(zip(tm_p, tm_b)))
    links, off = [], []                  # links: (chart row | None, TrackMan row | None); off: PAs that didn't line up exactly
    sm = difflib.SequenceMatcher(a=[k for k, _, _ in cpa], b=[k for k, _, _ in tpa], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for (k, cs, cn), (_, ts, tn) in zip(cpa[i1:i2], tpa[j1:j2]):
                pp = _pair_pa(ch_t[cs:cs + cn], tm_t[ts:ts + tn])
                links += [(None if a is None else cs + a, None if b is None else ts + b) for a, b in pp]
                if cn != tn:
                    off.append({"Pitcher": k[0], "Batter": k[1], "Chart": cn, "TrackMan": tn,
                                "Unpaired": sum(a is None or b is None for a, b in pp)})
        else:                            # PAs only one file has (or the two disagree on the batter)
            for k, cs, cn in cpa[i1:i2]:
                links += [(cs + a, None) for a in range(cn)]
                off.append({"Pitcher": k[0], "Batter": k[1], "Chart": cn, "TrackMan": 0, "Unpaired": cn})
            for k, ts, tn in tpa[j1:j2]:
                links += [(None, ts + b) for b in range(tn)]
                off.append({"Pitcher": k[0], "Batter": k[1], "Chart": 0, "TrackMan": tn, "Unpaired": tn})

    m = pl.DataFrame(links, schema={"_chart": pl.UInt32, "_tm": pl.UInt32}, orient="row")
    m = m.with_columns(pl.when(pl.col("_chart").is_null()).then(pl.lit(TM_ONLY))
                         .when(pl.col("_tm").is_null()).then(pl.lit(CHART_ONLY)).otherwise(pl.lit(PAIRED)).alias("Pair"))
    out = m.join(tm, on="_tm", how="left").join(chart, on="_chart", how="left")
    # A chart-only row takes its names from TrackMan's spelling ("Last, First") when the player is in the file.
    full_p = dict(zip(tm_p, tm["Pitcher"].to_list()))
    full_b = dict(zip(tm_b, tm["Batter"].to_list()))
    out = out.with_columns(
        pl.coalesce("Pitcher", pl.col("ChartPitcher").replace_strict(pmap, default=pl.col("ChartPitcher")).replace_strict(full_p, default=None), "ChartPitcher").alias("Pitcher"),
        pl.coalesce("Batter", pl.col("ChartBatter").replace_strict(bmap, default=pl.col("ChartBatter")).replace_strict(full_b, default=None), "ChartBatter").alias("Batter"),
    ).drop("_tm", "_chart")
    out = out.select([c for c in out.columns if c != "Pair"] + ["Pair"])
    counts = out["Pair"].value_counts()
    n = {r[0]: r[1] for r in counts.iter_rows()}
    summary = {"chart_rows": chart.height, "tm_rows": tm.height, "warmups": warmups,
               "paired": n.get(PAIRED, 0), "chart_only": n.get(CHART_ONLY, 0), "tm_only": n.get(TM_ONLY, 0),
               "renamed": sorted({(c, t) for c, t in {**pmap, **bmap}.items() if _norm(c) != _norm(t)}),
               "off": off}
    return out, summary


def pick_tm(chart_name: str, chart: pl.DataFrame, tms: dict[str, pl.DataFrame]) -> str:
    """Which TrackMan file (or database game) a chart goes with: the only one; else the ones on the
    chart's date (from its name); among several, the one that pairs the most rows."""
    if not tms:
        raise PairError(f"{chart_name}: no TrackMan file or game to pair it with.")
    if len(tms) == 1:
        return next(iter(tms))
    cands = tms
    years = {d.year for df in tms.values() for d in [_file_date(df)] if d}
    when = chart_date(chart_name, max(years) if years else None)
    if when:
        cands = {k: v for k, v in tms.items() if _file_date(v) == when} or tms
    return max(cands, key=lambda k: pair(cands[k], chart)[1]["paired"])


def _file_date(df: pl.DataFrame) -> date | None:
    d = df["Date"].drop_nulls().mode()
    try:
        return date.fromisoformat(d[0][:10]) if d.len() else None
    except ValueError:
        return None


# ── Report rows ────────────────────────────────────────────────────────────────────────────────────────
# The report reads TrackMan's columns (game_report's tables), so the chart fills the ones a practice export
# doesn't have: the calls and results from the chart's values, the count and outs worked out from them.
EVENTS = {"PitchCall": "ChartPitchCall", "KorBB": "ChartKorBB", "PlayResult": "ChartPlayResult",
          "TaggedHitType": "ChartTaggedHitType"}
BALL_CALLS = {"BallCalled", "BallinDirt", "BallIntentional", "AutomaticBall"}
STRIKE_CALLS = {"StrikeCalled", "StrikeSwinging", "AutomaticStrike"}
PA_END_CALLS = {"InPlay", "HitByPitch"}
OUTS_ON_PLAY = {"Out": 1, "Sacrifice": 1, "FieldersChoice": 1}   # TrackMan's usual; a double play isn't charted


def report_rows(out: pl.DataFrame) -> pl.DataFrame:
    """A paired frame -> the rows the report reads: TrackMan's columns, with the chart filling in what the
    TrackMan file doesn't have. A call or result is TrackMan's where the file has one (a game) and the
    chart's otherwise (a practice export has none); a pitch TrackMan missed takes the charted pitch type
    (_chart_types). PitchofPA, Balls, Strikes and OutsOnPlay are the file's when it has them, else worked
    out from the charted sequence (_sequence)."""
    out = out.with_columns(pl.coalesce(c, chart).alias(c) for c, chart in EVENTS.items() if chart in out.columns)
    out = (out.with_columns(pl.Series("_ct", _chart_types(out), dtype=pl.Utf8))
              .with_columns(pl.coalesce("TaggedPitchType", "_ct").alias("TaggedPitchType")).drop("_ct"))
    tracked = out.filter(pl.col("Pair") != CHART_ONLY)
    return out.with_columns(pl.Series(c, v, dtype=out.schema[c]) for c, v in _sequence(out).items()
                            if tracked[c].null_count() == tracked.height)


def _chart_types(out: pl.DataFrame) -> list[str | None]:
    """Each row's charted pitch type in TrackMan's tags for that pitcher, so a pitch TrackMan missed joins
    the right arsenal row (a charted FourSeamFastBall is TrackMan's "Fastball"): the same type if TrackMan
    tagged the pitcher with it, else his most-used TrackMan tag in the same group (TYPE_GROUP), else as charted."""
    if "ChartPitchType" not in out.columns:
        return [None] * out.height
    tags: dict[str, list[str]] = {}
    for p, t, _ in (out.filter(pl.col("TaggedPitchType").is_not_null()).group_by("Pitcher", "TaggedPitchType")
                       .len().sort("len", descending=True).iter_rows()):
        tags.setdefault(p, []).append(t)

    def pick(p, c):
        ts = tags.get(p, [])
        return (next((t for t in ts if _norm(t) == _norm(c)), None)
                or next((t for t in ts if _type(t) == _type(c)), c))
    return [None if c is None else pick(p, c) for p, c in zip(out["Pitcher"].to_list(), out["ChartPitchType"].to_list())]


def _sequence(out: pl.DataFrame) -> dict[str, list]:
    """PitchofPA, Balls and Strikes (the count before the pitch) and OutsOnPlay, from the calls and results
    in pitch order. A PA ends on a strikeout, walk, ball in play or hit by pitch (or four balls / three
    strikes), or when the pitcher or batter changes. After a pitch with no call (one the chart missed),
    the rest of that PA's count is unknown (blank)."""
    p, b, call, korbb, res = (out[c].to_list() for c in ("Pitcher", "Batter", "PitchCall", "KorBB", "PlayResult"))
    seq = {"PitchofPA": [], "Balls": [], "Strikes": [], "OutsOnPlay": []}
    prev, ended, n, balls, strikes, known = None, True, 0, 0, 0, True
    for i in range(out.height):
        if (p[i], b[i]) != prev or ended:
            n, balls, strikes, known = 0, 0, 0, True
        n += 1
        seq["PitchofPA"].append(n)
        seq["Balls"].append(balls if known else None)
        seq["Strikes"].append(strikes if known else None)
        seq["OutsOnPlay"].append(None if res[i] is None else OUTS_ON_PLAY.get(res[i], 0))
        c = call[i]
        if c in BALL_CALLS:
            balls += 1
        elif c in STRIKE_CALLS:
            strikes += 1
        elif c in report.FOUL_CALLS:
            strikes = min(strikes + 1, 2)
        elif c not in PA_END_CALLS:
            known = False
        ended = korbb[i] in ("Strikeout", "Walk") or c in PA_END_CALLS or balls >= 4 or strikes >= 3
        prev = (p[i], b[i])
    return seq


# ── Loc%: did the pitch cross inside its charted target (ChartIntendedLocation)? ─────────────────────────
# Matt's definitions (2026-10-31), on delispice's strike zone (report.ZONE) split into 3x3 squares. Side is
# measured from the batter, + toward him (inside). TrackMan's PlateLocSide is + on the third-base side, where a
# RHB stands (2026 D1 hit-by-pitches: RHB median +1.8 ft, LHB -1.85 ft), so a RHB's side is PlateLocSide and a
# LHB's its negative.
LOC_MARGIN = 0.25                    # ft past the zone's edges that still counts (3 in, about a ball), for every target but Middle
LOC_EXPAND = 5 / 12                  # ft past the outside edge that still counts for ExpandOutside (5 in)
LOC_SIDED = {"Inside", "Outside", "ExpandOutside"}   # need the batter's side; the others are symmetric


def loc_boxes() -> dict[str, tuple[float, float, float, float]]:
    """Target -> its region (side from, side to, height from, height to) in ft, side from the batter. Every
    target is a rectangle, so Loc% (loc_regions) and the pitch card's shading (loc_box_plate) share this."""
    b, t, h = report.ZONE["b"], report.ZONE["t"], report.ZONE["h"]
    third, m = (t - b) / 3, LOC_MARGIN
    return {"Middle": (-h, h, b, t),                                  # anywhere in the zone
            "Inside": (h / 3, h + m, b - m, t + m),                   # inner 3 squares + the margin
            "Outside": (-(h + m), -h / 3, b - m, t + m),              # outer 3 squares + the margin
            # the outer half of the outside column of squares, + 5 in past the edge and the margin above / below
            "ExpandOutside": (-(h + LOC_EXPAND), -2 * h / 3, b - m, t + m),
            "Down": (-(h + m), h + m, b - m, b + third),              # bottom 3 squares + the margin
            "Top": (-(h + m), h + m, t - third, t + m)}               # top 3 squares + the margin


def loc_regions() -> dict[str, pl.Expr]:
    """Target -> whether the pitch crossed in that target's region."""
    side = pl.when(pl.col("BatterSide") == "Left").then(-pl.col("PlateLocSide")).otherwise(pl.col("PlateLocSide"))
    ht = pl.col("PlateLocHeight")
    return {k: side.is_between(s0, s1) & ht.is_between(h0, h1) for k, (s0, s1, h0, h1) in loc_boxes().items()}


def loc_box_plate(target: str | None, batter_side: str | None) -> tuple[float, float, float, float] | None:
    """A target's region in TrackMan's plate coordinates (PlateLocSide from, to, height from, to) for this
    batter, as the card's location chart draws them; None for an unknown target, or a sided one without
    the batter's side."""
    found = {k.lower(): (k, v) for k, v in loc_boxes().items()}.get(re.sub(r"[^a-z]", "", (target or "").lower()))
    if found is None or (found[0] in LOC_SIDED and batter_side not in ("Left", "Right")):
        return None
    s0, s1, h0, h1 = found[1]
    return (-s1, -s0, h0, h1) if batter_side == "Left" else (s0, s1, h0, h1)


def located() -> pl.Expr:
    """True / False: the pitch crossed inside its charted target. Null (left out of Loc%) without a target,
    a TrackMan location, a defined target, or, for a sided target, the batter's side."""
    intent = pl.col("ChartIntendedLocation").str.to_lowercase().str.replace_all("[^a-z]", "")
    has_side = pl.col("BatterSide").is_in(["Left", "Right"])
    out = pl.lit(None, pl.Boolean)
    for k, hit in loc_regions().items():
        ok = report.HAS_LOC & (has_side if k in LOC_SIDED else pl.lit(True))
        out = pl.when((intent == k.lower()) & ok).then(hit).otherwise(out)
    return out


def summary_text(s: dict) -> str:
    warm = f" ({s['warmups']:,} warmups left out)" if s["warmups"] else ""
    return (f"{s['paired']:,} of {s['chart_rows']:,} chart rows paired with TrackMan's {s['tm_rows']:,} "
            f"pitches{warm} · {s['chart_only']:,} chart only · {s['tm_only']:,} TrackMan only")


# ── Database games ─────────────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=8)
def team_games(team: str) -> pl.DataFrame:
    """Every served game with this team: GameID, its file, date, level and opponent, newest first."""
    lf = pl.scan_parquet(str(config.PITCHES_DIR / "**" / "*.parquet"))
    g = (lf.filter((pl.col("PitcherTeam") == team) | (pl.col("BatterTeam") == team))
           .group_by("GameID").agg(pl.col("source_file").first(), pl.col("Date").min().alias("date"),
                                   pl.col("Level").first(), pl.col("HomeTeam").first(), pl.col("AwayTeam").first(),
                                   pl.len().alias("pitches"))
           .collect())
    return g.sort("date", "GameID", descending=True)


def game_options(team: str) -> list[dict]:
    g = team_games(team)
    home = {r["GameID"]: r for r in g.iter_rows(named=True)}
    out = []
    for gid, r in home.items():
        opp = r["AwayTeam"] if r["HomeTeam"] == team else r["HomeTeam"]
        side = "vs" if r["HomeTeam"] == team else "@"
        out.append({"label": f"{game_report.fmt_date(r['date'])} · {side} {data.team_label(opp or '?')} · "
                             f"{r['Level']} · {r['pitches']} pitches", "value": gid})
    return out


def game_rows(game_ids: list[str]) -> dict[str, pl.DataFrame]:
    """Each chosen game's served rows, in the file's pitch order, scored like Game Reports' rows
    (retags, xRV, RV, Stuff+)."""
    lf = pl.scan_parquet(str(config.PITCHES_DIR / "**" / "*.parquet"))
    df = data.apply_retags(lf.filter(pl.col("GameID").is_in(game_ids)).collect()
                               .with_columns(pl.col("Date").str.slice(0, 4).alias("Year")))
    for step in (data._with_xrv, data._with_rv, data._with_stuff):
        df = step(df)
    return {gid: g.sort("source_file", "PitchNo") for (gid,), g in df.group_by("GameID")}


# ── League reference: D1 averages and spreads for the arsenal's rate columns ────────────────────────────
# Built once from a season's D1 data (Matt, 2026-10-31) and kept next to Pitch Lab's reference in
# pipeline_v2/artifacts/ (git-ignored). The build command is in this file's docstring.
LEAGUE_YEAR = "2026"
LEAGUE_RATES = ["Strike %", "Zone %", "Zone-Pre2S%", "Chase%", "WHIFF %", "IZ-Whiff%"]    # the arsenal's names
# Pitch types compared as one (Matt): TrackMan tag -> the reference's pitch name; the rest keep the arsenal's.
LEAGUE_GROUPS = {"FourSeamFastBall": "Fastball", "TwoSeamFastBall": "Sinker", "Sweeper": "Slider"}
LEAGUE_MIN = 20          # pitches in a rate's denominator for a pitcher to count toward its spread
LEAGUE_FULL_SD = 3       # the arsenal's colors are full this many SDs from average (like the Stuff table)


def _rate_parts() -> dict[str, tuple[pl.Expr, pl.Expr]]:
    """LEAGUE_RATES as (numerator, denominator) pitch filters, by the report's own definitions: Strike % and
    WHIFF % as report.py's arsenal, the zone rates as game_report's."""
    swing = pl.col("PitchCall").is_in(report.SWING_CALLS)
    rates = {"Strike %": (pl.col("PitchCall").is_in(report.STRIKE_CALLS), pl.col("PitchCall").is_not_null()),
             "Zone %": (report.HAS_LOC & report.IN_ZONE, report.HAS_LOC),
             **game_report.ZONE_RATES,
             "WHIFF %": (pl.col("PitchCall") == "StrikeSwinging", swing)}
    return {k: (rates[k][0] & rates[k][1], rates[k][1]) for k in LEAGUE_RATES}


def league_pitch(name: str | None) -> str | None:
    """An arsenal pitch name or TrackMan tag -> its row in the reference (Four-Seam and FourSeamFastBall ->
    Fastball, Two-Seam -> Sinker, Sweeper -> Slider, ChangeUp -> Changeup)."""
    groups = {**LEAGUE_GROUPS, **{report.PITCH_NAMES.get(t, t): g for t, g in LEAGUE_GROUPS.items()}}
    return groups.get(name) or report.PITCH_NAMES.get(name, name)


def _league_summary(per_pitcher: pl.DataFrame) -> dict:
    """Per-pitcher sums -> pitches, and per rate: the pooled average (every pitch counts once) and the
    spread (SD) of D1 pitchers' own rates, over pitchers with LEAGUE_MIN pitches in its denominator."""
    out = {"pitches": int(per_pitcher["pitches"].sum())}
    for k in LEAGUE_RATES:
        n, d = per_pitcher[f"{k}|n"].sum(), per_pitcher[f"{k}|d"].sum()
        q = per_pitcher.filter(pl.col("PitcherId").is_not_null() & (pl.col(f"{k}|d") >= LEAGUE_MIN))
        sd = (q[f"{k}|n"] / q[f"{k}|d"]).std() if q.height > 1 else None
        out[k] = {"avg": round(n / d, 4) if d else None, "sd": None if sd is None else round(sd, 4), "pitchers": q.height}
    return out


def league_path(year: str = LEAGUE_YEAR):
    return config.ARTIFACTS_DIR / f"usc_league_D1_{year}.json"


def build_league(year: str = LEAGUE_YEAR) -> dict:
    """The season's D1 reference (verified games, both serving partitions), overall and per pitch type
    (LEAGUE_GROUPS combined) -> league_path(year)."""
    t0 = time.time()
    d = (pl.scan_parquet(str(config.PITCHES_DIR / "*" / year / "*.parquet"))
           .filter((pl.col("Level") == "D1") & pl.col("is_verified")))
    sums = [x for k, (num, den) in _rate_parts().items() for x in (num.sum().alias(f"{k}|n"), den.sum().alias(f"{k}|d"))]
    overall = _league_summary(d.group_by("PitcherId").agg(pl.len().alias("pitches"), *sums).collect())
    typed = (d.filter(pl.col("TaggedPitchType").is_not_null() & ~pl.col("TaggedPitchType").is_in(report.NON_PITCHES))
              .with_columns(pl.col("TaggedPitchType").replace(LEAGUE_GROUPS).replace(report.PITCH_NAMES).alias("pt")))
    per = typed.group_by("PitcherId", "pt").agg(pl.len().alias("pitches"), *sums).collect()
    by = {pt: _league_summary(g) for (pt,), g in per.group_by("pt")}
    ref = {"built": time.strftime("%Y-%m-%d %H:%M"), "level": "D1", "year": year,
           "source": f"pipeline_v2/serving/pitches/*/{year}, Level D1, verified games",
           "how": (f"avg: pooled rate (every pitch counts once); sd: spread of D1 pitchers' own rates, pitchers with "
                   f"{LEAGUE_MIN}+ pitches in the rate's denominator; combined: {LEAGUE_GROUPS}"),
           "overall": overall, "by_pitch": dict(sorted(by.items(), key=lambda kv: -kv[1]["pitches"]))}
    league_path(year).write_text(json.dumps(ref, indent=1))
    league_reference.cache_clear()
    print(f"saved {league_path(year).name}: {overall['pitches']:,} pitches ({time.time() - t0:.0f}s)")
    return ref


@lru_cache(maxsize=4)
def league_reference(year: str = LEAGUE_YEAR) -> dict | None:
    """The built reference, or None until ``--build`` has been run on this machine."""
    p = league_path(year)
    return json.loads(p.read_text()) if p.exists() else None


# ── Why this grade: one row of the Stuff table, explained ───────────────────────────────────────────────
# The app's version of why() in backend/research/stuff_model/stuff_explain.ipynb §10, which has the reasoning and
# the audit. It starts from the row's real pitches, tagged and referenced by the rules that graded them, so
# the first two answers add up to the table's number. The season comparisons come from a small built file
# (build_why), so a click doesn't load a season.
WHY_YEAR = "2026"                        # the season a pitch is compared with
WHY_MIN_N = 100                          # pitches a pitcher's pitch type needs to count in the comparisons
WHY_INPUTS = ["RelSpeed", "IVB", "HB", "SpinRate", "RelHeight", "RelSideM"]
WHY_REFS = ["refV", "refIVB", "refHB"]   # the primary fastball, in the comparisons file
WHY_DELTAS = {"dVelo": ("RelSpeed", "refV"), "dIVB": ("IVB", "refIVB"), "dHB": ("HB", "refHB")}   # this pitch minus its fastball
WHY_WORDS = {"RelSpeed": "velo", "IVB": "IVB", "HB": "HB (arm side +)", "SpinRate": "spin", "RelHeight": "release height",
             "RelSideM": "release side", "dVelo": "fastball velo delta", "dIVB": "fastball IVB delta", "dHB": "fastball HB delta"}
WHY_FMT = {"RelSpeed": ".1f", "IVB": ".1f", "HB": ".1f", "SpinRate": ",.0f", "RelHeight": ".2f", "RelSideM": ".2f",
           "dVelo": ".1f", "dIVB": ".1f", "dHB": ".1f"}
WHY_UNIT = {"RelSpeed": "mph", "IVB": "in", "HB": "in", "SpinRate": "rpm", "RelHeight": "ft", "RelSideM": "ft",
            "dVelo": "mph", "dIVB": "in", "dHB": "in"}
TYPE_NAME = {"FS": "Four-seam", "SI": "Sinker", "CT": "Cutter", "SL": "Slider", "CB": "Curveball", "CH": "Changeup",
             "SP": "Splitter"}
GROUP_NAME = {"FB": "fastball", "BB": "breaking ball", "OS": "offspeed pitch"}
WHY_FALLBACK = {"FB": "FB_1|FS", "BB": "BB|SL", "OS": "OS|CH"}   # a pitch type the season has no comparison for
WHY_READ, WHY_ALL = "read", "all"        # the Compare with dropdown's two fixed choices; the rest are pitch types
WHY_ROLE_NAME = {"FB_1": "primary fastball", "FB_2+": "second fastball", "BB": "breaking ball", "OS": "offspeed pitch"}
WHY_ROLE_ALL = {"FB_1": "All primary fastballs", "FB_2+": "All second fastballs", "BB": "All breaking balls", "OS": "All offspeed pitches"}
WHY_GRID_STEP = {"RelSpeed": 1.0, "IVB": 2.0, "HB": 1.5, "SpinRate": 150.0, "RelHeight": 0.25, "RelSideM": 0.25,
                 "dVelo": 1.0, "dIVB": 2.0, "dHB": 1.5}    # one cell of the grid, about half a D1 SD of each input
WHY_GRID_HALF = 4                        # an axis has 2 x this + 1 cells
WHY_GRID_BACK = 2                        # cells kept on the side that lowers the grade
WHY_GRID_LEAN = (2, 4)                   # cells away from now that are graded to find the better side
WHY_GRID_CLEAR = 2.0                     # Stuff+ points between the sides for one to count as better
WHY_GRID_PITCHES = 15                    # most pitches a grid grades (about 4 s for a fastball, 81 cells)
TAG_TYPE = {"FourSeamFastBall": "FS", "TwoSeamFastBall": "SI", "Sinker": "SI", "Cutter": "CT", "Slider": "SL", "Sweeper": "SL",
            "Curveball": "CB", "ChangeUp": "CH", "Splitter": "SP"}   # the plain "Fastball" tag can be either fastball


def _why_role() -> pl.Expr:
    return (pl.when(pl.col("retag").is_in(["Fastball_1", "Fastball"])).then(pl.lit("FB_1"))
              .when(pl.col("group") == "FB").then(pl.lit("FB_2+")).otherwise(pl.col("group")).alias("role"))


def _why_to_model(d: pl.DataFrame, zero_diffs: bool) -> pl.DataFrame:
    """Inputs -> the model's features. A primary fastball is its own reference, so its differentials are 0."""
    av, ah = pitch_lab.acc_from_movement(pl.col("RelSpeed"), pl.col("IVB"), pl.col("HB"), pitch_lab.reference()["conversion"])
    if zero_diffs:
        diffs = [pl.lit(0.0).alias(c) for c in WHY_DELTAS]
    elif "dVelo" in d.columns:                       # the deltas are given
        diffs = []
    else:
        diffs = [(pl.col(p) - pl.col(r)).alias(c) for c, (p, r) in WHY_DELTAS.items()]
    return d.with_columns(av.alias("acc_v"), ah.alias("acc_h"), *diffs)


def why_path(year: str = WHY_YEAR):
    from backend.models import stuff_v1 as sv
    return config.ARTIFACTS_DIR / f"usc_why_D1_{sv.MODEL_VERSION}_{year}.json"


def build_why(year: str = WHY_YEAR) -> dict:
    """Per pitch type (role | type), the season's pitch types with WHY_MIN_N+ pitches: their average inputs,
    average grade and the average grade at their own average shapes (the "typical numbers" line) -> why_path(year)."""
    from backend.models import stuff_store, stuff_v1 as sv
    t0 = time.time()
    model = data._stuff_model()
    src = data._stuff_src("stuff")
    saved = pl.read_parquet(stuff_store._score_path(year), columns=["PitchUID", "group", "retag", "type", src]).rename({src: "stuff"})
    d = sv.load_pitches([year], with_outcomes=False).join(saved.drop("stuff"), on="PitchUID", how="inner")
    d, _ = sv.merge_pitcher_ids(d)
    d = sv._training_pids(d, model.pid_map)
    P = (sv.add_features(d).filter(pl.col("gradeable") & pl.col("type").is_not_null()).with_columns(_why_role())
           .join(saved.select("PitchUID", "stuff"), on="PitchUID", how="left"))
    PT = (P.group_by("pid", "retag", "type", "group", "role", "PitcherThrows")
           .agg(pl.len().alias("n"), *[pl.col(c).mean() for c in [*WHY_INPUTS, "dVelo", "dIVB", "dHB", "stuff"]])
           .filter(pl.col("n") >= WHY_MIN_N)
           .with_columns(refV=pl.col("RelSpeed") - pl.col("dVelo"), refIVB=pl.col("IVB") - pl.col("dIVB"),
                         refHB=pl.col("HB") - pl.col("dHB"))
           .filter(pl.all_horizontal(pl.col(WHY_REFS).is_not_null())))
    types = {}
    for (role, t), items in PT.group_by("role", "type"):
        own = _why_to_model(items.with_row_index("i").select(pl.col("i").cast(pl.String).alias("PitchUID"), "group",
                                                             "PitcherThrows", *WHY_INPUTS, *WHY_REFS), role == "FB_1")
        types[f"{role}|{t}"] = {"type": t, "n": items.height, "type_mean": float(items["stuff"].mean()),
                                "own_mean": float(model.grade(own)["stuff"].mean()),
                                "base": items.select([pl.col(c).mean() for c in WHY_INPUTS + WHY_REFS]).row(0, named=True)}
    ref = {"built": time.strftime("%Y-%m-%d %H:%M"), "year": year, "model": sv.MODEL_VERSION, "view": data.STUFF_VIEW,
           "min_n": WHY_MIN_N, "types": types}
    why_path(year).write_text(json.dumps(ref))
    why_reference.cache_clear()
    print(f"saved {why_path(year).name}: {PT.height:,} pitch types ({time.time() - t0:.0f}s)")
    return ref


@lru_cache(maxsize=2)
def why_reference(year: str = WHY_YEAR) -> dict | None:
    """The built comparisons, or None until ``--build`` has been run for this model."""
    p = why_path(year)
    return json.loads(p.read_text()) if p.exists() else None


def _season_pitches(year: str, ids: list[str], model) -> pl.DataFrame:
    """A pitcher's whole saved season in model space: the score table's tags and the season's fastball
    reference, as training saw them (the notebook's load_season, for one pitcher)."""
    from backend.models import stuff_store, stuff_v1 as sv
    known = model.pid_map.filter(pl.col("PitcherId").is_in(ids))["pid"].to_list()
    every = set(ids) | set(model.pid_map.filter(pl.col("pid").is_in(known))["PitcherId"].to_list())
    d = sv.load_pitches([year], with_outcomes=False).filter(pl.col("PitcherId").is_in(list(every)))
    tags = (pl.scan_parquet(stuff_store._score_path(year)).select("PitchUID", "group", "retag", "type")
              .join(d.lazy().select("PitchUID"), on="PitchUID", how="semi").collect())
    d, _ = sv.merge_pitcher_ids(d.join(tags, on="PitchUID", how="inner"))
    return sv.add_features(sv._training_pids(d, model.pid_map)).filter(pl.col("gradeable"))


def _file_pitches(grp: pl.DataFrame, saved: pl.DataFrame, yr: str, model) -> pl.DataFrame | None:
    """The pitches of one file (one season of it) that aren't in the score table, tagged together, the same rows
    data._stuff_for graded them with: a pitcher's tags and fastball reference come from all his rows in the file."""
    from backend.models import stuff_v1 as sv
    miss = grp.filter(sv.can_grade()).join(saved, on="PitchUID", how="anti")
    if miss.height == 0:
        return None
    rows = (grp.join(miss.select("PitcherId").unique(), on="PitcherId", how="semi").unique(subset=["PitchUID"])
               .select(data._STUFF_RAW).with_columns(pl.lit(int(yr), pl.Int32).alias("Year")))
    return sv.prepare(rows, model, n_jobs=1).join(miss.select("PitchUID"), on="PitchUID", how="semi")


def why_pitches(sources: list[pl.DataFrame], pitcher: str, memo: dict) -> pl.DataFrame | None:
    """One pitcher's pitches in model space, with their grades, by the rules that graded them (data._stuff_for):
    a pitch in that season's score table keeps its saved tags and fastball reference; any other is tagged with the
    rows it was graded with. ``sources`` = the frames as they were graded, warmups included, one per file. ``memo``
    (kept with the session's files) holds each file's tagging, which only has to run once."""
    from backend.models import stuff_store, stuff_v1 as sv
    model = data._stuff_model()
    if model is None:
        return None
    keep = ["PitchUID", "pid", "group", "retag", "type", "PitcherThrows", *sv.SHAPE_ALL, *sv.DIFFS]
    parts = []
    for i, df in enumerate(sources):
        for (yr,), grp in df.partition_by("Year", as_dict=True).items():
            if yr is None:
                continue
            path = stuff_store._score_path(str(yr))
            if (i, yr) not in memo:
                saved = (pl.scan_parquet(path).select("PitchUID").join(grp.lazy().select("PitchUID"), on="PitchUID", how="semi").collect()
                         if path.exists() else grp.clear().select("PitchUID"))
                memo[(i, yr)] = (saved, _file_pitches(grp, saved, str(yr), model))
            saved, live = memo[(i, yr)]
            mine = grp.filter(pl.col("Pitcher") == pitcher).select("PitchUID", "PitcherId")
            inside = mine.join(saved, on="PitchUID", how="semi")
            if inside.height:
                ids = inside["PitcherId"].drop_nulls().unique().to_list()
                parts.append(_season_pitches(str(yr), ids, model).join(inside.select("PitchUID"), on="PitchUID", how="semi").select(keep))
            if live is not None:
                parts.append(live.join(mine.select("PitchUID"), on="PitchUID", how="semi").select(keep))
    parts = [p for p in parts if p.height]
    if not parts:
        return None
    P = pl.concat(parts, how="vertical_relaxed").unique(subset=["PitchUID"], keep="first").with_columns(_why_role())
    return P.join(model.grade(P).select("PitchUID", *data.STUFF_COLS), on="PitchUID", how="left")


def why_options(rows: pl.DataFrame) -> list[dict]:
    """The Stuff table's rows that have a grade, in its order: {tag, pitch, n, stuff}."""
    return [{"tag": r["TaggedPitchType"], "pitch": report._pitch_name(r), "n": r["count"], "stuff": r["Stuff"]}
            for r in report._arsenal_agg(rows).iter_rows(named=True) if r.get("Stuff") is not None]


@lru_cache(maxsize=1)
def _hitter_labels() -> dict[int, str]:
    """The model's 16 frozen hitter types -> a name built from their skills (the notebook's §3.2): H1 does the most damage."""
    model = data._stuff_model()
    Z, pi = model.points, model.pop["D1"]
    m = pi @ Z
    z = (Z - m) / np.sqrt(pi @ (Z - m) ** 2)
    words = [("misses a lot", "rarely misses"), ("fouls a lot", "few fouls"), ("does damage", "weak contact")]
    names = []
    for row in z:
        parts = [hi if v > 0.6 else lo for v, (hi, lo) in zip(row, words) if abs(v) > 0.6]
        names.append(", ".join(reversed(parts)) or "average")
    order = np.argsort(-Z[:, 2], kind="stable")
    return {int(i): f"H{h} {names[i]}" for h, i in enumerate(order, start=1)}


@lru_cache(maxsize=2)
def _shapley_matrix(n: int) -> np.ndarray:
    """phi = v @ M, where v[mask] = the grade with the inputs in ``mask`` at the pitch's values, the rest at the average."""
    M = np.zeros((2 ** n, n))
    for mask in range(2 ** n):
        k = bin(mask).count("1")
        for i in range(n):
            if mask >> i & 1:
                M[mask, i] += factorial(k - 1) * factorial(n - k) / factorial(n)
            else:
                M[mask, i] -= factorial(k) * factorial(n - k - 1) / factorial(n)
    return M


def _why_pieces(x: pl.DataFrame, g: str, hand: str) -> dict:
    """The run breakdown of the row's pitches: base + whiff + foul + contact = the grade. base = a pitch with the group's
    average pieces; each step swaps in this pitch's piece, whiff first, along the chain."""
    model, view = data._stuff_model(), data.STUFF_VIEW
    w, pi = model.weights[g][hand], model.pop[view]
    J = np.outer(pi, w)
    fix = model.p4_fix[g] if view == "P4" else {}
    mu, pts = model._center(view, g, "runs"), -10 / model.scale[g]["runs"][1]        # one run per swing, in points
    wb, fb, vb = model.avg_pieces[g]
    V0 = model._value(wb, fb, vb)
    n, K = x.height, len(model.points)
    whiff, foul, contact, rel, by = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros((n, K)), np.zeros((n, K))
    for a, m_, (pw, pf, pv) in model._pieces(g, x, model.points):
        V1, V2, V3 = model._value(pw, fb, vb), model._value(pw, pf, vb), model._value(pw, pf, pv)
        whiff[a:a + m_] = pts * np.einsum("nkc,kc->n", V1 - V0, J)
        foul[a:a + m_] = pts * np.einsum("nkc,kc->n", V2 - V1, J)
        contact[a:a + m_] = pts * np.einsum("nkc,kc->n", V3 - V2, J)
        by[a:a + m_] = 100 + pts * (np.einsum("nkc,c->nk", V3, w) - mu)
        rel[a:a + m_] = pts * np.einsum("nkc,c->nk", V3 - V0, w)
    avg = {p: float((a * J).sum()) + fix.get(p, 0.0) for p, a in zip(("whiff", "foul", "contact"), model.avg_pieces[g])}
    return {"base": 100 + pts * (float((V0 * J).sum()) + fix.get("runs", 0.0) - mu), "pts_whiff": float(whiff.mean()),
            "pts_foul": float(foul.mean()), "pts_contact": float(contact.mean()), "by": by.mean(0), "rel": rel.mean(0), "avg": avg}


def _why_comparison(ref: dict, role: str, t: str, g: str, versus: str) -> tuple[dict, list[dict], str]:
    """What a row's Why ladder is measured against -> (the comparison, the dropdown's options, the choice in effect).
    The default is the model's read of the row. The others are the same role's other pitch types and all of them
    together (weighted by pitch types, so it's the plain average of every pitch type in the role). A choice this row
    doesn't have goes back to the default. Only the row's own group is offered: each group has its own model, and a
    grade only compares inside its group."""
    types = {k.split("|")[1]: v for k, v in ref["types"].items() if k.startswith(f"{role}|")}
    read = types.get(t) or ref["types"][WHY_FALLBACK[g]]
    others = sorted((k for k in types if k != read["type"]), key=lambda k: -types[k]["n"])
    options = [{"label": f"{TYPE_NAME[read['type']]} (the model's read)", "value": WHY_READ},
               *[{"label": TYPE_NAME[k], "value": k} for k in others]]
    if len(types) > 1:
        options.append({"label": WHY_ROLE_ALL[role], "value": WHY_ALL})
    if versus == WHY_ALL and len(types) > 1:
        items, n = types.values(), sum(i["n"] for i in types.values())
        mean = lambda f: sum(f(i) * i["n"] for i in items) / n
        return ({"type": WHY_ALL, "name": WHY_ROLE_NAME[role], "type_mean": mean(lambda i: i["type_mean"]), "own_mean": mean(lambda i: i["own_mean"]),
                 "base": {c: mean(lambda i, c=c: i["base"][c]) for c in read["base"]}}, options, WHY_ALL)
    if versus in others:
        return {**types[versus], "name": TYPE_NAME[versus].lower()}, options, versus
    return {**read, "name": TYPE_NAME[read["type"]].lower()}, options, WHY_READ


def _why_inputs(own: dict, item: dict, role: str, g: str, hand: str) -> dict | None:
    """Exact Shapley values of the row's average shape vs. the average shape of ``item``: every combination of the
    model's inputs graded, each credited its average effect. The players are what the model sees: the pitch's own
    velo, movement, spin and release, and its deltas to its fastball, so a delta line has the whole delta's credit and
    a velo, IVB or HB line holds its delta where it is. None: no primary fastball to measure against."""
    model = data._stuff_model()
    zero = role == "FB_1"
    players = WHY_INPUTS if zero else WHY_INPUTS + list(WHY_DELTAS)
    if not zero and any(own[c] is None for c in WHY_DELTAS):
        return None
    base = {**item["base"], **{c: item["base"][p] - item["base"][r] for c, (p, r) in WHY_DELTAS.items()}}
    d = pl.DataFrame([{"PitchUID": str(m), "mask": m, "group": g, "PitcherThrows": hand,
                       **{p: (own[p] if m >> j & 1 else base[p]) for j, p in enumerate(players)}} for m in range(2 ** len(players))])
    d = _why_to_model(d, zero)
    v = d.join(model.grade(d).select("PitchUID", "stuff"), on="PitchUID").sort("mask")["stuff"].to_numpy().astype(float)
    return {"players": players, "phi": v @ _shapley_matrix(len(players)), "base": base, "start": float(v[0]),
            "type": item["type"], "name": item["name"], "type_mean": item["type_mean"], "typical": item["own_mean"] - float(v[0])}


def _why_sentence(r: dict, b: dict, il: dict | None, g: str) -> str:
    """One spoken sentence: the direction, the piece, then the inputs that go the same way and the one against."""
    gn, label = GROUP_NAME[g], data.STUFF_VIEW_LABEL[data.STUFF_VIEW]
    diff = r["stuff"] - b["base"]
    if abs(diff) < 1:
        s, sign = f"It's about average for {'an' if gn[0] in 'aeiou' else 'a'} {gn} {label}", 1
    else:
        sign = 1 if diff > 0 else -1
        s = f"It's {abs(diff):.0f} point{'s' if round(abs(diff)) != 1 else ''} {'above' if sign > 0 else 'below'} the average {gn} {label}"
    said = {"pts_whiff": ("misses more bats", "misses fewer bats"), "pts_foul": ("gets more fouls", "gets fewer fouls"),
            "pts_contact": ("gets weaker contact", "gets harder contact")}
    same = sorted([(k, b[k]) for k in said if b[k] * sign > 0], key=lambda v: -abs(v[1]))
    if same and abs(diff) >= 1:
        s += f", mostly because it {said[same[0][0]][0 if same[0][1] > 0 else 1]} ({same[0][1]:+.0f})"
    s += "."
    if il is None:
        return s
    clean = lambda p: WHY_WORDS[p].replace(" (arm side +)", "")
    ph = [(p, float(v)) for p, v in zip(il["players"], il["phi"]) if abs(v) >= 1]
    good = sorted([x for x in ph if x[1] * sign > 0], key=lambda v: -abs(v[1]))[:2]
    bad = sorted([x for x in ph if x[1] * sign < 0], key=lambda v: -abs(v[1]))[:1]
    if good:
        s += " The numbers behind that: " + " and ".join(f"{clean(p)} ({v:+.0f})" for p, v in good) + "."
    if bad:
        s += f" Going the other way: {clean(bad[0][0])} ({bad[0][1]:+.0f})."
    return s


def _why_row(rows: pl.DataFrame, P: pl.DataFrame | None, tag: str) -> tuple[pl.DataFrame, pl.DataFrame] | None:
    """A Stuff table row's pitches in model space (one group, one hand) + the grades the table shows for them.
    None: this pitcher has no graded pitch with this tag."""
    if P is None:
        return None
    d = rows.filter(pl.col("TaggedPitchType") == tag, pl.col("Stuff").is_not_null())
    if d.height == 0:
        return None
    shown = d.filter(report._IN_MAIN_GROUP).select("PitchUID", pl.col("Stuff").alias("_shown"))
    x = P.join(shown, on="PitchUID", how="inner")
    if x.height == 0:
        return None
    return x.filter(pl.col("group") == x["group"].mode().sort()[0], pl.col("PitcherThrows") == x["PitcherThrows"].mode().sort()[0]), shown


def _why_own(x: pl.DataFrame) -> dict:
    """A row's average inputs and fastball deltas."""
    return x.select(*[pl.col(c).mean() for c in [*WHY_INPUTS, *WHY_DELTAS]]).row(0, named=True)


def why_axes(role: str, has_fastball: bool) -> list[str]:
    """The inputs a row's grid can move: all nine of the Why ladder's. A primary fastball is its own
    reference, and a pitcher with no primary fastball has none to move, so those rows get only the six."""
    return WHY_INPUTS + list(WHY_DELTAS) if role != "FB_1" and has_fastball else list(WHY_INPUTS)


def why_grid(rows: pl.DataFrame, P: pl.DataFrame | None, tag: str, xf: str, yf: str, cap: int | None = None) -> dict | None:
    """Stuff+ on a grid of two Why inputs, everything else held: the row's real pitches, each moved by the same
    amount and regraded. Moving velo, IVB or HB carries that delta with it, because the fastball stays put, unless
    that delta is its own axis, which then sets it; a primary fastball is its own reference, so its deltas stay.
    Breaking balls are graded on acceleration, so a move changes theirs by what Pitch Lab's conversion says it should.

    A big row grades ``cap`` of its pitches, evenly spaced across their grades, and the grid is then shifted so
    the "now" cell is the row's grade (the size of the error is in usc_decisions.md section 9)."""
    got = _why_row(rows, P, tag)
    if got is None:
        return None
    x, _ = got
    cap = WHY_GRID_PITCHES if cap is None else cap
    own = _why_own(x)
    use = x if x.height <= cap else x.sort("stuff")[np.linspace(0, x.height - 1, cap).round().astype(int).tolist()]
    kx, ky = _why_lean(use, xf), _why_lean(use, yf)
    cells = [(a, b) for b in ky for a in kx]
    z = _why_moved(use, xf, yf, cells).reshape(len(ky), len(kx))
    ix, iy = kx.index(0), ky.index(0)
    z = z + (float(x["stuff"].cast(pl.Float64).mean()) - z[iy, ix])
    return {"xf": xf, "yf": yf, "x": [own[xf] + a * WHY_GRID_STEP[xf] for a in kx], "y": [own[yf] + b * WHY_GRID_STEP[yf] for b in ky],
            "z": z.tolist(), "now": (iy, ix), "own": own, "n": x.height, "used": use.height, "role": x["role"].drop_nulls().mode().sort()[0]}


def _why_moved(use: pl.DataFrame, xf: str, yf: str | None, cells: list[tuple[int, int]]) -> np.ndarray:
    """The pitches' mean Stuff+ at each (x cells, y cells) away from where they are now, in the order given."""
    conv = pitch_lab.reference()["conversion"]
    frame = pl.DataFrame({"_c": list(range(len(cells))), "_dx": [a * WHY_GRID_STEP[xf] for a, _ in cells],
                          "_dy": [b * (WHY_GRID_STEP[yf] if yf else 0.0) for _, b in cells]})
    mv = lambda c: pl.col("_dx") if c == xf else pl.col("_dy") if c == yf else pl.lit(0.0)
    dv, di, dh = mv("RelSpeed"), mv("IVB"), mv("HB")
    a0 = pitch_lab.acc_from_movement(pl.col("RelSpeed"), pl.col("IVB"), pl.col("HB"), conv)
    a1 = pitch_lab.acc_from_movement(pl.col("RelSpeed") + dv, pl.col("IVB") + di, pl.col("HB") + dh, conv)
    # a delta axis sets the delta; otherwise the pitch's own move carries it along (the fastball stays put)
    delta = lambda c, p: pl.when(pl.col("role") != "FB_1").then(pl.col(c) + (mv(c) if c in (xf, yf) else mv(p))).otherwise(pl.col(c)).alias(c)
    d = (use.with_row_index("_p").join(frame, how="cross")
            .with_columns(*[(pl.col(c) + mv(c)).alias(c) for c in WHY_INPUTS],
                          (pl.col("acc_v") + a1[0] - a0[0]).alias("acc_v"), (pl.col("acc_h") + a1[1] - a0[1]).alias("acc_h"),
                          *[delta(c, p) for c, (p, _) in WHY_DELTAS.items()],
                          pl.format("{}|{}", "_c", "_p").alias("PitchUID")))
    g = (d.select("PitchUID", "_c").join(data._stuff_model().grade(d).select("PitchUID", "stuff"), on="PitchUID")
          .group_by("_c").agg(pl.col("stuff").cast(pl.Float64).mean()).sort("_c"))
    return g["stuff"].to_numpy()


def _why_lean(use: pl.DataFrame, f: str) -> list[int]:
    """An axis's cells, counted from where the row is now. Most of them go the way that raises the grade, so the grid
    shows the way up, with a couple the other way; an input with no clear better side stays centered."""
    probe = [-WHY_GRID_LEAN[1], -WHY_GRID_LEAN[0], WHY_GRID_LEAN[0], WHY_GRID_LEAN[1]]
    v = _why_moved(use, f, None, [(a, 0) for a in probe])
    diff = float(v[2:].mean() - v[:2].mean())
    if abs(diff) < WHY_GRID_CLEAR:
        return list(range(-WHY_GRID_HALF, WHY_GRID_HALF + 1))
    back, ahead = WHY_GRID_BACK, 2 * WHY_GRID_HALF - WHY_GRID_BACK
    return list(range(-back, ahead + 1)) if diff > 0 else list(range(-ahead, back + 1))


def explain(rows: pl.DataFrame, P: pl.DataFrame | None, tag: str, versus: str = WHY_READ) -> dict | None:
    """One Stuff table row, explained: its grades plus the What, Why and Who ladders (the first two add up to the
    grade), as plain data for the tab to draw. ``versus`` picks what the Why ladder is measured against
    (_why_comparison); nothing else depends on it. None: this pitcher has no graded pitch with this tag."""
    got = _why_row(rows, P, tag)
    if got is None:
        return None
    x, shown = got
    model, view, label = data._stuff_model(), data.STUFF_VIEW, data.STUFF_VIEW_LABEL[data.STUFF_VIEW]
    g, hand = x["group"][0], x["PitcherThrows"][0]
    role = x["role"].drop_nulls().mode().sort()[0]
    types = x["type"].drop_nulls()
    t = types.mode().sort()[0] if types.len() else WHY_FALLBACK[g].split("|")[1]
    gr = x.select([pl.col(c).cast(pl.Float64).mean() for c in data.STUFF_COLS]).row(0, named=True)
    gn, who = GROUP_NAME[g], "P4 arm's" if view == "P4" else "D1"
    b = _why_pieces(x, g, hand)

    un = {c: (model._center(view, g, p), model.scale[g][p][1]) for c, p in
          [("whiff_plus", "whiff"), ("foul_plus", "foul"), ("contact_plus", "contact")]}
    real = {c: un[c][0] + (-1 if c == "contact_plus" else 1) * (gr[c] - 100) * un[c][1] / 10 for c in un}
    avg = b["avg"]
    what = [{"v": 100.0, "text": f"the average {who} {gn} {label}", "anchor": True},
            {"v": b["base"] - 100, "text": "the group's average pieces", "sub": f"{'an' if gn[0] in 'aeiou' else 'a'} {gn} with those, {label}: {b['base']:.1f}"},
            {"v": b["pts_whiff"], "text": "whiffs", "sub": f"P(whiff) {real['whiff_plus']:.1%} per swing vs {avg['whiff']:.1%} for the average one (Whiff+ {gr['whiff_plus']:.0f})"},
            {"v": b["pts_foul"], "text": "fouls", "sub": f"P(foul | contact) {real['foul_plus']:.1%} vs {avg['foul']:.1%} (Foul+ {gr['foul_plus']:.0f})"},
            {"v": b["pts_contact"], "text": "contact", "sub": f"{real['contact_plus']:+.3f} runs per ball in play vs {avg['contact']:+.3f} (Contact+ {gr['contact_plus']:.0f})"},
            {"v": gr["stuff"], "text": "= Stuff+", "anchor": True}]

    notes, ref = [], why_reference()
    own = _why_own(x)
    item, versus_options, versus = _why_comparison(ref, role, t, g, versus) if ref else (None, [], WHY_READ)
    il = _why_inputs(own, item, role, g, hand) if ref else None
    why, fb_total, why_note = None, None, None
    if il is not None:
        name = il["name"]
        order = np.argsort(-np.abs(il["phi"]))
        typical, other = -il["typical"], gr["stuff"] - il["type_mean"] - float(il["phi"].sum()) + il["typical"]
        why = [{"v": il["type_mean"], "text": f"the average {name}{'' if name.endswith('pitch') else ' pitch'} type in {WHY_YEAR} {label}", "anchor": True}]
        for j in order:
            p = il["players"][j]
            why.append({"v": float(il["phi"][j]), "text": WHY_WORDS[p],
                        "sub": f"{format(own[p], WHY_FMT[p])} {WHY_UNIT[p]} ({format(il['base'][p], WHY_FMT[p])})"})
        why.append({"v": typical, "text": f"a typical {name}'s numbers",
                    "sub": f"add up to {il['typical']:+.1f} too, because the average shape is a low spot, so that comes off"})
        why.append({"v": other, "text": "everything else", "sub": ("mostly how much the pitches scatter around their average, compared with a typical " + name
                                                                    if g == "FB" else "what one average pitch can't show, such as how the pitches scatter")})
        why.append({"v": gr["stuff"], "text": "= Stuff+", "anchor": True})
        if len(il["players"]) == 9:
            fb_total = float(il["phi"][[il["players"].index(c) for c in WHY_DELTAS]].sum())
    elif ref is None:
        why_note = "This needs its comparisons built: .venv/bin/python -m delispice_app.usc --build"
    else:
        why_note = "No breakdown by input: these pitches have no main fastball to measure the differentials against."

    labels, pi = _hitter_labels(), model.pop[view]
    who_rows = [{"text": labels[int(i)], "v": float(b["rel"][i]), "grade": float(b["by"][i]), "mix": 100 * float(pi[i])}
                for i in np.argsort(-b["rel"])]

    if x.height < WHY_MIN_N:
        notes.append(f"Only {x.height} pitch{'' if x.height == 1 else 'es'}, so the average is thin.")
    mix = x.group_by("retag").len().sort("len", descending=True)
    if mix.filter(pl.col("len") >= 0.1 * x.height).height > 1:
        notes.append("This row mixes the model's pitches: " + ", ".join(f"{a} {100 * n / x.height:.0f}%" for a, n in mix.iter_rows())
                     + ". Each is graded as what it is, then averaged, so the shape shown is a blend and \"everything else\" absorbs the difference.")
    if il is not None and versus == WHY_READ and TAG_TYPE.get(tag, il["type"]) != il["type"]:
        notes.append(f"TrackMan's tag says {report.PITCH_NAMES.get(tag, tag)}, but the model reads most of these as {il['name']}s, "
                     "so that's the default comparison.")
    if x["pid"].n_unique() > 1:
        notes.append(f"The data has this pitcher under {x['pid'].n_unique()} IDs, so this row's pitches use {x['pid'].n_unique()} separate fastball references.")
    gap = abs(gr["stuff"] - float(shown["_shown"].mean()))
    if gap > 0.05 or x.height < shown.height:
        notes.append(f"Check before quoting: this explains {x.height} of the row's {shown.height} graded pitches, and its grade differs from the Stuff table's by {gap:.2f}.")
    return {"pitch": report.PITCH_NAMES.get(tag, tag), "n": x.height, "group": g, "hand": hand, "grades": gr,
            "label": label, "hundred": f"100 = the average {who} {gn} {label}; 10 points = about 1 SD of pitch types. A grade only compares inside its group.",
            "sentence": _why_sentence(gr, b, il, g), "what": what, "why": why, "why_note": why_note, "fb_total": fb_total, "who": who_rows,
            "who_hint": f"Stuff+ points vs. an average {gn} against the same hitter type", "notes": notes,
            "axes": why_axes(role, all(own[c] is not None for c in WHY_DELTAS)), "versus_options": versus_options, "versus": versus}


# ── Export: a standalone HTML page -> PDF, with headless Chrome (no new packages) ───────────────────────
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def chrome() -> str | None:
    """Chrome or Chromium for the PDF: $DELISPICE_CHROME, the Mac app, or one on the PATH (Linux). None if none."""
    for path in (os.environ.get("DELISPICE_CHROME"), MAC_CHROME):
        if path and Path(path).exists():
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    return None


def render(page: str) -> bytes:
    """An HTML page -> PDF bytes, at the page's own @page size. Chrome runs with a throwaway profile, so it
    never touches a Chrome window that's open."""
    if (CHROME := chrome()) is None:
        raise RuntimeError("PDFs need Chrome or Chromium on the machine running the app, and this one has neither")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src, out = tmp / "report.html", tmp / "report.pdf"
        src.write_text(page)
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=30000",
                        f"--user-data-dir={tmp / 'profile'}", "--no-pdf-header-footer", f"--print-to-pdf={out}",
                        src.as_uri()], capture_output=True, timeout=180)
        if not out.exists():
            raise RuntimeError("Chrome didn't write the file.")
        return out.read_bytes()


def main(argv=None):
    p = argparse.ArgumentParser(description="USC tab: build the D1 league reference and the Why comparisons")
    p.add_argument("--build", action="store_true", help="build usc_league_D1_<year>.json and usc_why_D1_<model>_<year>.json")
    p.add_argument("--year", default=LEAGUE_YEAR)
    a = p.parse_args(argv)
    if not a.build:
        p.error("pass --build")
    build_league(a.year)
    build_why(a.year)


if __name__ == "__main__":
    main()
