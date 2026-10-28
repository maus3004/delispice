"""Pitch by pitch: the Game Reports pitcher report's pitch log and pitch card (see pitchbypitch.md).

No Dash, like game_report.py: app.py lays out the section and wires the clicks. Everything comes from
the report's own rows (one pitcher, the chosen games; retags, RV, xRV and Stuff+ already attached):

- ``pitch_store`` turns them into one compact, JSON-ready table in log order (the page keeps it in a
  dcc.Store), with ``log_tree`` grouping it into games -> innings (or Warmup / Live sessions) -> batters.
- The card pieces (``card``, ``release_fig``, ``movement_fig``, ``location_fig``, ``tilt_fig``) read one
  pitch, plus his other pitches of the same type, from that store. A click redraws only the card:
  nothing is re-read or re-scored.
"""
from __future__ import annotations

import math

import plotly.graph_objects as go
import polars as pl

from . import game_report, report

GAME = "game"                          # an upload's kind (game_report.read_upload); database games are always games
SAME_TYPE, SINGLE = "same", "single"   # the card charts: this pitch over his others of its type (the app's
                                       # only mode since 2026-10-24), or alone (still here for notebooks)
# The log's big number (TrackMan's "Pitch Speed ▾"): store column -> (label, decimals). Stuff+ is the
# card's grade (vs the batter's side), so the log and the card always agree.
METRICS = {"velo": ("Velo", 1), "stuff": ("Stuff+", 0), "spin": ("Spin", 0), "ivb": ("IVB", 1), "hb": ("HB", 1)}
UNTAGGED = "Untagged"                  # practice warmups mostly have no pitch type
ABBREV = {"Four-Seam": "4S", "Fastball": "FB", "Two-Seam": "2S", "Sinker": "SI", "Cutter": "CT", "Slider": "SL",
          "Sweeper": "SW", "Curveball": "CB", "Changeup": "CH", "Splitter": "SP", "Knuckleball": "KN",
          "Undefined": "UN", "Other": "OT", UNTAGGED: "—"}
CALLS = {"BallCalled": "Ball", "BallinDirt": "Ball in dirt", "BallIntentional": "Intentional ball",
         "StrikeCalled": "Called strike", "StrikeSwinging": "Swinging strike", "FoulBall": "Foul",
         "FoulBallNotFieldable": "Foul", "FoulBallFieldable": "Foul", "InPlay": "In play",
         "HitByPitch": "Hit by pitch", "AutomaticBall": "Automatic ball", "AutomaticStrike": "Automatic strike"}
RESULTS = {"HomeRun": "Home run", "FieldersChoice": "Fielder's choice"}
PA_ENDS = {"Single": "1B", "Double": "2B", "Triple": "3B", "HomeRun": "HR", "Out": "Out", "FieldersChoice": "FC",
           "Error": "E", "Sacrifice": "SAC"}
_SIDE = {"Right": "R", "Left": "L"}


# ── The store: one row per pitch, in log order ───────────────────────────────────────────────────
def _col(rows: pl.DataFrame, name: str, dtype=pl.Utf8) -> pl.Expr:
    """A column, or blanks when the rows don't carry it."""
    return pl.col(name) if name in rows.columns else pl.lit(None, dtype=dtype)


def _r(name: str, d: int) -> pl.Expr:
    return pl.col(name).cast(pl.Float64).round(d)


