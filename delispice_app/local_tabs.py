"""The USC tab for delispice_app: it opens on two cards, Pitcher Dive (this file's report, logic in usc.py) and
MAC (mac_tab.py). On the site only the signed-in owner sees it (auth.py: local tabs aren't guest tabs).

Loaded by the local_tabs hook in app.py. This file, usc.py (the tab's logic), mac_tab.py and usc_decisions.md
(what was decided and why) are tracked since 2026-11-18 (Matt's call). Before that they were Mac-only.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
from html import escape
from pathlib import Path

import plotly
import plotly.graph_objects as go
import plotly.io as pio
import polars as pl
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from . import data, game_report, mac_tab, pitch_by_pitch, report, usc

TABS = [("USC", "usc")]
DESKTOP = os.environ.get("DELISPICE_DESKTOP") == "1"     # the Mac desktop window (launch.py), not a browser
# The USC tab opens on a card per view; a card opens its view, and "Back to USC" returns to the cards.
# (label, value, blurb). The cards' look is in assets/usc.css.
VIEWS = [("Pitcher Dive", "dive", "Pair a TrackMan file with its chart and build each pitcher's report."),
         ("MAC", "mac", "Matchup calculator: every pitcher × hitter pair, in xRV per 100 pitches.")]
HOME = "home"

# The paired rows' columns worth eyeballing to check the alignment, in this order (those present are shown).
PREVIEW_COLS = ["Pitcher", "Batter", "PitchNo", "TaggedPitchType", "ChartPitchType", "RelSpeed", "ExitSpeed",
                "ChartPitchCall", "ChartKorBB", "ChartPlayResult", "ChartTaggedHitType",
                "ChartNotes", "Pair"]
NO_CHART = "no chart"                    # a TrackMan file or game uploaded without a chart file


def _team_options() -> list[dict]:
    teams = data.get_index()["Team"].drop_nulls().unique().to_list()
    return sorted(({"label": data.team_label(t), "value": t} for t in teams), key=lambda o: o["label"])


def _decode(contents: str) -> bytes:
    return base64.b64decode(contents.split(",", 1)[1])


def _combine(tms: dict[str, pl.DataFrame], charts: dict[str, pl.DataFrame], tm_names: dict[str, str]):
    """Pair every chart with its TrackMan file or game -> (one frame for the report, per-pair blocks to show)."""
    frames, blocks, used = [], [], set()
    for name, chart in charts.items():
        key = usc.pick_tm(name, chart, tms)
        out, s = usc.pair(tms[key], chart)
        used.add(key)
        frames.append(usc.report_rows(out).with_columns(pl.lit(name).alias("ChartFile")))
        blocks.append((f"{name} ↔ {tm_names[key]}", s, out))
    for key in tms.keys() - used:
        frames.append(usc.live_rows(tms[key]).with_columns(pl.lit(NO_CHART).alias("Pair")))
        blocks.append((f"{tm_names[key]} (no chart file)", None, None))
    return pl.concat(frames, how="diagonal_relaxed"), blocks


# ── Pitch by pitch: Game Reports' widget (pitch_by_pitch.py), with its own ids so the two tabs' clicks
# never cross, and without the retag controls ────────────────────────────────────────────────────────
CHART_UID = "chart:"                     # PitchUID prefix for a pitch only the chart has


def _pbp_store(rows: pl.DataFrame, height_in: int | None) -> dict | None:
    """pitch_by_pitch's store for one pitcher's paired rows: the pitches in paired order (a pitch only the
    chart has sits where it was thrown), grouped by batter. A chart-only pitch gets an id and its
    neighbors' game; a practice file's PAs are numbered from the charted sequence (usc.report_rows)."""
    if rows.height == 0:
        return None
    d = rows.with_row_index("_ord").with_columns(
        pl.coalesce(pl.col("PitchUID"), pl.lit(CHART_UID) + pl.col("_ord").cast(pl.Utf8)).alias("PitchUID"),
        (pl.col("_ord") + 1).cast(pl.Int64).alias("PitchNo"),          # pitch_store orders by PitchNo
        pl.col("GameID", "Date", "Level", "Inning", "Top/Bottom").forward_fill().backward_fill(),
        pl.col("Batter").str.strip_chars(),                            # TrackMan's "Larson, Brandon "
        # a chart-only pitch has no BatterSide: the side the batter stood on for his tracked pitches
        pl.col("BatterSide").fill_null(pl.col("BatterSide").drop_nulls().first().over("Batter")))
    if d["PAofInning"].null_count() == d.height and "PitchofPA" in d.columns:
        d = d.with_columns((pl.col("PitchofPA") == 1).cum_sum().cast(pl.Int64).alias("PAofInning"))
    else:
        d = d.with_columns(pl.col("PAofInning").forward_fill())
    games = (d.group_by("GameID", maintain_order=True).agg(pl.col("Date").drop_nulls().first().alias("game_date"))
              .with_columns(pl.lit(None, pl.Utf8).alias("opponent"), pl.lit(1).alias("games_that_day")))
    retagged = set(d["PitchUID"].to_list()) & set(data.load_retags()["pitch"])
    return pitch_by_pitch.pitch_store(d, games, pitch_by_pitch.GAME, retagged,
                                      upload=d["Level"].is_null().all(), height_in=height_in)


def _pbp_row(p, i, metric, selected):
    """One log row (app._pbp_row with this tab's id type)."""
    tip = " · ".join(b for b in (p["pt"][i], f"{p['balls'][i]}-{p['strikes'][i]}" if p["balls"][i] is not None else "",
                                 pitch_by_pitch.CALLS.get(p["call"][i], p["call"][i] or "")) if b)
    return html.Button([html.Span(className="pbp-dot", style={"background": p["color"][i]}),
                        html.Span(pitch_by_pitch.metric_text(p, i, metric), className="pbp-big"),
                        html.Span(f"#{p['n'][i]}", className="pbp-n")],
                       id={"type": "usc-pbp-row", "i": i}, n_clicks=0, title=tip,
                       className="pbp-row sel" if selected else "pbp-row")


def _pbp_log(store, metric, sel_uid):
    """The whole log (app._pbp_log with this tab's rows)."""
    p, out = store["p"], []
    for game in store["tree"]:
        if game["title"]:
            out.append(html.Div(game["title"], className="pbp-game"))
        for s in game["sections"]:
            body = []
            for g in s["groups"]:
                if g["title"]:
                    body.append(html.Div([html.Span(g["title"], className="pbp-bat-name"),
                                          html.Span(g["end"], className="pbp-bat-end")], className="pbp-bat"))
                body += [_pbp_row(p, i, metric, p["uid"][i] == sel_uid) for i in g["idx"]]
            if s["collapsed"]:
                out.append(html.Details([html.Summary(s["title"], className="pbp-sec"), *body], className="pbp-fold"))
            else:
                out += ([html.Div(s["title"], className="pbp-sec")] if s["title"] else []) + body
    return out


def _pbp_section(m):
    """Game Reports' pitch-by-pitch layout (app.pbp_section) with usc-pbp-* ids and no retag controls."""
    return html.Div(style={"marginTop": "16px", "fontFamily": m.FONT}, children=[
        dcc.Store(id="usc-pbp-store"), dcc.Store(id="usc-pbp-sel"),
        html.Div("Pitch by pitch", style={"fontWeight": 600, "fontSize": "15px", "marginBottom": "6px"}),
        html.Div(style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "alignItems": "flex-start"}, children=[
            html.Div(style={**m._PBP_BOX, "padding": 0, "width": "176px", "flex": "0 0 176px"}, children=[
                html.Div([html.Span("Pitches", style=m._PBP_TITLE),
                          dcc.Dropdown(id="usc-pbp-metric", value="velo", clearable=False,
                                       style={"width": "92px", "fontSize": "12px"},
                                       options=[{"label": lab, "value": k} for k, (lab, _) in pitch_by_pitch.METRICS.items()])],
                         style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                                "padding": "6px 8px", "borderBottom": "1px solid #e2e2e2"}),
                html.Div(id="usc-pbp-log", style={"height": "600px", "overflowY": "auto"}),
            ]),
            html.Div(style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "alignItems": "flex-start",
                            "flex": "1 1 0", "minWidth": "min-content"}, children=[
                html.Div(className="pbp-grid", children=[
                    m._pbp_panel("Release (ft)", "usc-pbp-release", "usc-pbp-rel-tiles"),
                    m._pbp_panel("Movement (in)", "usc-pbp-move", "usc-pbp-move-tiles", tiles_first=False),
                    m._pbp_panel("Location (ft)", "usc-pbp-loc", "usc-pbp-loc-tiles"),
                    m._pbp_panel("Ball spin (tilt)", "usc-pbp-tilt", "usc-pbp-spin-tiles", tiles_first=False),
                ]),
                html.Div(style={"width": "190px", "flex": "0 0 190px", "display": "flex", "flexDirection": "column",
                                "gap": "8px"}, children=[
                    html.Div([html.Div("Tag", style=m._PBP_LABEL), html.Div(id="usc-pbp-tag")], style=m._PBP_BOX),
                    html.Div(id="usc-pbp-grades", style=m._PBP_BOX),
                    html.Div(id="usc-pbp-outcome", style=m._PBP_BOX),
                ]),
            ]),
        ]),
    ])