def pitch_store(rows: pl.DataFrame, games: pl.DataFrame, kind: str = GAME, retagged=frozenset(),
                upload: bool = False, height_in: int | None = None) -> dict:
    """The rows -> ``{"p": {column: [values]}, "tree": log_tree(...), "default": index, "kind", "upload",
    "shoulder"}``.

    Order: the games as ``games`` lists them, then ``PitchNo`` (``Time`` when it's missing). ``n`` is his
    n-th pitch of that game (TrackMan's PitchNo counts both teams' pitches).
    Grades: the four grades vs the side the batter stood on; with no batter named (practice warmups:
    their BatterSide is set anyway) or no split on file, the overall grades, labeled "overall".
    Arm angle, with the pitcher's height: the release chart's estimate (report.release_fig), the shoulder
    at 70% of height and atan(release height above it / |release side|), per pitch; blank without one."""
    order = {g: i for i, g in enumerate(games["GameID"].to_list())}
    d = (rows.with_columns(pl.col("GameID").replace_strict(order, default=len(order), return_dtype=pl.Int64).alias("_g"),
                           _col(rows, "PitchNo", pl.Int64).alias("_no"), _col(rows, "Time").alias("_t"))
             .sort("_g", "_no", "_t", nulls_last=True)
             .with_columns(pl.int_range(1, pl.len() + 1).over("_g").alias("n")))
    named = _col(d, "Batter").str.strip_chars().fill_null("") != ""
    side = (pl.when(named & (pl.col("BatterSide") == "Right")).then(pl.lit("RHB"))
              .when(named & (pl.col("BatterSide") == "Left")).then(pl.lit("LHB")))
    split = lambda g: pl.when(side == "RHB").then(pl.col(g + "RHB")).when(side == "LHB").then(pl.col(g + "LHB"))
    use_split = split("Stuff").is_not_null()
    grade = lambda g: pl.when(use_split).then(split(g)).otherwise(pl.col(g)).cast(pl.Float64).round(0)
    tag = pl.col("TaggedPitchType")
    pt = (pl.when(tag.is_null()).then(pl.lit(UNTAGGED)).otherwise(tag.replace(report.PITCH_NAMES)))
    shoulder = report.SHOULDER_FRAC * height_in / 12 if height_in else None
    arm = (pl.arctan2(pl.col("RelHeight") - shoulder, pl.col("RelSide").abs()).degrees().round(1)
           if shoulder is not None else pl.lit(None, dtype=pl.Float64))
    out = d.select(
        uid=pl.col("PitchUID"), game=pl.col("GameID"), n=pl.col("n"), date=pl.col("Date"),
        inning=_col(d, "Inning", pl.Int64), half=_col(d, "Top/Bottom"), pa=_col(d, "PAofInning", pl.Int64),
        batter=_col(d, "Batter"), bside=pl.col("BatterSide"), session=_col(d, "PitchSession"),
        tag=tag, pt=pt, retag=pl.col("PitchUID").is_in(list(retagged)), group=_col(d, "StuffGroup"),
        velo=_r("RelSpeed", 1), spin=_r("SpinRate", 0), axis=_r("SpinAxis", 1),
        ivb=_r("InducedVertBreak", 1), hb=_r("HorzBreak", 1),
        rh=_r("RelHeight", 3), rs=_r("RelSide", 3), ext=_r("Extension", 3), arm=arm,
        px=_r("PlateLocSide", 3), py=_r("PlateLocHeight", 3),
        balls=pl.col("Balls"), strikes=pl.col("Strikes"), call=pl.col("PitchCall"), result=pl.col("PlayResult"),
        korbb=pl.col("KorBB"), ev=_r("ExitSpeed", 1), la=_r("Angle", 1),
        rv=_r("RV", 3), xrv=_r("xRV", 3),
        stuff=grade("Stuff"), whiff=grade("WhiffPlus"), foul=grade("FoulPlus"), contact=grade("ContactPlus"),
        glabel=pl.when(use_split).then(pl.lit("vs ") + side)
                 .when(pl.col("Stuff").is_not_null()).then(pl.lit("overall")),
        level=pl.col("Level"), verified=_col(d, "is_verified", pl.Boolean).fill_null(True),
    )
    p = out.to_dict(as_series=False)
    p["color"] = [report.PITCH_COLORS.get(t, "#8c8c8c") for t in p["pt"]]
    p["tilt"] = [report.spin_clock(a) for a in p["axis"]]
    tree = log_tree(p, games, kind)
    return {"p": p, "tree": tree, "default": default_index(tree), "kind": kind, "upload": upload,
            "shoulder": shoulder}


# ── The log: games -> innings / sessions -> batters ──────────────────────────────────────────────
def _ordinal(k: int) -> str:
    return f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def _pa_end(p: dict, i: int) -> str:
    """How the PA ended, from its last pitch by him ('' when it went on after he left)."""
    if p["korbb"][i] == "Strikeout":
        return "K"
    if p["korbb"][i] == "Walk":
        return "BB"
    if p["call"][i] == "HitByPitch":
        return "HBP"
    res = p["result"][i]
    if p["call"][i] == "InPlay" and res and res != "Undefined":
        return PA_ENDS.get(res, res)
    return ""


def log_tree(p: dict, games: pl.DataFrame, kind: str = GAME) -> list[dict]:
    """``[{title, sections: [{title, collapsed, groups: [{title, end, idx}]}]}]``, one entry per game.

    A game file: a section per half-inning he pitched ("Top 1st"), a group per PA (the batter, his side
    and how it ended). A practice file: a section per run of Warmup / Live pitches (``PitchSession``),
    Warmup collapsed, and no batter groups. A game's title (date, opponent) only when there are several."""
    labels = ({g["GameID"]: game_report.game_label(g) for g in games.iter_rows(named=True)}
              if games.height > 1 else {})
    tree, keys = [], []                       # keys: the current (game, section, group), to spot changes
    for i, gid in enumerate(p["game"]):
        if kind == GAME:
            skey = (p["inning"][i], p["half"][i])
            stitle = f"{p['half'][i] or ''} {_ordinal(p['inning'][i])}".strip() if p["inning"][i] is not None else None
            gkey = (p["pa"][i], p["batter"][i])
        else:
            skey, stitle, gkey = p["session"][i], p["session"][i], None
        if not keys or keys[0] != gid:
            tree.append({"title": labels.get(gid), "sections": []})
            keys = [gid, object(), object()]
        secs = tree[-1]["sections"]
        if keys[1] != skey:
            secs.append({"title": stitle, "collapsed": kind != GAME and (skey or "").lower() == "warmup", "groups": []})
            keys[1:] = [skey, object()]
        if keys[2] != gkey:
            title = None
            if kind == GAME and p["batter"][i]:
                hand = _SIDE.get(p["bside"][i] or "")
                title = p["batter"][i] + (f" ({hand})" if hand else "")
            secs[-1]["groups"].append({"title": title, "end": "", "idx": []})
            keys[2] = gkey
        secs[-1]["groups"][-1]["idx"].append(i)
    for game in tree:
        for s in game["sections"]:
            if kind == GAME:
                for g in s["groups"]:
                    g["end"] = _pa_end(p, g["idx"][-1])
            elif s["title"]:
                n = sum(len(g["idx"]) for g in s["groups"])
                s["title"] = f"{s['title']} · {n} pitch{'es' if n != 1 else ''}"
    return tree


def default_index(tree: list[dict]) -> int | None:
    """The pitch selected when the report opens: the first one outside a collapsed (Warmup) section."""
    first = None
    for game in tree:
        for s in game["sections"]:
            for g in s["groups"]:
                if g["idx"]:
                    first = g["idx"][0] if first is None else first
                    if not s["collapsed"]:
                        return g["idx"][0]
    return first


def metric_text(p: dict, i: int, metric: str) -> str:
    """The log row's big number."""
    v = p[metric][i]
    return "—" if v is None else f"{v:.{METRICS[metric][1]}f}"


# ── The card ─────────────────────────────────────────────────────────────────────────────────────
def ft_in(v) -> str:
    """Feet -> TrackMan's feet and inches: 6.25 -> 6'3\", -2.67 -> -2'8\"."""
    if v is None:
        return "—"
    inches = round(abs(v) * 12)
    ft, inch = divmod(inches, 12)
    return f"{'-' if v < 0 and inches else ''}{ft}'{inch}\""


def _num(v, d: int = 1, signed: bool = False) -> str:
    return "—" if v is None else (f"{v:+.{d}f}" if signed else f"{v:.{d}f}")


def card(store: dict, i: int) -> dict:
    """One pitch's card as display strings: the shape tiles, the tag, the grades and the outcome."""
    p = store["p"]
    in_play = p["call"][i] == "InPlay"
    grades = {k: _num(p[k][i], 0) for k in ("stuff", "whiff", "foul", "contact")}
    note = ""
    if p["stuff"][i] is None:
        if not store["upload"] and p["level"][i] != "D1":
            note = "Stuff+ is graded for D1 games only."
        elif any(p[k][i] is None for k in ("velo", "spin", "ivb", "hb")):
            note = "Not graded: the pitch has no spin or movement reading."
        else:
            note = "Not graded."
    korbb, res = p["korbb"][i], p["result"][i]
    if korbb in ("Strikeout", "Walk"):
        result = korbb
    elif in_play and res and res != "Undefined":
        result = RESULTS.get(res, res)
    else:
        result = ""
    b, s = p["balls"][i], p["strikes"][i]
    ev, la = p["ev"][i], p["la"][i]
    return {
        "n": p["n"][i], "pt": p["pt"][i], "abbrev": ABBREV.get(p["pt"][i], p["pt"][i][:2].upper()),
        "group": p["group"][i], "retagged": p["retag"][i], "tag": p["tag"][i], "verified": p["verified"][i],
        "rel_height": ft_in(p["rh"][i]), "rel_side": ft_in(p["rs"][i]), "extension": ft_in(p["ext"][i]),
        "arm": f"{p['arm'][i]:.0f}°" if p["arm"][i] is not None else None,      # None: no height on file
        "ivb": _num(p["ivb"][i]), "hb": _num(p["hb"][i]),
        "velo": _num(p["velo"][i]), "spin": _num(p["spin"][i], 0), "tilt": p["tilt"][i] or "—",
        "glabel": p["glabel"][i], "grades": grades, "grade_note": note,
        "count": f"{b}-{s}" if b is not None and s is not None else "",
        "call": CALLS.get(p["call"][i], p["call"][i] or ""), "result": result,
        "ev_la": (f"{_num(ev, 0)} mph · {_num(la, 0)}°" if in_play and (ev is not None or la is not None) else ""),
        "rv": _num(p["rv"][i], 2, signed=True), "xrv": _num(p["xrv"][i], 2, signed=True) if in_play else None,
        "batter": p["batter"][i], "bside": p["bside"][i],
    }