# Stuff table colors (Matt, 2026-10-31): a gradient from white at 100 to green above and red below. A grade
# is 100 + 10 x standard deviations from its pitch group's baseline (the site's view: the average P4 arm's pitch type
# vs P4 hitters); the color is full at 3 SD.
GRADE_GREEN, GRADE_RED = (99, 190, 123), (248, 105, 107)   # the full colors (a spreadsheet's red-white-green)
GRADE_FULL = 30                                            # points from 100 where the color is full (3 SD)


def _scale_color(f: float) -> str:
    """-1 (full red) .. 0 (white) .. +1 (full green); beyond ±1 stays full."""
    f = max(-1.0, min(1.0, f))
    r, g, b = (round(255 + (c - 255) * abs(f)) for c in (GRADE_GREEN if f > 0 else GRADE_RED))
    return f"#{r:02x}{g:02x}{b:02x}"


def _grade_color(v: int) -> str:
    return _scale_color((v - 100) / GRADE_FULL)


def _find(c, kind):
    """The first component of this type inside c (c itself included)."""
    if isinstance(c, kind):
        return c
    kids = getattr(c, "children", None)
    for k in kids if isinstance(kids, (list, tuple)) else [kids]:
        if hasattr(k, "to_plotly_json") and (hit := _find(k, kind)) is not None:
            return hit
    return None


def _color_rates(block, df: pl.DataFrame, overall: bool = False):
    """Color a table's rate cells (usc.LEAGUE_RATES) on the Stuff table's gradient, by standard deviations
    from the D1 average (usc.league_reference; full at LEAGUE_FULL_SD): the arsenal's against its row's pitch
    type, the line's (``overall``) against all pitches. Higher is green for all six. No reference built, or
    no spread: left uncolored."""
    ref, table = usc.league_reference(), _find(block, html.Table)
    if not ref or table is None:
        return block
    for tr, row in zip(table.children[1:], df.iter_rows(named=True)):     # children[0] is the header row
        lg = ref["overall"] if overall else ref["by_pitch"].get(usc.league_pitch(row["Pitch"]))
        for td, col in zip(tr.children, df.columns):
            x = (lg or {}).get(col) if col in usc.LEAGUE_RATES else None
            if x and x["sd"] and row[col]:
                z = (float(row[col].rstrip("%")) / 100 - x["avg"]) / x["sd"]
                td.style = {**td.style, "background": _scale_color(z / usc.LEAGUE_FULL_SD)}
    return block


def _color_grades(c):
    """Color every grade cell in a Stuff table block (app._stuff_table_block) by _grade_color; a grade cell
    is the only one whose text is a whole number. Returns the block."""
    kids = getattr(c, "children", None)
    if isinstance(kids, str):
        if kids.isdigit():
            c.style = {**(c.style or {}), "background": _grade_color(int(kids))}
        return c
    for k in kids if isinstance(kids, (list, tuple)) else [kids]:
        if hasattr(k, "to_plotly_json"):                     # a Dash component (not text or None)
            _color_grades(k)
    return c


# ── The report's top (name, line, Stuff + charts, arsenal): on screen, and as a page of the reports PDF ──
EXPORT_W = 1500                          # the export's content width, px (one 17 x 11 in landscape page)
EXPORT_CHARTS = {"movement": 460, "release": 560}     # the export's chart widths, px (report.py's own sizes)


def _printable(fig):
    """A figure that prints: WebGL (scattergl) traces draw blank in a PDF, so they become plain scatter."""
    return pio.from_json(fig.to_json().replace('"scattergl"', '"scatter"'))


def _report_view(m, pitcher: str, rows: pl.DataFrame, export: bool = False) -> tuple[list, int | None]:
    """The name, the line (Game Reports' line table, on TrackMan's columns with the chart's calls and
    results filled in by usc.report_rows), the Stuff table (every pitch opened to its vs RHB / vs LHB rows)
    beside the movement and release charts, then Game Reports' arsenal -> (components, the pitcher's
    height). The tables use every pitch (a pitch TrackMan missed counts, with the chart's type and call),
    the charts only the pitches TrackMan measured.

    On screen the two charts share whatever width the row has, so it never scrolls sideways. In the export
    (``export``) everything is drawn at fixed sizes, as static charts that print."""
    present = {c for c in rows.columns if rows[c].null_count() < rows.height}   # a file without a chart: no calls
    tracked = rows.filter(pl.col("Pair") != usc.CHART_ONLY)                     # the pitches TrackMan measured
    bio = data.bio_lookup(pitcher, m._modal_id(tracked, "PitcherId")) if tracked.height else None
    height_in = (bio or {}).get("height_in")
    ars, _contact, stuff = game_report.arsenal_tables(rows, height_in, present)
    ars = ars.drop("RV/100", strict=False)                                       # not in the USC report (Matt)
    line = game_report.line_table(rows, present)
    stuff = [{**g, "key": g["row"]["Pitch"]} for g in stuff]

    def graph(fig, name, grow):
        if export:
            return dcc.Graph(figure=_printable(fig).update_layout(width=EXPORT_CHARTS[name]), config={"staticPlot": True})
        return dcc.Graph(figure=fig.update_layout(width=None, autosize=True),
                         config={"displaylogo": False, "responsive": True},
                         style={"flex": f"{grow} 1 0", "minWidth": "260px", "height": f"{fig.layout.height}px"})

    stuff_block = _color_grades(m._stuff_table_block(stuff, "Stuff",
                                                     keep_open=("usc-stuff-row", [g["key"] for g in stuff])))
    row = html.Div([html.Div(stuff_block, style={"flex": "0 0 auto"}),
                    graph(report.movement_fig(tracked), "movement", 1),
                    graph(report.release_fig(tracked, height_in), "release", 1.25)],    # the release chart has a legend
                   style={"display": "flex", "gap": "10px", "alignItems": "flex-start", "marginTop": "10px",
                          **({"justifyContent": "space-between"} if export else {"overflowX": "auto"})})
    # One block: the tables stretch to its width and the chart row spans it, so the edges line up.
    block = html.Div([
        m._scroll(_color_rates(m.html_table(line, full_width=True, compact=True), line, overall=True)),
        row,
        m._scroll(_color_rates(m.html_table(ars, title=f"Arsenal · {rows.height:,} pitches", full_width=True, compact=True), ars)),
    ], style={"width": f"{EXPORT_W}px"} if export else {"width": "100%", "maxWidth": f"{EXPORT_W}px", "marginTop": "10px"})
    return [m._player_header(pitcher, []), block], height_in


# ── Why this grade: one row of the Stuff table, explained (usc.explain builds it; not in the PDF) ─────────
WHY_BAR = 90                             # px of a ladder row's bar
WHY_GRID_AXES = ("HB", "RelSpeed")       # the Stuff+ grid's axes until they're changed (Matt liked velo gap x HB)


def _why_section(m):
    return html.Div(style={"marginTop": "16px", "fontFamily": m.FONT, "maxWidth": f"{EXPORT_W}px"}, children=[
        html.Div([html.Span("Why this grade", style={"fontWeight": 600, "fontSize": "15px", "marginRight": "16px"}),
                  html.Span("Pitch", style=m.LABEL),
                  dcc.Dropdown(id="usc-why-pitch", options=[], clearable=False, placeholder="Pick a pitch…",
                               style={"width": "340px", "fontSize": "13px"}),
                  html.Span("Compare with", style={**m.LABEL, "marginLeft": "16px"}),
                  dcc.Dropdown(id="usc-why-versus", options=[], value=usc.WHY_READ, clearable=False,
                               style={"width": "260px", "fontSize": "13px"})],
                 style={"display": "flex", "alignItems": "center", "marginBottom": "6px"}),
        dcc.Loading(html.Div(id="usc-why"), type="dot"),
        dcc.Store(id="usc-why-row"),                     # the row the panel shows, and the inputs its grid can move
        html.Div(id="usc-why-grid-wrap", style={"display": "none"}, children=[
            html.Div([html.Span("Stuff+ grid", style={"fontWeight": 600, "fontSize": "14px", "marginRight": "16px"}),
                      html.Span("X axis", style=m.LABEL),
                      dcc.Dropdown(id="usc-why-x", options=[], value=WHY_GRID_AXES[0], clearable=False, style={"width": "240px", "fontSize": "13px"}),
                      html.Span("Y axis", style={**m.LABEL, "marginLeft": "16px"}),
                      dcc.Dropdown(id="usc-why-y", options=[], value=WHY_GRID_AXES[1], clearable=False, style={"width": "240px", "fontSize": "13px"})],
                     style={"display": "flex", "alignItems": "center", "marginTop": "14px"}),
            dcc.Loading([html.Div(id="usc-why-grid-note", style={"fontSize": "12px", "color": "#666", "margin": "4px 0"}),
                         dcc.Graph(id="usc-why-grid", config={"displaylogo": False}, style={"maxWidth": "860px"})], type="dot"),
        ]),
    ])


def _why_delta(f: str, v: float, g: dict) -> float | None:
    """What a velo tick's value makes the fastball velo delta: None if a delta doesn't apply, or is its own axis."""
    own = g["own"]
    if f != "RelSpeed" or g["role"] == "FB_1" or own["dVelo"] is None or "dVelo" in (g["xf"], g["yf"]):
        return None
    return own["dVelo"] + v - own["RelSpeed"]


def _why_axis(f: str, values: list[float], g: dict) -> tuple[str, list[str]]:
    """An axis's title and tick labels. A velo axis's ticks also say its delta to the fastball, the number that matters most."""
    word, fmt = usc.WHY_WORDS[f], usc.WHY_FMT[f]
    ticks = [format(v, fmt).replace("-", "−") for v in values]
    if _why_delta(f, values[0], g) is not None:
        ticks = [f"{t} (delta {_why_num(_why_delta(f, v, g))})" for t, v in zip(ticks, values)]
    return f"{word[0].upper()}{word[1:]}, {usc.WHY_UNIT[f]}", ticks


def _why_grid_fig(g: dict):
    """The grid on the Stuff table's colors (white at 100, full at 70 and 130), a box on where the row is now and a
    dashed line where it crosses 100."""
    xs, ys, z = g["x"], g["y"], g["z"]
    iy, ix = g["now"]
    xf, yf = g["xf"], g["yf"]
    x_title, x_ticks = _why_axis(xf, xs, g)
    y_title, y_ticks = _why_axis(yf, ys, g)
    now = z[iy][ix]

    def tip(i, j):
        lines = [f"{x_title.split(',')[0]}: {format(xs[j], usc.WHY_FMT[xf]).replace('-', '−')} {usc.WHY_UNIT[xf]}",
                 f"{y_title.split(',')[0]}: {format(ys[i], usc.WHY_FMT[yf]).replace('-', '−')} {usc.WHY_UNIT[yf]}"]
        for f, v in ((xf, xs[j]), (yf, ys[i])):
            if _why_delta(f, v, g) is not None:
                lines.append(f"Fastball velo delta: {_why_num(_why_delta(f, v, g))} mph")
        return "<br>".join([*lines, f"<b>Stuff+ {z[i][j]:.1f}</b> ({z[i][j] - now:+.1f} vs now)"])

    hover = [[tip(i, j) for j in range(len(xs))] for i in range(len(ys))]
    fig = go.Figure(go.Heatmap(x=xs, y=ys, z=z, zmin=100 - GRADE_FULL, zmax=100 + GRADE_FULL, showscale=False, xgap=1, ygap=1,
                               colorscale=[[0, _scale_color(-1)], [0.5, "#ffffff"], [1, _scale_color(1)]],
                               texttemplate="%{z:.0f}", textfont=dict(size=12, color="#222"), hovertext=hover, hoverinfo="text"))
    fig.add_trace(go.Contour(x=xs, y=ys, z=z, showscale=False, hoverinfo="skip", contours=dict(coloring="none", start=100, end=100, size=1),
                             line=dict(color="#222", width=2, dash="dash")))
    dx, dy = xs[1] - xs[0], ys[1] - ys[0]
    fig.add_shape(type="rect", x0=xs[ix] - dx / 2, x1=xs[ix] + dx / 2, y0=ys[iy] - dy / 2, y1=ys[iy] + dy / 2, line=dict(color="#222", width=2.5))
    fig.add_annotation(x=xs[ix], y=ys[iy] - dy / 2, text="now", showarrow=False, yanchor="bottom", font=dict(size=10, color="#222"))
    fig.update_xaxes(title=x_title, tickvals=xs, ticktext=[t.replace(" (", "<br>(") for t in x_ticks], showgrid=False, zeroline=False, ticks="")
    fig.update_yaxes(title=y_title, tickvals=ys, ticktext=y_ticks, showgrid=False, zeroline=False, ticks="")
    fig.update_layout(height=480, margin=dict(l=70, r=20, t=10, b=60), plot_bgcolor="#fff", paper_bgcolor="#fff", showlegend=False)
    return fig