# ── The card's charts (pitcher's view, like TrackMan's) ──────────────────────────────────────────
_GRID = "#ececec"
_HALO_OPACITY = 0.25


def _frame(fig: go.Figure, xr, yr, w: int, h: int, square: bool = True) -> go.Figure:
    axis = dict(fixedrange=True, zeroline=True, zerolinecolor="#c8c8c8", gridcolor=_GRID, tickfont=dict(size=10, color="#888"))
    fig.update_layout(width=w, height=h, template="plotly_white", showlegend=False,
                      margin=dict(l=30, r=8, t=6, b=24), hovermode="closest",
                      xaxis=dict(range=list(xr), **axis),
                      yaxis=dict(range=list(yr), **axis, **(dict(scaleanchor="x", scaleratio=1) if square else {})))
    return fig


def _same_type(p: dict, i: int, xk: str, yk: str) -> list[int]:
    """His other pitches of this one's type with both coordinates."""
    return [j for j in range(len(p["uid"])) if j != i and p["pt"][j] == p["pt"][i]
            and p[xk][j] is not None and p[yk][j] is not None]


def _points(fig: go.Figure, p: dict, i: int, xk: str, yk: str, mode: str) -> tuple[list, list]:
    """The faded same-type pitches (SAME_TYPE), then the selected pitch with TrackMan's ring. Returns
    every drawn x and y, so the axes can grow to fit an unusual pitch."""
    color, xs, ys = p["color"][i], [], []
    if mode == SAME_TYPE:
        js = _same_type(p, i, xk, yk)
        xs, ys = [p[xk][j] for j in js], [p[yk][j] for j in js]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", marker=dict(color=color, size=7, opacity=0.25),
                                 text=[f"#{p['n'][j]} · {_num(p['velo'][j])} mph" for j in js],
                                 hovertemplate=f"{p['pt'][i]} %{{text}}<extra></extra>"))
    x, y = p[xk][i], p[yk][i]
    if x is not None and y is not None:
        fig.add_trace(go.Scatter(x=[x], y=[y], mode="markers", hoverinfo="skip",
                                 marker=dict(color=color, size=30, opacity=_HALO_OPACITY)))
        fig.add_trace(go.Scatter(x=[x], y=[y], mode="markers", marker=dict(color=color, size=14, line=dict(color="#fff", width=2)),
                                 hovertemplate=f"<b>#{p['n'][i]} {p['pt'][i]}</b><extra></extra>"))
        xs, ys = xs + [x], ys + [y]
    return xs, ys


def _fit(lo: float, hi: float, vals: list, pad: float) -> tuple[float, float]:
    """The default range, widened to fit every value (plus a little room)."""
    return (min([lo, *[v - pad for v in vals]]), max([hi, *[v + pad for v in vals]]))


def release_fig(store: dict, i: int, mode: str = SAME_TYPE) -> go.Figure:
    """Release side x release height (ft), pitcher's view: a RHP releases on the + side. With a height
    on file, the estimated arm runs from the shoulder (0, 70% of height) to this pitch's release point."""
    p, fig = store["p"], go.Figure()
    sh = store.get("shoulder")
    if sh is not None and p["rs"][i] is not None and p["rh"][i] is not None:
        fig.add_trace(go.Scatter(x=[0, p["rs"][i]], y=[sh, p["rh"][i]], mode="lines", hoverinfo="skip",
                                 line=dict(color=p["color"][i], width=3)))
        fig.add_trace(go.Scatter(x=[0], y=[sh], mode="markers", marker=dict(color="#222", size=8),
                                 hovertemplate=f"Shoulder (est.) {sh:.2f} ft<extra></extra>"))
    xs, ys = _points(fig, p, i, "rs", "rh", mode)
    return _frame(fig, _fit(-4.5, 4.5, xs, 0.5), _fit(0, 7.5, ys, 0.5), 300, 262)


def movement_fig(store: dict, i: int, mode: str = SAME_TYPE) -> go.Figure:
    """Horizontal x induced vertical break (in), pitcher's view (a RHP's arm side is +)."""
    fig = go.Figure()
    xs, ys = _points(fig, store["p"], i, "hb", "ivb", mode)
    return _frame(fig, _fit(-25, 25, xs, 3), _fit(-25, 25, ys, 3), 262, 262)


def location_fig(store: dict, i: int, mode: str = SAME_TYPE) -> go.Figure:
    """Where it crossed the plate (ft), pitcher's view (+ is the third-base side), with TrackMan's 3x3 zone."""
    fig = go.Figure()
    z = report.ZONE
    w, h = 2 * z["h"] / 3, (z["t"] - z["b"]) / 3
    for k in (1, 2):                                       # the 3x3 grid inside the zone
        fig.add_shape(type="line", x0=-z["h"] + k * w, x1=-z["h"] + k * w, y0=z["b"], y1=z["t"], line=dict(color="#999", width=1))
        fig.add_shape(type="line", x0=-z["h"], x1=z["h"], y0=z["b"] + k * h, y1=z["b"] + k * h, line=dict(color="#999", width=1))
    fig.add_shape(type="rect", x0=-z["h"], y0=z["b"], x1=z["h"], y1=z["t"], line=dict(color="#333", width=1.5))
    fig.add_shape(type="line", x0=-0.708, y0=0.62, x1=0.708, y1=0.62, line=dict(color="#999", width=2))   # plate front
    xs, ys = _points(fig, store["p"], i, "px", "py", mode)
    fig = _frame(fig, _fit(*report.XR, xs, 0.3), _fit(*report.YR, ys, 0.3), 262, 262)
    fig.update_xaxes(zeroline=False)
    return fig


def _clock_xy(axis_deg: float, r: float) -> tuple[float, float]:
    """A spin axis -> the point at radius r where its tilt sits on a clock face (12:00 up, 3:00 right).
    The same conversion as report.spin_clock, unrounded: 180° is 12:00."""
    theta = math.radians((axis_deg + 180) % 360)          # clockwise from 12
    return r * math.sin(theta), r * math.cos(theta)


def tilt_fig(store: dict, i: int, mode: str = SAME_TYPE) -> go.Figure:
    """A flat clock face with the pitch's tilt as its hand (where the spin points the movement, in the
    pitcher's view: a RHP four-seam's ~1:00 leans to his arm side). SAME_TYPE adds his other pitches of
    the type as faint hands."""
    p = store["p"]
    color = p["color"][i]
    fig = go.Figure()
    fig.add_shape(type="circle", x0=-1, y0=-1, x1=1, y1=1, line=dict(color="#bbb", width=2), fillcolor="#fafafa",
                  layer="below")                         # below the hands (shapes default to above the traces)
    for h in range(12):
        a = math.radians(h * 30)
        fig.add_shape(type="line", x0=0.9 * math.sin(a), y0=0.9 * math.cos(a), x1=math.sin(a), y1=math.cos(a),
                      line=dict(color="#999", width=2 if h % 3 == 0 else 1), layer="below")
    fig.add_trace(go.Scatter(x=[0.74 * math.sin(math.radians(h * 30)) for h in range(12)],
                             y=[0.74 * math.cos(math.radians(h * 30)) for h in range(12)],
                             text=[str(h or 12) for h in range(12)], mode="text", hoverinfo="skip",
                             textfont=dict(size=10, color="#777")))
    if mode == SAME_TYPE:
        xs, ys = [], []
        for j in range(len(p["uid"])):
            if j != i and p["pt"][j] == p["pt"][i] and p["axis"][j] is not None:
                x, y = _clock_xy(p["axis"][j], 0.6)
                xs += [0, x, None]
                ys += [0, y, None]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=color, width=1.5), opacity=0.25, hoverinfo="skip"))
    if p["axis"][i] is not None:
        x, y = _clock_xy(p["axis"][i], 0.6)
        fig.add_trace(go.Scatter(x=[0, x], y=[0, y], mode="lines", line=dict(color=color, width=5), hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=[x], y=[y], mode="markers", marker=dict(color=color, size=12, line=dict(color="#fff", width=2)),
                                 hovertemplate=f"Tilt {p['tilt'][i]}<extra></extra>"))
    fig.add_trace(go.Scatter(x=[0], y=[0], mode="markers", marker=dict(color="#444", size=7), hoverinfo="skip"))
    hidden = dict(range=[-1.08, 1.08], visible=False, fixedrange=True)
    fig.update_layout(width=190, height=190, template="plotly_white", showlegend=False, margin=dict(l=4, r=4, t=4, b=4),
                      xaxis=hidden, yaxis={**hidden, "scaleanchor": "x", "scaleratio": 1})
    return fig