def _why_num(v: float, anchor: bool = False) -> str:
    return f"{v:.1f}" if anchor else f"{v:+.1f}".replace("-", "−")


def _why_bar(v: float, top: float):
    """A bar either side of a center line, green for points gained and red for points lost (the Stuff table's colors)."""
    side = "left" if v >= 0 else "right"
    return html.Div([html.Div(style={"position": "absolute", "left": "50%", "top": 0, "bottom": 0, "width": "1px", "background": "#bbb"}),
                     html.Div(style={"position": "absolute", "top": "2px", "bottom": "2px", side: "50%",
                                     "width": f"{50 * min(abs(v) / top, 1) if top else 0:.1f}%", "background": _scale_color(1 if v >= 0 else -1)})],
                    style={"position": "relative", "height": "16px"})


def _why_ladder(steps: list[dict]):
    """Value, bar, then what the line is. The first and last lines are the ladder's ends: bold, no bar."""
    top = max((abs(s["v"]) for s in steps if not s.get("anchor")), default=0)
    out = []
    for s in steps:
        anchor = s.get("anchor", False)
        text = [html.Span(s["text"], style={"fontWeight": 600 if anchor else 400})]
        if s.get("sub"):
            text.append(html.Span(f"  {s['sub']}", style={"color": "#666"}))
        out.append(html.Div([html.Div(_why_num(s["v"], anchor), style={"textAlign": "right", "fontVariantNumeric": "tabular-nums", "fontWeight": 600 if anchor else 400}),
                             html.Div() if anchor else _why_bar(s["v"], top),
                             html.Div(text)],
                            style={"display": "grid", "gridTemplateColumns": f"46px {WHY_BAR}px 1fr", "columnGap": "8px", "alignItems": "start",
                                   "padding": "3px 0", "fontSize": "12.5px", "lineHeight": "1.35",
                                   **({"borderTop": "1px solid #e2e2e2"} if anchor and out else {})}))
    return out


def _why_view(m, r: dict):
    """The grade, the sentence, then What, Why and Who side by side (they wrap on a narrow window)."""
    gr = r["grades"]
    chip = html.Span(f"{gr['stuff']:.0f}", style={"background": _grade_color(round(gr["stuff"])), "padding": "1px 8px", "fontWeight": 700, "fontSize": "16px"})
    side = lambda k: f"{gr[k]:.0f}" if gr.get(k) is not None else "—"
    head = [html.Div([html.Span(f"{r['pitch']} · {r['n']:,} graded pitches · Stuff+ ", style={"fontWeight": 600}), chip,
                      html.Span(f" {r['label']} (vs RHB {side('stuff_rhb')} · vs LHB {side('stuff_lhb')})", style={"color": "#666"})],
                     style={"fontSize": "14px", "marginBottom": "4px"}),
            html.Div(r["sentence"], style={"fontSize": "13px", "marginBottom": "2px"}),
            html.Div(r["hundred"], style={"fontSize": "12px", "color": "#666"})]
    box = {**m._PBP_BOX, "flex": "1 1 400px", "minWidth": "340px"}
    cap = {"fontSize": "11.5px", "color": "#666", "margin": "2px 0 4px"}
    what = html.Div([html.Div("What it does to hitters", style=m._PBP_TITLE),
                     html.Div("Adds up to the grade, through whiffs, fouls and contact.", style=cap), *_why_ladder(r["what"])], style=box)
    deltas = " A delta is this pitch minus its fastball, and the velo, IVB and HB lines hold their deltas where they are." if r["fb_total"] is not None else ""
    why_body = ([html.Div("Adds up to the grade. Each line: what that number is worth against the average in brackets." + deltas, style=cap), *_why_ladder(r["why"])]
                if r["why"] else [html.Div(r["why_note"], style={"fontSize": "12.5px", "color": "#666", "marginTop": "4px"})])
    if r["fb_total"] is not None:
        why_body.append(html.Div(f"The three fastball deltas account for {_why_num(r['fb_total'])} of that.", style={**cap, "marginTop": "4px"}))
    why = html.Div([html.Div("Why, in numbers they throw", style=m._PBP_TITLE), *why_body], style=box)
    top = max(abs(w["v"]) for w in r["who"])
    grid = {"display": "grid", "gridTemplateColumns": f"46px {WHY_BAR}px 1fr 40px 34px", "columnGap": "8px", "alignItems": "center",
            "padding": "2px 0", "fontSize": "12.5px", "lineHeight": "1.3"}
    num = {"textAlign": "right", "fontVariantNumeric": "tabular-nums"}
    who = html.Div([html.Div("Who it plays best and worst against", style=m._PBP_TITLE), html.Div(r["who_hint"] + ".", style=cap),
                    html.Div([html.Div(), html.Div(), html.Div("Hitter type"), html.Div("Grade", style=num), html.Div("Mix", style=num)],
                             style={**grid, "color": "#666", "fontSize": "11px"}),
                    *[html.Div([html.Div(_why_num(w["v"]), style=num), _why_bar(w["v"], top), html.Div(w["text"]),
                                html.Div(f"{w['grade']:.0f}", style=num), html.Div(f"{w['mix']:.0f}%", style={**num, "color": "#666"})], style=grid)
                      for w in r["who"]]], style=box)
    notes = [html.Div(n, style={"fontSize": "12px", "color": "#9a5b00", "marginTop": "4px"}) for n in r["notes"]]
    return html.Div([*head, html.Div([what, why, who], style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "alignItems": "flex-start", "marginTop": "8px"}), *notes])


_UNITLESS = {"fontWeight", "lineHeight", "opacity", "zIndex", "flex", "flexGrow", "flexShrink", "order"}


def _css(style: dict | None) -> str:
    """A Dash style dict -> CSS text (fontSize -> font-size; bare numbers get px, as React does)."""
    out = []
    for k, v in (style or {}).items():
        name = re.sub(r"[A-Z]", lambda c: "-" + c[0].lower(), k)
        out.append(f"{name}: {v}{'px' if isinstance(v, (int, float)) and k not in _UNITLESS else ''}")
    return "; ".join(out)


def _to_html(c) -> str:
    """Dash html components (and static dcc.Graphs) -> HTML text, for the export page."""
    if c is None or c is False:
        return ""
    if isinstance(c, (str, int, float)):
        return escape(str(c))
    if isinstance(c, (list, tuple)):
        return "".join(_to_html(k) for k in c)
    if isinstance(c, dcc.Graph):
        return c.figure.to_html(full_html=False, include_plotlyjs=False, config=c.config)
    tag = type(c).__name__.lower()
    attrs = "".join(f' {a}="{escape(str(v))}"' for a, v in (("style", _css(getattr(c, "style", None))),
                                                            ("class", getattr(c, "className", None)),
                                                            ("title", getattr(c, "title", None))) if v)
    if tag == "details" and getattr(c, "open", False):
        attrs += " open"
    return f"<{tag}{attrs}>{_to_html(getattr(c, 'children', None))}</{tag}>"


EXPORT_PAGE_H = 990                      # px of a 17 x 11 in page inside its margins (10.4 in at 96 per inch)


def _export_page(m, views: list[list]) -> str:
    """Reports as one standalone page for Chrome to print: one 17 x 11 in landscape page per report (a
    slide), the app's styles, Plotly inline. A report taller than a page (a pitcher with many pitch types)
    is scaled down to fit its page. Backgrounds print (print-color-adjust), so colored cells keep their colors."""
    css = (Path(m.__file__).parent / "assets" / "pitcher.css").read_text()
    plotly_js = (Path(plotly.__file__).parent / "package_data" / "plotly.min.js").read_text()
    slides = "".join(f'<section class="slide">{_to_html(v)}</section>' for v in views)
    return f"""<!doctype html><html><head><meta charset="utf-8"><script>{plotly_js}</script><style>{css}
@page {{ size: 17in 11in; margin: 0.3in; }}
* {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; background: #fff; font-family: {m.FONT}; color: #222; }}
.slide {{ width: {EXPORT_W}px; break-after: page; }}
.slide:last-child {{ break-after: auto; }}
</style></head><body>{slides}
<script>
document.querySelectorAll(".slide").forEach(s => {{
  if (s.scrollHeight > {EXPORT_PAGE_H}) s.style.zoom = {EXPORT_PAGE_H} / s.scrollHeight;
}});
</script></body></html>"""


def _free_path(folder: Path, stem: str, ext: str) -> Path:
    """folder/stem.ext, or stem-2.ext, stem-3.ext... when that name is taken (nothing is overwritten)."""
    path, k = folder / f"{stem}{ext}", 2
    while path.exists():
        path, k = folder / f"{stem}-{k}{ext}", k + 1
    return path


def _pitcher_options(df: pl.DataFrame) -> list[dict]:
    """The pitchers in the paired rows, in the order they first threw, with their pitch counts (every
    pitch either file has)."""
    p = (df.with_row_index("_i").filter(pl.col("Pitcher").is_not_null())
           .group_by("Pitcher").agg(pl.len(), pl.col("_i").min()).sort("_i"))
    return [{"label": f"{name} · {n:,} pitches", "value": name} for name, n, _ in p.iter_rows()]


def _block(m, title: str, s: dict | None, out: pl.DataFrame | None):
    if s is None:
        return html.Div([m._title(title), html.Div("Included without chart columns (warmups left out).",
                                                   style={"fontSize": "12px", "color": "#666", "fontFamily": m.FONT})])
    parts = [m._title(title), html.Div(usc.summary_text(s), style={"fontSize": "13px", "fontFamily": m.FONT})]
    if s["renamed"]:
        parts.append(html.Div("Names matched despite spelling: " + ", ".join(f"{c} = {t}" for c, t in s["renamed"]),
                              style={"fontSize": "12px", "color": "#666", "fontFamily": m.FONT}))
    if s["off"]:
        parts.append(m.html_table(pl.DataFrame(s["off"]), title="Plate appearances that didn't line up exactly", compact=True))
    cols = [c for c in PREVIEW_COLS if c in out.columns]
    prev = out.select(cols).with_columns(pl.col(pl.Float64).round(1))
    parts.append(m._title(f"Rows · {out.height:,}"))
    parts.append(html.Div(m.html_table(prev, compact=True), style={"maxHeight": "420px", "overflowY": "auto",
                                                                   "border": "1px solid #eee"}))
    return html.Div(parts, style={"marginTop": "10px"})


def panels(m) -> list:
    """The tab panels, built once at startup. ``m`` is the app module: its ``app`` for callbacks, plus
    helpers and styles (``html_table``, ``_title``, ``LABEL``, ``MAROON``, ``FONT``, ``_UPLOAD_BOX``)."""
    radio = {"inputStyle": {"marginRight": "4px", "marginLeft": "10px"}, "style": m._GR_RADIO, "inline": True}
    note = {"color": "#888", "fontSize": "11px"}
    # Pitcher Dive: the files -> paired rows -> pitcher report view (usc.py; usc_decisions.md).
    dive = html.Div(id="usc-dive", children=[
        dcc.Store(id="usc-key", storage_type="memory"),           # this session's paired rows (kept server-side)
        html.Div([html.Span("Data", style=m.LABEL),
                  dcc.RadioItems(id="usc-source", value="upload", **radio,
                                 options=[{"label": "Database games", "value": "data"},
                                          {"label": "Upload files", "value": "upload"}])],
                 style={"margin": "8px 0"}),
        html.Div(id="usc-up-wrap", children=[
            dcc.Upload(id="usc-upload", accept=".csv,.parquet", multiple=True, max_size=game_report.UPLOAD_MAX_BYTES,
                       style=m._UPLOAD_BOX,
                       children=html.Div(["Drag the TrackMan file and its chart file here (both at once), or ",
                                          html.A("choose them"),
                                          html.Div("A chart file has a ChartPitcher column · kept only for this "
                                                   "browser session", style=note)])),
        ]),
        html.Div(id="usc-db-wrap", style={"display": "none"}, children=[
            html.Div([html.Span("Team", style=m.LABEL),
                      dcc.Dropdown(id="usc-team", options=_team_options(), value=usc.TEAM, clearable=False,
                                   style={"width": "320px", "fontSize": "13px"})],
                     style={"display": "flex", "alignItems": "center"}),
            html.Div([html.Span("Games", style=m.LABEL),
                      dcc.Dropdown(id="usc-games", options=[], value=[], multi=True, placeholder="Pick one or more games…",
                                   style={"width": "640px", "fontSize": "13px"})],
                     style={"display": "flex", "alignItems": "center", "marginTop": "8px"}),
        ]),
        # The report (what the PDF will hold): shown once the files are paired.
        html.Div(id="usc-report-wrap", style={"display": "none"}, children=[
            html.Div([html.Span("Pitcher", style=m.LABEL),
                      dcc.Dropdown(id="usc-pitcher", options=[], clearable=False, placeholder="Pick a pitcher…",
                                   style={"width": "320px", "fontSize": "13px"}),
                      # every pitcher's report (name, line, Stuff + charts, arsenal), one PDF page each
                      html.Button("Generate pitcher reports", id="usc-reports-btn", n_clicks=0,
                                  style={**m._BTN, "marginLeft": "16px"}),
                      dcc.Loading(html.Div(id="usc-reports-note", style={"fontSize": "12px", "marginLeft": "10px"}),
                                  type="dot"),
                      dcc.Download(id="usc-reports-dl")],
                     style={"display": "flex", "alignItems": "center", "marginTop": "12px"}),
            html.Div(id="usc-report", style={"marginTop": "8px", "fontFamily": m.FONT}),
            # Stuff+ per pitch, drawn like the velo chart (Game Reports' chart); not in the PDF, like pitch by pitch
            html.Div(m._graph("usc-stuff-dist"), style={**m._ROW_SCROLL, "marginTop": "16px"}),   # under the arsenal
            _why_section(m),
            _pbp_section(m),
        ]),
        dcc.Loading(html.Div(id="usc-result", style={"marginTop": "10px", "maxWidth": "1200px"}), type="dot"),
        html.Button("Download paired rows (CSV)", id="usc-dl-btn", style={"display": "none"}),
        dcc.Download(id="usc-dl"),
    ])
    # The landing cards, and the way back to them. `usc-view` holds the open view (HOME = the cards); MAC (mac_tab.py)
    # shows itself when it reads "mac" there.
    home = html.Div(id="usc-home", className="usc-cards", children=[
        html.Button([html.Div(label, className="usc-card-title"), html.Div(blurb, className="usc-card-note")],
                    id=f"usc-card-{value}", n_clicks=0, className="usc-card")
        for label, value, blurb in VIEWS])
    back = html.Button("← Back to USC", id="usc-back", n_clicks=0, className="usc-back", style={"display": "none"})
    usc_panel = html.Div(id="usc-panel", style={"display": "none"},
                         children=[dcc.Store(id="usc-view", data=HOME), home, back, dive,
                                   *mac_tab.panels(m, switch="usc-view", prop="data")])

    @m.app.callback(Output("usc-panel", "style"), Input("role-tabs", "value"))
    def cb_usc_toggle(role):
        return {"padding": "8px 4px"} if role == "usc" else {"display": "none"}

    @m.app.callback(Output("usc-view", "data"), Input("usc-back", "n_clicks"),
                    *[Input(f"usc-card-{value}", "n_clicks") for _, value, _ in VIEWS], prevent_initial_call=True)
    def cb_usc_pick(*clicks):
        if not any(clicks):
            raise PreventUpdate
        return HOME if ctx.triggered_id == "usc-back" else ctx.triggered_id.removeprefix("usc-card-")

    @m.app.callback(Output("usc-home", "style"), Output("usc-back", "style"), Output("usc-dive", "style"),
                    Input("usc-view", "data"))
    def cb_usc_view(view):
        hide = {"display": "none"}
        at_home = view in (None, HOME)
        return ({} if at_home else hide), (hide if at_home else {}), ({} if view == "dive" else hide)

    @m.app.callback(Output("usc-up-wrap", "style"), Output("usc-db-wrap", "style"), Input("usc-source", "value"))
    def cb_usc_source(source):
        return ({"display": "none"}, {}) if source == "data" else ({}, {"display": "none"})

    @m.app.callback(Output("usc-games", "options"), Output("usc-games", "value"), Input("usc-team", "value"))
    def cb_usc_games(team):
        return (usc.game_options(team), []) if team else ([], [])

    @m.app.callback(
        Output("usc-key", "data"), Output("usc-result", "children"), Output("usc-dl-btn", "style"),
        Output("usc-pitcher", "options"), Output("usc-pitcher", "value"), Output("usc-report-wrap", "style"),
        Input("usc-upload", "contents"), Input("usc-games", "value"), Input("usc-source", "value"),
        State("usc-upload", "filename"), State("usc-key", "data"),
        State("usc-team", "value"), State("usc-pitcher", "value"), prevent_initial_call=True)
    def cb_usc_build(up_contents, games, source, up_names, old_key, team, old_pitcher):
        """Read the files (or the chosen games), pair each uploaded chart with its TrackMan file, keep the
        result in this server's memory for the session, show how the rows lined up and list the pitchers.
        Database games come straight from the database, with no chart."""
        hidden = {"display": "none"}
        no_report = ([], None, hidden)
        if ctx.triggered_id == "usc-source":
            return old_key, "", hidden, *no_report           # switching source clears the view
        tms, charts, names, errors, odd = {}, {}, {}, [], []
        files = zip(up_names or [], up_contents or []) if source == "upload" else []
        for name, contents in files:
            try:
                raw = _decode(contents)
                if usc.is_chart(raw):
                    charts[name], unknown = usc.read_chart(name, raw)
                    if unknown:
                        odd.append(f"{name}: not a TrackMan value, set to Undefined: {', '.join(unknown)}")
                else:
                    up = game_report.read_upload(name, raw)
                    tms[name], names[name] = up["df"], name
                    if "PitchCall" not in up["present"]:      # read_upload's stand-in (InPlay wherever there's
                        tms[name] = tms[name].with_columns(pl.lit(None, pl.Utf8).alias("PitchCall"))  # an EV): the chart has the calls
            except usc.PairError as e:
                errors.append(str(e))
            except game_report.UploadError as e:
                errors.append(f"{name}: {e}")
            except Exception as e:                           # a malformed upload must never break the page
                errors.append(f"{name}: couldn't read the file ({type(e).__name__}).")
        if source == "data" and games:
            label = {o["value"]: o["label"] for o in usc.game_options(team)}
            for gid, df in usc.game_rows(games).items():
                tms[gid], names[gid] = df, label.get(gid, gid)
        msg = [html.Div(e, style={"color": "#a30000", "fontSize": "12px", "fontFamily": m.FONT}) for e in errors]
        msg += [html.Div(o, style={"color": "#9a5b00", "fontSize": "12px", "fontFamily": m.FONT}) for o in odd]
        if not tms:
            if charts or errors:
                msg.append(html.Div("No TrackMan file to pair with: add the TrackMan file (or pick the games).",
                                    style={"fontSize": "13px", "fontFamily": m.FONT}))
            return old_key, msg, hidden, *no_report
        try:
            combined, blocks = _combine(tms, charts, names)
        except usc.PairError as e:
            return old_key, msg + [html.Div(str(e), style={"color": "#a30000"})], hidden, *no_report
        # The frames as they were graded (warmups included; games picked together), for "Why this grade".
        sources = list(tms.values()) if source == "upload" else [pl.concat(list(tms.values()), how="diagonal_relaxed")]
        key = game_report.put_upload({"df": combined, "sources": sources}, replaces=old_key)
        shown = [_block(m, *b) for b in blocks] if source == "upload" else []     # games have nothing to pair
        pitchers = _pitcher_options(combined)
        values = [o["value"] for o in pitchers]
        pick = old_pitcher if old_pitcher in values else (values[0] if values else None)
        dl = {"marginTop": "10px", "fontSize": "13px", "fontFamily": m.FONT, "cursor": "pointer"} if source == "upload" else hidden
        return key, msg + shown, dl, pitchers, pick, {} if pitchers else hidden

    @m.app.callback(Output("usc-report", "children"), Output("usc-pbp-store", "data"),
                    Output("usc-stuff-dist", "figure"),
                    Input("usc-pitcher", "value"), Input("usc-key", "data"))
    def cb_usc_report(pitcher, key):
        """The report for the chosen pitcher: the name, the line (Game Reports' line table, on TrackMan's
        columns with the chart's calls and results filled in by usc.report_rows), then the Stuff table
        (every pitch opened to its vs RHB / vs LHB rows), movement and release charts in one row, then
        Game Reports' arsenal, then the pitch-by-pitch store and the Stuff+ distribution. The tables and the
        pitch log use every pitch (a pitch TrackMan missed counts, with the chart's type and call); the
        charts only the pitches TrackMan measured."""
        up = game_report.get_upload(key)
        if not pitcher or up is None:
            return "", None, {}
        rows = up["df"].filter(pl.col("Pitcher") == pitcher)
        view, height_in = _report_view(m, pitcher, rows)
        return view, _pbp_store(rows, height_in), game_report.stuff_dist_fig(rows, pitcher, report.hand_of(rows))

    @m.app.callback(Output("usc-why-pitch", "options"), Output("usc-why-pitch", "value"),
                    Input("usc-pitcher", "value"), Input("usc-key", "data"), State("usc-why-pitch", "value"))
    def cb_usc_why_pitches(pitcher, key, old):
        """The Stuff table's graded rows; the same pitch stays selected when the next pitcher throws it."""
        up = game_report.get_upload(key)
        if not pitcher or up is None:
            return [], None
        opts = usc.why_options(up["df"].filter(pl.col("Pitcher") == pitcher))
        values = [o["tag"] for o in opts]
        return ([{"label": f"{o['pitch']} · {o['n']:,} pitches · Stuff+ {o['stuff']:.0f}", "value": o["tag"]} for o in opts],
                old if old in values else (values[0] if values else None))

    @m.app.callback(Output("usc-why", "children"), Output("usc-why-row", "data"), Output("usc-why-versus", "options"),
                    Output("usc-why-versus", "value"), Input("usc-why-pitch", "value"), Input("usc-pitcher", "value"),
                    Input("usc-key", "data"), Input("usc-why-versus", "value"))
    def cb_usc_why(tag, pitcher, key, versus):
        """What, Why and Who for the chosen row. The first one for a file tags that file's pitches; that and each
        pitcher's pitches in model space stay with the session's files, so after that a row takes a second or two,
        and going back to a row and comparison already seen is instant. Compare with stays where it is when the next
        row has that choice, and goes back to the model's read when it doesn't.
        The grid runs after this, off usc-why-row, so the two never tag the same file at once."""
        up = game_report.get_upload(key)
        if not pitcher or up is None:
            return "", None, [], usc.WHY_READ
        if not tag:
            return html.Div("No graded pitches for this pitcher.", style={"fontSize": "13px", "color": "#666"}), None, [], usc.WHY_READ
        try:
            memo = up.setdefault("why", {})
            if pitcher not in memo:
                memo[pitcher] = usc.why_pitches(up["sources"], pitcher, memo)
            cached = ("why", pitcher, tag, versus)
            if cached not in memo:
                memo[cached] = usc.explain(up["df"].filter(pl.col("Pitcher") == pitcher), memo[pitcher], tag, versus or usc.WHY_READ)
            r = memo[cached]
        except Exception as e:                       # an explanation must never break the page
            return (html.Div(f"Couldn't explain this pitch ({type(e).__name__}: {e}).", style={"fontSize": "12px", "color": "#a30000"}),
                    None, [], usc.WHY_READ)
        if not r:
            return "", None, [], usc.WHY_READ
        return _why_view(m, r), {"key": key, "pitcher": pitcher, "tag": tag, "axes": r["axes"]}, r["versus_options"], r["versus"]

    @m.app.callback(Output("usc-why-x", "options"), Output("usc-why-x", "value"), Output("usc-why-y", "options"),
                    Output("usc-why-y", "value"), Input("usc-why-row", "data"), Input("usc-why-x", "value"), Input("usc-why-y", "value"))
    def cb_usc_why_axes(row, x, y):
        """The inputs this row can move. Each axis greys out the other's input, and an axis on an input this row doesn't
        have (a fastball's own fastball) goes back to its default."""
        if not row:
            raise PreventUpdate
        axes = row["axes"]
        x = x if x in axes else next(a for a in (*WHY_GRID_AXES, *axes) if a != y)
        y = y if y in axes and y != x else next(a for a in (*WHY_GRID_AXES[::-1], *axes) if a != x)
        opts = lambda other: [{"label": usc.WHY_WORDS[a], "value": a, "disabled": a == other} for a in axes]
        return opts(y), x, opts(x), y

    @m.app.callback(Output("usc-why-grid", "figure"), Output("usc-why-grid-note", "children"), Output("usc-why-grid-wrap", "style"),
                    Input("usc-why-row", "data"), Input("usc-why-x", "value"), Input("usc-why-y", "value"))
    def cb_usc_why_grid(row, x, y):
        """Stuff+ on the chosen two inputs. Each grid is kept with the session's files, so going back to one is instant."""
        if not row:
            return {}, "", {"display": "none"}
        if x == y or x not in row["axes"] or y not in row["axes"]:
            raise PreventUpdate                      # cb_usc_why_axes is about to correct the axes
        up = game_report.get_upload(row["key"])
        if up is None:
            raise PreventUpdate
        memo, pitcher, tag = up.setdefault("why", {}), row["pitcher"], row["tag"]
        try:
            cached = ("grid", pitcher, tag, x, y)
            if cached not in memo:
                memo[cached] = usc.why_grid(up["df"].filter(pl.col("Pitcher") == pitcher), memo.get(pitcher), tag, x, y)
            g = memo[cached]
        except Exception as e:                       # like the panel above: never break the page
            return {}, html.Span(f"Couldn't draw this grid ({type(e).__name__}: {e}).", style={"color": "#a30000"}), {}
        if g is None:
            return {}, "", {"display": "none"}
        note = ["The row's real pitches, each moved by the same amount and regraded, with everything else held. "
                + ("A primary fastball's move would also change the pitcher's other grades; this shows only its own. " if g["role"] == "FB_1"
                   else "Moving velo, IVB or HB carries that delta with it, because the fastball stays put, unless that delta is the other axis. ")
                + "The box is where the row is now and the dashed line is 100. Each axis gives most of its cells to the side that raises the grade, "
                  "and the far corners are shapes few D1 pitchers throw."]
        if g["used"] < g["n"]:
            note.append(f" Graded from {g['used']} of the row's {g['n']} pitches, spread across their grades: usually within about a point of grading them all.")
        return _why_grid_fig(g), note, {}

    @m.app.callback(Output("usc-reports-note", "children"), Output("usc-reports-dl", "data"),
                    Input("usc-reports-btn", "n_clicks"), State("usc-key", "data"), prevent_initial_call=True)
    def cb_usc_reports(_n, key):
        """Every pitcher's report (name, line, Stuff + charts, arsenal; no pitch by pitch), one 17 x 11 in
        landscape page each, in the dropdown's order, as one PDF. In a browser (the site, or browser mode) it
        downloads. The desktop window (pywebview) ignores downloads, so there the app saves it to ~/Downloads
        on this Mac and opens it in Preview."""
        up = game_report.get_upload(key)
        if up is None:
            return html.Span("Load the files first.", style={"color": "#a30000"}), no_update
        df = up["df"]
        pitchers = [o["value"] for o in _pitcher_options(df)]
        views = [_report_view(m, p, df.filter(pl.col("Pitcher") == p), export=True)[0] for p in pitchers]
        try:
            pdf = usc.render(_export_page(m, views))
        except Exception as e:                       # no Chrome on this machine, or it failed
            return html.Span(f"Couldn't make the PDF ({e}).", style={"color": "#a30000"}), no_update
        day = (df["Date"].drop_nulls().mode().to_list() or [""])[0][:10]
        name = f"USC_pitcher_reports_{day}".rstrip("_")
        if not DESKTOP:
            return (html.Span(f"Downloaded {len(pitchers)} pitcher reports ({name}.pdf).", style={"color": "#2e6b3a"}),
                    dcc.send_bytes(pdf, f"{name}.pdf"))
        path = _free_path(Path.home() / "Downloads", name, ".pdf")
        path.write_bytes(pdf)
        subprocess.run(["open", str(path)], check=False)          # shows it in Preview
        return (html.Span(f"Saved {len(pitchers)} pitcher reports to ~/Downloads/{path.name} (opened in Preview).",
                          style={"color": "#2e6b3a"}), no_update)

    @m.app.callback(Output("usc-pbp-log", "children"), Output("usc-pbp-sel", "data", allow_duplicate=True),
                    Input("usc-pbp-store", "data"), Input("usc-pbp-metric", "value"), State("usc-pbp-sel", "data"),
                    prevent_initial_call=True)
    def cb_usc_pbp_log(store, metric, sel):
        """A new pitcher (or another big number) redraws the log; the selected pitch stays if it's still there."""
        if not store or not store["p"]["uid"]:
            return [], None
        uids = store["p"]["uid"]
        if sel not in uids:
            sel = uids[store["default"] or 0]
        return _pbp_log(store, metric or "velo", sel), sel

    @m.app.callback(Output("usc-pbp-sel", "data", allow_duplicate=True),
                    Output({"type": "usc-pbp-row", "i": ALL}, "className"),
                    Input({"type": "usc-pbp-row", "i": ALL}, "n_clicks"), State("usc-pbp-store", "data"),
                    prevent_initial_call=True)
    def cb_usc_pbp_click(_clicks, store):
        """A click selects that pitch and moves the highlight; rows just drawn (n_clicks 0) don't count."""
        t = ctx.triggered_id
        if not isinstance(t, dict) or not store or not any(c.get("value") for c in ctx.triggered):
            raise PreventUpdate
        i = t["i"]
        return store["p"]["uid"][i], ["pbp-row sel" if o["id"]["i"] == i else "pbp-row" for o in ctx.outputs_list[1]]

    @m.app.callback(Output("usc-pbp-release", "figure"), Output("usc-pbp-move", "figure"),
                    Output("usc-pbp-loc", "figure"), Output("usc-pbp-tilt", "figure"),
                    Output("usc-pbp-rel-tiles", "children"), Output("usc-pbp-move-tiles", "children"),
                    Output("usc-pbp-spin-tiles", "children"), Output("usc-pbp-tag", "children"),
                    Output("usc-pbp-grades", "children"), Output("usc-pbp-outcome", "children"),
                    Input("usc-pbp-sel", "data"), State("usc-pbp-store", "data"))
    def cb_usc_pbp_card(sel, store):
        """The selected pitch's card (app.cb_pbp_card without the retag controls). A pitch only the chart has
        says so under its tag."""
        if not store or sel not in store["p"]["uid"]:
            raise PreventUpdate
        i = store["p"]["uid"].index(sel)
        c = pitch_by_pitch.card(store, i)
        mode = pitch_by_pitch.SAME_TYPE
        chart_only = sel.startswith(CHART_UID)
        tag = [html.Div([html.Span(c["abbrev"], style={"fontSize": "26px", "fontWeight": 800, "marginRight": "8px"}),
                         html.Span(c["pt"], style={"fontSize": "14px"})]),
               html.Div(f"#{c['n']}" + (f" · Stuff+ group {c['group']}" if c["group"] else ""),
                        style={"fontSize": "12px", "color": "#666"}),
               html.Div("Charted only: TrackMan didn't track this pitch", style={"fontSize": "11px", "color": m.MAROON})
               if chart_only else None,
               html.Div("Retagged (saved for everyone)", style={"fontSize": "11px", "color": m.MAROON}) if c["retagged"] else None]
        return (pitch_by_pitch.release_fig(store, i, mode), pitch_by_pitch.movement_fig(store, i, mode),
                pitch_by_pitch.location_fig(store, i, mode), pitch_by_pitch.tilt_fig(store, i, mode),
                m._pbp_tiles([("Release height", c["rel_height"], "ft"), ("Release side", c["rel_side"], "ft"),
                              ("Extension", c["extension"], "ft")]
                             + ([("Arm angle", c["arm"], "est.")] if c["arm"] else [])),
                m._pbp_tiles([("Vert Break", c["ivb"], "in"), ("Horz Break", c["hb"], "in")]),
                m._pbp_tiles([("Pitch speed", c["velo"], "mph"), ("Total spin", c["spin"], "rpm"), ("Tilt", c["tilt"], "")]),
                tag, m._pbp_grades(c), m._pbp_outcome(c))

    @m.app.callback(Output("usc-dl", "data"), Input("usc-dl-btn", "n_clicks"), State("usc-key", "data"),
                    prevent_initial_call=True)
    def cb_usc_download(_n, key):
        up = game_report.get_upload(key)
        if up is None:
            raise PreventUpdate
        return dcc.send_string(up["df"].write_csv(), "usc_paired.csv")

    return [usc_panel]
