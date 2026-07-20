"""Dash UI for delispice_app (pitcher/batter reports) — a single Python program, no external API.

A role tab (Pitchers / Batters) switches what the shared cascading picker looks up and which report
renders below:
  * Pitchers — summary, Batter + Count split checkboxes that drive the arsenal table AND the location
    heatmap, plus movement / heatmap / velocity charts.
  * Batters — batting-line summary + a spray chart (a starting scaffold; more tables/charts to come).

Run standalone (opens in a browser) with ``python -m delispice_app.app``; ``launch.py`` wraps this same
server in a native pop-up window via pywebview.
"""
from __future__ import annotations

import io
import json
import os
import re
from datetime import date, datetime

import numpy as np
import plotly.graph_objects as go
from dash import ALL as ALLPM, Dash, Input, Output, Patch, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from . import data, report, scouting

ALL = data.ALL
RETAG_TYPES = ["Fastball", "Sinker", "FourSeamFastBall", "TwoSeamFastBall", "Cutter", "Slider",
               "Sweeper", "Curveball", "ChangeUp", "Splitter", "Knuckleball", "Undefined", "Other"]
RETAG_TYPE_OPTS = [{"label": t, "value": t} for t in RETAG_TYPES]

# ── Styling (maroon tables, matching the notebook) ───────────────────────────────────────────────
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
MAROON = "#8a1520"
TH = {"background": MAROON, "color": "#fff", "padding": "4px 10px", "border": "1px solid #fff",
      "whiteSpace": "nowrap"}
TD = {"padding": "3px 10px", "border": "1px solid #e2e2e2", "textAlign": "center"}
TABLE = {"borderCollapse": "collapse", "fontFamily": FONT, "fontSize": "13px"}
LABEL = {"fontWeight": 600, "fontSize": "13px", "marginRight": "8px", "fontFamily": FONT}
DD = {"width": "260px", "fontSize": "13px"}


def _title(text):
    return html.Div(text, style={"fontWeight": 600, "margin": "12px 0 3px", "fontFamily": FONT})


def html_table(df, title=None, empty_msg="No data."):
    """A polars DataFrame -> styled html.Table (maroon header), or an italic empty message."""
    if df is None or df.height == 0:
        body = html.I(empty_msg)
    else:
        head = html.Tr([html.Th(c, style=TH) for c in df.columns])
        rows = [html.Tr([html.Td("" if v is None else v, style=TD) for v in r]) for r in df.iter_rows()]
        body = html.Table([head, *rows], style=TABLE)
    return html.Div([_title(title), body]) if title else body


def _scroll(child):
    """Wrap a (wide) table so it scrolls horizontally instead of clipping."""
    return html.Div(child, style={"overflowX": "auto"})


def _uniq(col):
    """Sorted unique non-null values of a polars Series (safe when some rows have a null date/team)."""
    return sorted(v for v in col.unique().to_list() if v is not None)


def _player_header(name, sub_bits, bio=None):
    children = [
        html.Div(name, style={"fontSize": "23px", "fontWeight": 700, "margin": "4px 0"}),
        html.Div(" · ".join(sub_bits), style={"color": "#555", "fontSize": "13px"}),
    ]
    if bio is not None:
        children.append(bio)
    return html.Div(children)


def _bio_line(bio, report_years):
    """The height / age / birthday / draft-eligibility line under a pitcher's name. Age + eligibility
    use the report's season (single year -> that year's draft day) or today (multiple years)."""
    if not bio or (bio["height_in"] is None and bio["birthdate"] is None):
        return html.Div("Height / age unknown — add it with ✎ Edit height / birthday below.",
                        style={"color": "#999", "fontSize": "12px", "fontStyle": "italic", "marginTop": "2px"})
    parts = []
    if bio["height"]:
        parts.append(bio["height"])
    bd = bio["birthdate"]
    if bd:
        if len(report_years) == 1:
            draft_yr, age_ref = report_years[0], report.draft_day(report_years[0])
        else:
            draft_yr, age_ref = date.today().year, date.today()
        parts += [f"Age {report.age_on(bd, age_ref):.1f}", f"Born {bd.strftime('%m/%d/%Y')}",
                  f"Draft eligible: {report.draft_eligible(bd, report.draft_day(draft_yr))}"]
    return html.Div(" · ".join(parts),
                    style={"color": "#333", "fontSize": "13px", "fontWeight": 600, "marginTop": "2px"})


def _bio_edit_form(prefix):
    """Manual bio override — for players Baseball Reference couldn't match. Saving appends a
    Status='manual' row to heights.csv (wins on read; the scraper won't re-touch it). ``prefix``
    ('bio' / 'bbio') namespaces the ids so the pitcher and batter forms don't collide."""
    return html.Details(open=False, style={"margin": "4px 0 2px", "fontFamily": FONT}, children=[
        html.Summary("✎ Edit height / birthday",
                     style={"cursor": "pointer", "fontSize": "12px", "color": "#8a1520"}),
        html.Div([
            html.Span("Height", style=LABEL),
            dcc.Input(id=f"{prefix}-height-input", type="text", placeholder="6-2 or 74",
                      style={"width": "90px", "fontSize": "12px"}),
            html.Span("Birthday", style=LABEL),
            dcc.Input(id=f"{prefix}-bday-input", type="text", placeholder="MM/DD/YYYY",
                      style={"width": "110px", "fontSize": "12px"}),
            html.Button("Save", id=f"{prefix}-save-btn", n_clicks=0, style=_BTN),
            html.Span(id=f"{prefix}-save-status",
                      style={"marginLeft": "8px", "color": "#666", "fontSize": "12px"}),
        ], style={"display": "flex", "alignItems": "center", "gap": "6px", "flexWrap": "wrap",
                  "marginTop": "4px"}),
    ])


def _parse_height_input(s):
    """'6-2' / \"6'2\" / inches '74' -> (inches:int|None, error:str|None). Blank -> (None, None)."""
    if not s or not str(s).strip():
        return None, None
    t = str(s).strip().replace("'", "-").replace("’", "-").replace('"', "")
    m = re.fullmatch(r"(\d)\s*-\s*(\d{1,2})", t)
    if m:
        ft, inch = int(m[1]), int(m[2])
        return (None, "Inches must be 0–11 (e.g. 6-2).") if inch > 11 else (ft * 12 + inch, None)
    if t.isdigit():
        n = int(t)
        return (n, None) if 40 <= n <= 90 else (None, "Height in inches should be ~40–90, or use 6-2.")
    return None, "Height format: 6-2 or inches like 74."


def _parse_bday_input(s):
    """'MM/DD/YYYY' (also ISO / MM-DD-YYYY) -> (date|None, error:str|None). Blank -> (None, None)."""
    if not s or not str(s).strip():
        return None, None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date(), None
        except ValueError:
            pass
    return None, "Birthday format: MM/DD/YYYY."


def _arsenal_block(dff, batter, count):
    label = f"{'/'.join(batter) if batter else 'All batters'} · {', '.join(count) if count else 'all counts'}"
    if dff.height == 0:
        return html.Div([_title(f"Arsenal · {label}"), html.I("No pitches match this split.")])
    return _scroll(html_table(report.build_arsenal(dff), title=f"Arsenal · {label} · {dff.height:,} pitches"))


_VS_LABEL = {"All": "All pitchers", "Right": "vs RHP", "Left": "vs LHP"}


# Per-column widths sized so every header fits on one line.
_BT_W = {"Pitch": 160, "Pitches Seen": 92, "Pitch Seen %": 92, "Swing %": 68, "Contact %": 80,
         "Good Decision %": 112, "Whiff %": 64, "I-Zone Swing %": 112, "I-Zone Whiff %": 112,
         "Chase %": 66, "Hard Hit %": 84, "Avg EV": 68, "xRV/BBE": 76}


def _bt_grid(cols):
    return {"display": "grid", "gridTemplateColumns": " ".join(f"{_BT_W.get(c, 90)}px" for c in cols)}


def _bt_cell(val, i, kind):
    left = i == 0
    st = {"padding": "4px 8px", "fontSize": "12px", "fontFamily": FONT,
          "textAlign": "left" if left else "center", "whiteSpace": "nowrap",
          "overflow": "hidden", "textOverflow": "ellipsis"}
    if kind == "header":
        st |= {"background": MAROON, "color": "#fff", "fontWeight": 600, "borderRight": "1px solid #fff",
               "display": "flex", "alignItems": "center",
               "justifyContent": "flex-start" if left else "center"}
    else:
        st |= {"borderBottom": "1px solid #eee"}
        if kind in ("family", "all"):
            st |= {"fontWeight": 700, "background": "#faf3f4"}
            if kind == "all":                       # combined total row — stand out a touch
                st |= {"background": "#efdde0", "borderBottom": "2px solid #d9b3b8"}
        elif left:
            st |= {"paddingLeft": "30px", "color": "#333"}
    return html.Div("" if val is None else val, style=st)


def _batter_pitchtable_block(rows, vs):
    """Collapsible pitch-type table grouped into families (native <details>, no callback needed)."""
    families = report.batter_pitch_families(rows, None if vs == "All" else vs)
    label = _VS_LABEL.get(vs, vs)
    if not families:
        return html.Div([_title(f"By pitch type · {label}"), html.I("No pitches match this split.")])
    cols = report.BATTER_TABLE_COLS
    grid = _bt_grid(cols)
    total_w = sum(_BT_W.get(c, 90) for c in cols)
    blocks = [html.Div([_bt_cell(c, i, "header") for i, c in enumerate(cols)], style=grid)]
    for fam in families:
        is_all = fam["family"] == "All"             # leading combined row: not collapsible, no arrow
        kind = "all" if is_all else "family"
        name_bits = ([] if is_all else
                     [html.Span("▶", className="fam-arrow",
                                style={"display": "inline-block", "marginRight": "7px", "fontSize": "10px", "color": MAROON})])
        name_bits.append(html.Span(fam["agg"]["Pitch"]))
        first = html.Div(name_bits,
                         style={"padding": "4px 8px", "fontSize": "12px", "fontFamily": FONT, "fontWeight": 700,
                                "background": "#efdde0" if is_all else "#faf3f4", "whiteSpace": "nowrap",
                                "borderBottom": "2px solid #d9b3b8" if is_all else "1px solid #eee"})
        row_cells = [first] + [_bt_cell(fam["agg"][c], i, kind) for i, c in enumerate(cols) if i > 0]
        row = html.Div(row_cells, style=grid)
        if is_all:
            blocks.append(row)                      # plain total row, not a <details>
        else:
            subs = [html.Div([_bt_cell(sub[c], i, "sub") for i, c in enumerate(cols)], style=grid) for sub in fam["subs"]]
            blocks.append(html.Details([html.Summary(row), *subs], className="fam"))
    return html.Div([_title(f"By pitch type · {label}  (click a family to expand)"),
                     _scroll(html.Div(blocks, style={"minWidth": f"{total_w}px", "border": "1px solid #e2e2e2"}))])


# ── Savant-style percentile sliders (movement chart ⟷ location heatmap) ─────────────────────────
# (label, pool column, higher-is-better, format)  — displayed top to bottom in this order.
_PCT_METRICS = [
    ("Fastball Velo", "fb_velo", True,  lambda v: f"{v:.1f}"),
    ("Avg Exit Velo", "avg_ev",  False, lambda v: f"{v:.1f}"),
    ("Chase %",       "chase",   True,  lambda v: f"{100 * v:.1f}"),
    ("Whiff %",       "whiff",   True,  lambda v: f"{100 * v:.1f}"),
    ("K %",           "k_pct",   True,  lambda v: f"{100 * v:.1f}"),
    ("BB %",          "bb_pct",  False, lambda v: f"{100 * v:.1f}"),
    ("Barrel %",      "barrel",  False, lambda v: f"{100 * v:.1f}"),
    ("Hard Hit %",    "hardhit", False, lambda v: f"{100 * v:.1f}"),
    ("GB %",          "gb",      True,  lambda v: f"{100 * v:.1f}"),
    ("Extension",     "ext",     True,  lambda v: f"{v:.1f}"),
]


def _pct_color(p):
    """Savant-ish diverging colour: blue (poor) -> grey (50th) -> red (elite)."""
    lo, mid, hi = (52, 96, 173), (185, 185, 185), (214, 39, 40)
    a, b, t = (mid, hi, (p - 50) / 50) if p >= 50 else (lo, mid, p / 50)
    return "rgb({},{},{})".format(*(round(a[i] + (b[i] - a[i]) * t) for i in range(3)))


def _pct_row(label, pctl, value_txt):
    """One slider row: label · track with a numbered dot at the percentile · raw value."""
    lab = html.Div(label, style={"width": "92px", "fontSize": "11.5px", "fontWeight": 600,
                                 "whiteSpace": "nowrap"})
    val = html.Div(value_txt, style={"width": "46px", "fontSize": "11.5px", "textAlign": "right",
                                     "color": "#444"})
    if pctl is None:
        track = html.Div(html.Div(style={"height": "4px", "background": "#eee", "borderRadius": "2px",
                                         "position": "absolute", "left": 0, "right": 0, "top": "9px"}),
                         style={"flex": "1", "position": "relative", "height": "22px"})
        return html.Div([lab, track, val], style={"display": "flex", "alignItems": "center",
                                                  "gap": "8px", "margin": "7px 0"})
    c = _pct_color(pctl)
    dot = html.Div(f"{pctl:.0f}", style={
        "position": "absolute", "top": "0", "left": f"calc({pctl}% - 11px)",
        "width": "22px", "height": "22px", "borderRadius": "11px", "background": c,
        "color": "#fff", "fontSize": "10.5px", "fontWeight": 700, "display": "flex",
        "alignItems": "center", "justifyContent": "center", "boxShadow": "0 0 0 2px #fff"})
    track = html.Div([
        html.Div(style={"height": "4px", "background": "#e3e3e3", "borderRadius": "2px",
                        "position": "absolute", "left": 0, "right": 0, "top": "9px"}),
        html.Div(style={"height": "4px", "background": c, "opacity": 0.55, "borderRadius": "2px",
                        "position": "absolute", "left": 0, "width": f"{pctl}%", "top": "9px"}),
        dot,
    ], style={"flex": "1", "position": "relative", "height": "22px"})
    return html.Div([lab, track, val], style={"display": "flex", "alignItems": "center",
                                              "gap": "8px", "margin": "7px 0"})


def _pct_panel(pitcher, level, years_sel):
    """Percentile rankings vs qualified (>=10 IP) pitchers at the selected Level (+Years)."""
    years_key = tuple(sorted(years_sel)) if years_sel else ()
    pool = data.percentile_pool(level or ALL, years_key)
    if pool.height == 0:
        return ""
    me = pool.filter(pool["Pitcher"] == pitcher)
    if me.height == 0:
        return ""
    me = me.row(0, named=True)
    qual = pool.filter(pool["qualified"])
    rows = []
    for label, col, higher, fmt in _PCT_METRICS:
        v = me[col]
        ref = qual[col].drop_nulls().to_numpy()
        if v is None or len(ref) < 10:
            rows.append(_pct_row(label, None, "—"))
            continue
        p = 100 * (np.sum(ref < v) + 0.5 * np.sum(ref == v)) / len(ref)
        if not higher:
            p = 100 - p                                  # goodness-oriented: 100 = elite, Savant-style
        rows.append(_pct_row(label, float(np.clip(p, 1, 99.4)), fmt(v)))
    lvl = level if level and level != ALL else "all levels"
    yrs = f" · {', '.join(sorted(years_key))}" if years_key else ""
    ip = f"{me['outs'] // 3}.{me['outs'] % 3}"
    note = "" if me["qualified"] else f"  (this pitcher: {ip} IP — below the bar, shown anyway)"
    return html.Div([
        html.Div("Percentile Rankings", style={"fontWeight": 700, "fontSize": "13px",
                                               "textAlign": "center", "margin": "2px 0 1px"}),
        html.Div(f"vs {qual.height:,} qualified {lvl} pitchers (≥{data.QUAL_OUTS // 3} IP){yrs}{note}",
                 style={"fontSize": "10.5px", "color": "#777", "textAlign": "center",
                        "marginBottom": "6px"}),
        *rows,
    ], style={"width": "370px", "padding": "26px 14px 8px", "fontFamily": FONT})


# ── AutoCluster helpers ──────────────────────────────────────────────────────────────────────────
def _cluster_palette(ent):
    """Label -> colour for the cluster view. Renamed clusters take their pitch's usual colour
    (keyed post-PITCH_NAMES, matching how the figures display labels); numbered clusters take the
    distinct cluster palette; unclustered pitches are grey."""
    pal = {data.UNCLUSTERED: "#8c8c8c"}
    for i in range(ent["k"]):
        lbl = data.cluster_label(ent, i)
        key = report.PITCH_NAMES.get(lbl, lbl)
        pal[key] = report.PITCH_COLORS.get(key, report.CLUSTER_COLORS[i % len(report.CLUSTER_COLORS)])
    return pal


def _cluster_rename_area(ent):
    """One rename dropdown per cluster (TrackMan names), colour-chipped, with pitch counts."""
    counts = {}
    for c in ent["assign"].values():
        counts[c] = counts.get(c, 0) + 1
    pal = _cluster_palette(ent)
    rows = []
    for i in range(ent["k"]):
        lbl = data.cluster_label(ent, i)
        chip = html.Span(style={"display": "inline-block", "width": "10px", "height": "10px",
                                "borderRadius": "5px", "marginRight": "5px",
                                "background": pal.get(report.PITCH_NAMES.get(lbl, lbl), "#8c8c8c")})
        rows.append(html.Div([chip,
            html.Span(f"Cluster {i} ({counts.get(i, 0):,} pitches) →",
                      style={"fontSize": "12px", "marginRight": "6px"}),
            dcc.Dropdown(id={"type": "clu-name", "index": i}, options=RETAG_TYPE_OPTS,
                         value=ent["names"].get(str(i)), placeholder=f"name Cluster {i}…",
                         clearable=True, style=_RDD)],
            style={"display": "flex", "alignItems": "center", "gap": "4px", "marginBottom": "4px"}))
    return html.Div(rows, style={"margin": "2px 0 6px 14px"})


def _cluster_options(ent):
    """Assignment-dropdown options: each cluster id with its (renamed) label."""
    return [{"label": data.cluster_label(ent, i), "value": i} for i in range(ent["k"])]


def _review_area(ent, cur, remaining):
    """One low-confidence pitch at a time: its metrics + assign/skip controls (avoids a wall of rows)."""
    def metric(lbl, val, unit=""):
        return html.Div([html.Div(lbl, style={"fontSize": "10px", "color": "#888", "fontWeight": 600}),
                         html.Div(f"{val:.1f}{unit}" if val is not None else "—",
                                  style={"fontSize": "15px", "fontWeight": 700})],
                        style={"textAlign": "center", "minWidth": "70px"})
    guess = data.cluster_label(ent, cur["cluster"]) if cur["cluster"] is not None else data.UNCLUSTERED
    return html.Div([
        html.Div([html.Span("Unsure pitches", style={"fontWeight": 700, "fontSize": "12px"}),
                  html.Span(f"  {remaining} below {int(data.UNSURE_THRESHOLD * 100)}% confidence — "
                            "place them one at a time.",
                            style={"fontSize": "11px", "color": "#777"})]),
        html.Div([metric("Velo", cur["RelSpeed"], " mph"), metric("Spin", cur["SpinRate"]),
                  metric("Vert Break", cur["InducedVertBreak"], " in"),
                  metric("Horz Break", cur["HorzBreak"], " in"),
                  html.Div([html.Div("Model's guess", style={"fontSize": "10px", "color": "#888", "fontWeight": 600}),
                            html.Div(f"{guess} · {cur['conf'] * 100:.0f}%",
                                     style={"fontSize": "13px", "fontWeight": 700})],
                           style={"textAlign": "center", "minWidth": "130px"})],
                 style={"display": "flex", "gap": "12px", "alignItems": "center", "margin": "6px 0",
                        "padding": "6px 10px", "background": "#fff", "border": "1px solid #e2c9cc"}),
        html.Div([html.Span("Assign to", style={"fontSize": "12px"}),
                  dcc.Dropdown(id="clu-assign-to", options=_cluster_options(ent), value=cur["cluster"],
                               clearable=False, style=_RDD),
                  html.Button("Assign", id="clu-assign-apply", n_clicks=0, style=_BTN),
                  html.Button("Skip", id="clu-assign-skip", n_clicks=0, style=_BTN)],
                 style=_ROW),
    ], style={"margin": "2px 0 6px 14px"})


def _safe_name(pitcher):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", pitcher).strip("_")


def _heat_patch(dff):
    """In-place heatmap update (preserves the on-chart stat dropdown selection), mirroring batch_update."""
    surfaces, n = report.heat_surface_data(dff)
    patch = Patch()
    for i, (x, y, z) in enumerate(surfaces):
        patch["data"][i]["x"] = x
        patch["data"][i]["y"] = y
        patch["data"][i]["z"] = z
    patch["layout"]["title"]["text"] = f"Location · {n:,} pitches"
    return patch


def _graph(gid, **kw):
    return dcc.Graph(id=gid, config={"displaylogo": False}, **kw)


# ── App + layout ─────────────────────────────────────────────────────────────────────────────────
app = Dash(__name__, title="delispice_app", suppress_callback_exceptions=True)  # review card is built on demand
server = app.server
scouting.init_db()          # create the scouting SQLite store on first import (idempotent)

_default_level = "D1" if "D1" in data.levels() else ALL


def _checklist(cid, options, inline=True):
    return dcc.Checklist(id=cid, options=[{"label": o, "value": o} for o in options], value=[],
                         inline=inline, inputStyle={"marginRight": "4px", "marginLeft": "10px"},
                         style={"fontFamily": FONT, "fontSize": "13px", "display": "inline-block"})


def _dropdown(did, options, value, width=None):
    style = {**DD, "width": width} if width else DD
    return dcc.Dropdown(id=did, options=options, value=value, clearable=False, style=style)


role_tabs = dcc.Tabs(id="role-tabs", value="about", style={"width": "380px", "marginBottom": "10px"},
                     colors={"primary": MAROON, "background": "#faf7f7", "border": "#e2c9cc"},
                     children=[dcc.Tab(label="About", value="about"),
                               dcc.Tab(label="Pitchers", value="pitcher"),
                               dcc.Tab(label="Batters", value="batter"),
                               dcc.Tab(label="Shortlist", value="shortlist")])

selection = html.Div([
    role_tabs,
    html.Div(id="picker-controls", style={"display": "none"}, children=[
    html.Div([html.Span("Year(s):", style=LABEL), _checklist("year-check", data.years()),
              html.Span("  (none = All years)", style={"color": "#888", "fontSize": "12px"})],
             style={"marginBottom": "8px"}),
    html.Div([
        html.Div([html.Span("Level", style=LABEL),
                  _dropdown("level-dd", [{"label": ALL, "value": ALL}] + [{"label": l, "value": l} for l in data.levels()], _default_level)]),
        html.Div([html.Span("Conference", style=LABEL), _dropdown("conf-dd", [{"label": ALL, "value": ALL}], ALL, width="150px")]),
        html.Div([html.Span("Team", style=LABEL), _dropdown("team-dd", [{"label": ALL, "value": ALL}], ALL, width="320px")]),
    ], style={"display": "flex", "gap": "18px", "flexWrap": "wrap", "alignItems": "center"}),
    html.Div([html.Span("Pitcher", id="player-label", style=LABEL),
              dcc.Dropdown(id="player-dd", options=[], value=None, placeholder="Type a name to search…",
                           style={"width": "460px", "fontSize": "13px"})],
             style={"marginTop": "10px", "display": "flex", "alignItems": "center"}),
    html.Div([html.Button("⟳ Rebuild index", id="refresh-btn", n_clicks=0,
                          style={"fontSize": "12px", "cursor": "pointer"}),
              html.Span(id="pick-status", style={"marginLeft": "12px", "color": "#666", "fontSize": "12px"})],
             style={"marginTop": "10px"}),
    ]),
], style={"padding": "14px 16px", "background": "#faf7f7", "borderBottom": f"2px solid {MAROON}"})

splits = html.Div([
    html.Div([html.Span("Batter:", style=LABEL), _checklist("batter-check", ["LHH", "RHH"])]),
    html.Div([html.Span("Count:", style=LABEL), _checklist("count-check", report.SITS + report.SPECIFICS)],
             style={"marginTop": "6px"}),
], style={"margin": "8px 0"})

# Retag tool (lives on the pitcher report; edits apply to both reports via the data override layer)
_BTN = {"fontSize": "12px", "cursor": "pointer", "marginLeft": "6px", "padding": "2px 8px"}
_RDD = {"width": "165px", "fontSize": "12px", "display": "inline-block", "verticalAlign": "middle"}
_ROW = {"marginBottom": "8px", "display": "flex", "alignItems": "center", "gap": "6px", "flexWrap": "wrap"}
retag_panel = html.Details(open=False, style={"margin": "6px 0", "background": "#faf7f7",
                                              "border": "1px solid #e2c9cc", "padding": "4px 10px"}, children=[
    html.Summary("🏷  Retag pitches", style={"cursor": "pointer", "fontWeight": 600, "fontSize": "13px",
                                             "fontFamily": FONT, "padding": "4px 0"}),
    html.Div([
        html.Div([html.Span("① Box/lasso-select pitches on the movement chart, then assign to",
                            id="retag-lasso-label", style={"fontSize": "12px"}),
                  dcc.Dropdown(id="retag-lasso-to", options=RETAG_TYPE_OPTS, placeholder="pitch type…", style=_RDD),
                  html.Button("Assign selected", id="retag-lasso-apply", n_clicks=0, style=_BTN),
                  html.Span(id="retag-lasso-status", style={"marginLeft": "6px", "color": "#666", "fontSize": "12px"})],
                 style=_ROW),
        html.Div([html.Span(id="retag-info", style={"color": "#666", "fontSize": "12px", "marginRight": "10px"}),
                  html.Button("Reset this pitcher", id="retag-reset-pitcher", n_clicks=0, style=_BTN),
                  html.Button("Reset all", id="retag-reset-all", n_clicks=0, style=_BTN)],
                 style={"display": "flex", "alignItems": "center", "flexWrap": "wrap"}),
        html.Hr(style={"margin": "8px 0"}),
        html.Div([html.Span("② AutoCluster (GMM)", style={"fontSize": "12px", "fontWeight": 700}),
                  dcc.Checklist(id="cluster-release",
                                options=[{"label": " + RelHeight & Extension", "value": "r"}], value=[],
                                style={"display": "inline-block", "fontSize": "12px", "fontFamily": FONT}),
                  html.Button("Run AutoCluster", id="cluster-run", n_clicks=0, style=_BTN),
                  html.Button("Revert clustering", id="cluster-revert", n_clicks=0, style=_BTN),
                  html.Span(id="cluster-status", style={"marginLeft": "6px", "color": "#666", "fontSize": "12px"})],
                 style=_ROW),
        html.Div(id="cluster-rename-area"),
        html.Div(id="cluster-review-area"),
        html.Div([html.Button("⬇ CSV with clusters", id="cluster-dl-csv", n_clicks=0, style=_BTN),
                  html.Button("⬇ Parquet with clusters", id="cluster-dl-parquet", n_clicks=0, style=_BTN),
                  html.Span(id="cluster-dl-status", style={"marginLeft": "6px", "color": "#666", "fontSize": "12px"}),
                  dcc.Download(id="cluster-download")],
                 style=_ROW),
    ], style={"padding": "6px 2px 4px", "fontFamily": FONT}),
])

# Pitcher report
pitcher_report = html.Div(id="report", style={"display": "none"}, children=[
    html.Div(id="report-header"),
    _bio_edit_form("bio"),
    html.Div(id="summary"),
    html.Hr(style={"margin": "10px 0"}),
    html.Div("Splits — Batter AND Count drive the arsenal table AND the heatmap (heatmap stat: "
             "dropdown on the chart). Movement + velocity show all pitches.",
             style={"fontFamily": FONT, "fontSize": "13px", "fontWeight": 600}),
    splits,
    dcc.Loading(type="default", children=[
        html.Div(id="arsenal"),
        html.Hr(style={"margin": "10px 0"}),
        retag_panel,
        html.Div([_graph("move-graph"), html.Div(id="pct-panel"), _graph("heat-graph")],
                 style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),
        _graph("velo-graph"),
    ]),
])

# Batter report
batter_report = html.Div(id="batter-report", style={"display": "none"}, children=[
    html.Div(id="batter-header"),
    _bio_edit_form("bbio"),
    html.Div(id="batter-summary"),
    html.Hr(style={"margin": "10px 0"}),
    html.Div([html.Span("Pitch type", style=LABEL),
              dcc.RadioItems(id="batter-vs", value="All", inline=True,
                             options=[{"label": "All", "value": "All"}, {"label": "vs RHP", "value": "Right"},
                                      {"label": "vs LHP", "value": "Left"}],
                             inputStyle={"marginRight": "4px", "marginLeft": "12px"},
                             style={"display": "inline-block", "fontFamily": FONT, "fontSize": "13px"})],
             style={"margin": "6px 0"}),
    html.Div([html.Span("Family:", style=LABEL),
              dcc.RadioItems(id="batter-family", value="All", inline=True,
                             options=[{"label": f, "value": f} for f in report.BATTER_FAMILIES],
                             inputStyle={"marginRight": "4px", "marginLeft": "12px"},
                             style={"display": "inline-block", "fontFamily": FONT, "fontSize": "13px"})],
             style={"margin": "6px 0"}),
    dcc.Loading(type="default", children=[
        html.Div(id="batter-pitchtable"),
        html.Hr(style={"margin": "10px 0"}),
        html.Div([_graph("bheat-graph"), _graph("launch-graph"), _graph("spray-graph")],
                 style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),
    ]),
])

# One collapsible "Components" topic (flat accordion; body accepts any Dash content, incl. images)
def _topic(title, *body):
    return html.Details(open=False, style={"margin": "6px 0", "background": "#faf7f7",
                                           "border": "1px solid #e2c9cc", "padding": "2px 12px",
                                           "maxWidth": "720px"}, children=[
        html.Summary(title, style={"cursor": "pointer", "fontWeight": 600, "fontSize": "14px",
                                   "fontFamily": FONT, "padding": "6px 0"}),
        html.Div(list(body), style={"fontFamily": FONT, "fontSize": "14px", "lineHeight": "1.6",
                                    "padding": "2px 0 10px"}),
    ])


# About tab landing panel (default view; the tab hides the picker + reports)
about_panel = html.Div(id="about-panel", style={"padding": "8px 4px"}, children=[
    html.Div(
        [
            html.H2(
                "delispice",
                style={
                "fontFamily": FONT, 
                "color": MAROON, 
                "margin": 0,
                }
            ),
            # yea i honestly don't know it still doesn't look right but whatever
            html.Span(
                "—",
                style={
                    "fontFamily":FONT,
                    "fontSize":"18px",
                    "color":"#444",
                    "marginLeft":0,
                },
            ),
            html.Span(
                "College Baseball on Trackman",
                style={
                    "fontFamily":FONT,
                    "fontSize":"18px",
                    "color":"#444",
                    "marginLeft":0,
                },
            ),
        ],
        style={
            "display":"flex",
            "alignItems":"center",
            "gap":"12px",
        },
    ),
    html.P(["Pick the ", html.B("Pitchers"), " or ", html.B("Batters"),
            " tab above to begin. Filter by year, level, conference, and team, then choose a "
            "player to build their report."],
           style={"fontFamily": FONT, "fontSize": "14px", "maxWidth": "680px", "lineHeight": "1.5"}),

    html.P(
        ["Go to the ", html.B("Shortlist"), " tab to read scouting reports on various amateur players by our contributors!"],
        style={"fontFamily": FONT, "fontSize": "14px", "maxWidth": "680px", "lineHeight": "1.5"}
    ),

    html.H3(
        "Contributors",
        style={
            "fontFamily": FONT,
            "display":"inline-block",
            "borderBottom":f"2px solid {MAROON}",
            "paddingBottom":"2px",
            "color": MAROON,
            "marginTop": "24px",
            "marginBottom": "6px",
        },
    ),
    _topic("Matt Baek - Creator and Developer",
        html.Img(src=app.get_asset_url("Matt_Baek_Intro.png"),
        style={"maxWidth": "25%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),
        html.P(
            "Hello, my name is Matt Baek "
            "and I am currently in my last year at University of Southern California studying Applied and Computational Mathematics while " \
            "working as an analyst for USC baseball! " \
            "I've spent summers as an analyst with the Santa Barbara Foresters in the Cali Collegiate League and the " \
            "Brewster Whitecaps in the Cape Cod Baseball League " \
            "learning and growing a greater love for baseball.",
            style={"fontFamily": FONT, "fontSize": "16px", "maxWidth": "680px", "lineHeight": "1.5"}
        ),
        html.P(
            "This website is a personal project that I created during my time with the Brewster Whitecaps. " \
            "Wrestling and working with this project has taught me so much of everything from networking and database management to machine learning modeling. " \
            "This project will be ongoing as I search for graduate opportunities, hoping to bring what I learned from this project to help a school " \
            "or front office one day. ",
            style={"fontFamily": FONT, "fontSize": "16px", "maxWidth": "680px", "lineHeight": "1.5"}
        ),
        html.P(
            "Below is a link that directs you to my resume! Please feel free to reach out to me from the info on the resume!",
            style={"fontFamily": FONT, "fontSize": "16px", "maxWidth": "680px", "lineHeight": "1.5"}
        ),
        html.A("Matt Baek Resume",
        href=app.get_asset_url("Matt_Baek_Resume.pdf"),    
        style={"display": "inline-block", "marginTop": "10px", "fontSize": "13px",
                "fontFamily": FONT, "color": "#fff", "background": MAROON,
                "padding": "6px 12px", "borderRadius": "4px", "textDecoration": "none"}),

    ),
    
    # Moving Components
    html.H3(
        "Models and Machinery",
        style={
            "fontFamily": FONT,
            "display":"inline-block",
            "borderBottom":f"2px solid {MAROON}",
            "paddingBottom":"2px",
            "color": MAROON,
            "marginTop": "24px",
            "marginBottom": "6px",
        },
    ),
    html.P(["Below are articles that Matt Baek wrote about the learning process and the thoughts while designing the models" \
            " and the inner workings of this app"],
           style={"fontFamily": FONT, "fontSize": "14px", "maxWidth": "680px", "lineHeight": "1.5",
                  "marginTop": "0"}),
    _topic("Autocluster and Pitch Retagging",
           # How to use
           html.H4("How to use AutoCluster", style={"fontFamily": FONT, "color": MAROON, "fontSize": "14px", "margin": "12px 0 4px"}),
           html.P(
               "Hunter Dietz (Arkansas) Pre-AutoCluster Trackman tags:"
           ),
           html.Img(src=app.get_asset_url("PreCluster.png"),
           style={"maxWidth": "50%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),

           html.P(
               "To use the autocluster feature on a pitcher, click \"AutoCluster\" There is an option to use RelHeight and Extension " \
               "if the pitcher varies in those features, but otherwise would be noise so the default excludes them"
           ),
           html.Img(src=app.get_asset_url("Cluster_howitworks.png"),
           style={"maxWidth": "100%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),
           html.P(
               "Each cluster will be numbered and as you look at the movement data, you can group the clusters however you please" \
               " and according to your own definition of the pitch type. Statistics and charts will be auto updated. The clusters are " \
               "not perfect, as you see with Dietz's cutters misclustered as ChangeUps, but use the lasso tool to fix those small mistakes."
           ),
           html.Img(src=app.get_asset_url("PostCluster.png"),
           style={"maxWidth": "50%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),
           html.P("Below is a deeper dive into the model and the methodology."),

           # Exact Methodology, Limitations, and Future Developments
           html.H4("Introduction", 
                   style={"fontFamily": FONT, "color": MAROON, "fontSize": "14px", "margin": "12px 0 4px"}),
           html.P(
               "Pitch labels are the thorn of many analysts attempting to create models with college Trackman " \
               "data. While many D1 Trackman taggers do an excellent job of identifying and labeling pitches, " \
               "the practice itself is not standardized and there is no such “ground truth” for what should be " \
               "labeled, say a Slider vs. Sweeper or a Sinker vs. Two-Seam. Another limitation of identifying " \
               "pitches comes from the fact that we cannot know the pitcher’s intent for every pitch thrown nor " \
               "the exact name of the pitches that a pitcher throws."
            ),
            html.P(
                "The focus of this model was to ignore the technicalities of naming pitches and simply group " \
                "them together first. Simplifying the features by only taking velocity, spinrate, and movement " \
                "proved to be most effective, but some pitchers did vary in their release characteristics so an " \
                "option to include RelHeight and Extension was added which does improve the model for some pitchers. "
            ),
            html.H4("The Current Model - Gaussian Mixture Model",
                    style={"fontFamily": FONT, "color": MAROON, "fontSize": "14px", "margin": "12px 0 4px"}),
            html.P(
                "The model currently takes four features: Velocity, SpinRate, Vertical Movement, and Horizontal Movement. There are some " \
                "pitchers where release characteristics truly differ for each pitch, but generally those four features work perfectly fine. " \
                "All left-handed pitcher's horizontal movement was multiplied by -1 to make the model pitcher hand agnostic."
            ),

            html.P(
                "The model iterates through 1–6 pitch clusters using both the BIC and ICL to determine the optimal number of clusters, " \
                "with a lower ICL taking precedence when the criteria differ. A full covariance matrix was used for each Gaussian component, " \
                "allowing each pitch cluster to have its own size, elliptical shape, and orientation, as well as capturing correlations between" \
                " features, rather than constraining clusters to spherical or axis-aligned (diagonal) forms"
            ),

            html.H4("Previous Iterations and the Lessons Learned",
                    style={"fontFamily": FONT, "color": MAROON, "fontSize": "14px", "margin": "12px 0 4px"}),
            html.P(
                "Before I even decided on using GMM to cluster pitches, the first iterations of an autotagging model used supervised learning by presenting the model with a \"golden set\" " \
                "of MLB statcast data, but as I quickly learned the model kept failing in differentiating between close pitches like " \
                "sliders and sweepers or changeups and splitters because there is no \"exact\" definition of what a slider is. I tuned the " \
                "model to a point where it got 85% accuracy, but that didn't seem anywhere good enough for a dataset that might contain one million rows."
            ),
            html.Img(src=app.get_asset_url("Supervised_Iteration.png"),
                style={"maxWidth": "50%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),
            html.P(
                "But upon closer inspection, there were areas where the pitcher threw a slider but the model labeled the sliders " \
                "as both slider and cutters. So the new focus was to simply arrive at a coherent, singular cluster of a pitch rather than " \
                "trying to get the exact name of the pitch down, which is where we are today with the GMM clustering algorithm."
            ),
            html.P(
                "But, even developing the current clustering model wasn't without its own learning experiences. " \
                "Initially I selected features that I, myself, would look for when I tagged games on Trackman. These features included " \
                "Velocity, SpinRate, Movement, and Tilt (SpinAxis). Every feature normalized easily except for SpinAxis where 10 degrees " \
                "and 350 degrees are both curveballs. Even normalized, the model would have a tough time seeing that these two pitches are " \
                "the same. So, to get around this problem, I decided to take the sin and cosine of the spin axis to convert the degrees into " \
                "two numbers from 0-1, spin_x and spin_y. This was clearly a mistake. "
            ),
            html.Img(src=app.get_asset_url("diving_BIC.png"),
                style={"maxWidth": "15%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),
            html.P(
                "We select the optimal number of clusters, or pitches, by looking for k with the lowest BIC. " \
                "But BIC dives with each k and these college pitchers certainly don't have six pitch mixes. " \
            ),
            dcc.Markdown(r"""
                $BIC = -2 \ln(\hat{L}) + k \ln(n)$, where $-2 \ln(\hat{L})$ is the reward function and $k \ln(n)$ is the penalty function.
                """, mathjax=True
            ),
            dcc.Markdown(r"""
                The relationship of the SpinAxis features: $spin\_x^2 + spin\_y^2 = 1$ flattened the data into a perfect, 
                         infinitely thin ring. So while the BIC formula is normally a balance act with the reward and penalty function, in this 
                         case, the model was realizing that if it adds more components, k, it can cover more tiny, straight-line arcs along 
                         the ring that the SpinAxis features created. The penalty of overfitting, or adding more k components, was essentially
                         dwarfed by the Gaussian squishing itself into that ring with zero variance which shot the log-likelihood to infinity.
                         So even if we extend the loop to find k to k=10 or k=100, it would select 10 or 100 because BIC keeps diving with each increase in k.
                         Also, Trackman's SpinAxis is derived from movement so adding SpinAxis was a redundant feature anyways.
                         """, mathjax=True),
            html.P(
                "In addition, by this point, I had been only using BIC as a scoring metric. " \
                "However, with inconsistent pitchers, BIC began to overestimate an inconsistent pitcher's pitch mix thinking that the " \
                "variations represented different pitch types rather than inconsistent deliveries. "
            ),
            html.Img(src=app.get_asset_url("Gaeckle_BIC_example.png"),
                style={"maxWidth": "50%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),
            html.P(
                "Now Gabe Gaeckle is not an inconsisent pitcher by any means but even with a consistent pitcher, " \
                "Gaeckle's cluster 1 and cluster 3 are essentially the same pitches but clustered separately. "
            ),
            html.P(
                "To solve this quirk, I implemented ICL as another scoring metric that serves as a double check for BIC and take precedence over BIC. " \
                "ICL penalizes uncertainty and favors distinct, well-separated clusters"
            ),
            html.Img(src=app.get_asset_url("BIC_and_ICL.png"),
                style={"maxWidth": "50%", "marginTop": "8px", "border": "1px solid #e2c9cc"}),
            html.P(
                "BIC and ICL will normally be very close together as seen with the Mason Edwards example. " \
                "But in our new Gabe Gaeckles example, even as the BIC for k=5 is the lowest in BIC, because the ICL for k=4 is lower than the ICL for k=5, we go with k=4."
            ),

            html.H4("Limitations and Future Developments",
                    style={"fontFamily": FONT, "color": MAROON, "fontSize": "14px", "margin": "12px 0 4px"}),
            html.P(
                "The biggest limitation of this model is sample size. Even with Hunter Dietz's example run, because he threw so few " \
                "ChangeUps, the model mistakenly clustered many of his cutters into ChangeUps. This sample limitation is the reason why I have" \
                " not rolled out the autocluster for all pitchers because many of these Trackman tagged pitchers do not throw enough pitches " \
                "for the clustering model to be confident."
            ),
            html.P(
                "The second limitation is differentiating between pitch subgroups. The model does well differentaiting sliders and cutters " \
                "(for the most part), but the biggest one is differentiating FourSeam and Sinkers. Now most NCAA D1 pitchers only have one of the two " \
                "and often a pitch that looks like a sinker will just be a butchered FourSeam and vice versa. But for the true FourSeam, Sinker " \
                "pitchers, the model will clump all of those pitches as one cluster. The solution to this limitation would be to add in a sub-clustering " \
                "model, using the same idea as the RelHeight and Extension function. If the data looks like a pitcher truly has a FourSeam, Sinker mix, there will be a " \
                "button that runs a clustering model within the Fastball cluster using k=2 to split apart the two pitches. Who knows how it'll work, but that's something " \
                "that will be on the way for this model."
            ),
            html.P(
                "In terms of future developments, the goal is always to create a true autotagger," \
                " one without humans having to manually label the clusters. To do so, we will use the autocluster feature " \
                "to create a \"golden set\" of data with pitch types according to my definition of different pitch types and run a " \
                "supervised classifier that will hopefully be better than the one that I tried to use with MLB Statcast data."
            ),
        ),
    _topic("Contact Quality - Expected Runs on Contact",
           html.P("Placeholder — TrackMan ingestion, cleaning, and the parquet layout. Write here.")),
    _topic("Eye Metric - Expected Runs on Swing/Take Decisions",
           html.P("The model begins with a simple premise like all swing decision models do, "),
           html.H4("Limitations and Future Developments", style={"fontFamily": FONT, "color": MAROON, "fontSize": "14px", "margin": "12px 0 4px"}),
           html.P(
               "Heights are not taken into consideration and with the upcoming NCAA rule change to introduce ABS to D1 games, " \
               "adding heights would be the obvious next step. " \
               "So, rather than training the model on the raw pitch locations, we would normalize the strike-zone according to each batter's height."
           ),
    ),
           
    _topic("Hosting & infrastructure",
    html.P("Placeholder — VPS, Tailscale tunnel, Caddy, and the home box. Write here.")),
])

# ══ Shortlist / scouting ═══════════════════════════════════════════════════════════════════════════
_SL_INPUT = {"fontSize": "12px", "fontFamily": FONT, "padding": "3px 6px"}
_SL_FORM_STYLE = {"display": "none", "border": f"1px solid {MAROON}", "background": "#fff",
                  "padding": "12px 14px", "margin": "10px 0", "maxWidth": "780px"}
_SL_HDR = {"background": MAROON, "color": "#fff", "fontWeight": 600, "fontSize": "12px",
           "fontFamily": FONT, "padding": "5px 8px", "borderRight": "1px solid #fff"}
_SL_COLS = [("Player", 190), ("School", 150), ("Pos", 80), ("OVR", 70), ("Author", 200)]
_SL_GRID = {"display": "grid", "gridTemplateColumns": " ".join(f"{w}px" for _, w in _SL_COLS)}


def _sl_in(id_, ph, w="120px", type_="text"):
    return dcc.Input(id=id_, type=type_, placeholder=ph, style={**_SL_INPUT, "width": w})


def _sl_grade(id_, ph=""):
    return dcc.Input(id=id_, type="number", placeholder=ph, min=20, max=80, step=5,
                     style={**_SL_INPUT, "width": "56px"})


def _sl_field(label, control):
    return html.Div([html.Span(label, style={**LABEL, "width": "96px", "display": "inline-block"}), control],
                    style={"display": "flex", "alignItems": "center", "marginBottom": "6px"})


_hitter_grades = html.Div(id="sl-hitter-grades", style={"display": "none"}, children=[
    html.Div([html.Span(g, style={**LABEL, "width": "56px"}), _sl_grade(f"sl-g-{g}")],
             style={"display": "inline-flex", "alignItems": "center", "marginRight": "12px", "marginBottom": "4px"})
    for g in scouting.HITTER_GRADES])

_pitcher_grades = html.Div(id="sl-pitcher-grades", style={"display": "none"}, children=[
    html.Div([html.Span("Control", style={**LABEL, "width": "56px"}), _sl_grade("sl-g-Control")],
             style={"marginBottom": "6px"}),
    html.Div([html.Div([_sl_in(f"sl-p{i}-name", f"Pitch {i}", "120px"), _sl_grade(f"sl-p{i}-grade")],
                       style={"display": "inline-flex", "gap": "4px", "marginRight": "10px", "marginBottom": "4px"})
              for i in range(1, 7)]),
])

shortlist_form = html.Div(id="sl-form", style=_SL_FORM_STYLE, children=[
    _sl_field("Type", dcc.RadioItems(id="sl-form-role", value="pitcher", inline=True,
        options=[{"label": "Pitcher", "value": "pitcher"}, {"label": "Hitter", "value": "batter"}],
        style={"display": "inline-block", "fontSize": "13px", "fontFamily": FONT},
        inputStyle={"marginRight": "4px", "marginLeft": "10px"})),
    _sl_field("Find player", dcc.Dropdown(id="sl-form-pick", options=[], placeholder="type a name (optional)…",
                                          style={"width": "360px", "fontSize": "12px"})),
    _sl_field("Name", _sl_in("sl-form-name", "player name", "260px")),
    _sl_field("School", _sl_in("sl-form-school", "school", "220px")),
    _sl_field("Position", _sl_in("sl-form-pos", "e.g. RHP or 1B/DH", "160px")),
    _sl_field("Bats / Throws", _sl_in("sl-form-bt", "R/R", "90px")),
    _sl_field("OVR", _sl_grade("sl-form-ovr")),
    _sl_field("Date", dcc.Input(id="sl-form-date", type="date", value=date.today().isoformat(),
                                style={**_SL_INPUT, "width": "150px"})),
    _hitter_grades, _pitcher_grades,
    html.Div([html.Span("Report", style={**LABEL, "display": "block", "marginBottom": "3px"}),
              dcc.Textarea(id="sl-form-body", placeholder="scouting notes…",
                           style={"width": "100%", "height": "120px", **_SL_INPUT})],
             style={"marginTop": "8px"}),
    html.Div([html.Button("Save report", id="sl-save-btn", n_clicks=0, style=_BTN),
              html.Span(id="sl-save-status", style={"marginLeft": "10px", "fontSize": "12px", "color": "#666"})],
             style={"marginTop": "8px"}),
])

shortlist_panel = html.Div(id="shortlist-panel", style={"display": "none", "padding": "8px 4px"}, children=[
    html.H2("Shortlist", style={"fontFamily": FONT, "color": MAROON, "margin": "0 0 8px"}),
    html.Div([
        dcc.Input(id="sl-search", type="text", placeholder="Search name, school, or position…",
                  style={**_SL_INPUT, "width": "240px", "marginRight": "10px"}),
        dcc.RadioItems(id="sl-role", value="all", inline=True,
            options=[{"label": "All", "value": "all"}, {"label": "Pitchers", "value": "pitcher"},
                     {"label": "Batters", "value": "batter"}],
            style={"fontSize": "13px", "fontFamily": FONT, "marginRight": "6px"},
            inputStyle={"marginRight": "4px", "marginLeft": "10px"}),
        dcc.Dropdown(id="sl-author-filter", options=[], placeholder="Author…", clearable=True,
                     style={"width": "130px", "fontSize": "12px"}),
        html.Span("· click a column heading to sort", style={"fontSize": "12px", "color": "#999"}),
    ], style={"display": "flex", "alignItems": "center", "gap": "6px", "flexWrap": "wrap", "marginBottom": "8px"}),
    dcc.Store(id="sl-sort", data={"col": "OVR", "dir": "desc"}),
    dcc.Store(id="sl-auth", storage_type="session"),      # {initials, pw} — re-verified on every write
    html.Div([html.Span("Author", style=LABEL),
              _sl_in("sl-login-initials", "initials", "80px"),
              dcc.Input(id="sl-login-pw", type="password", placeholder="password",
                        style={**_SL_INPUT, "width": "130px"}),
              html.Button("Sign in", id="sl-login-btn", n_clicks=0, style=_BTN),
              html.Button("Sign out", id="sl-logout-btn", n_clicks=0, style=_BTN),
              html.Span(id="sl-login-status", style={"marginLeft": "8px", "fontSize": "12px", "color": "#666"})],
             style={"display": "flex", "alignItems": "center", "gap": "6px", "marginBottom": "6px"}),
    html.Div(id="sl-editor", style={"display": "none"}, children=[
        html.Button("+ Add report", id="sl-add-btn", n_clicks=0, style=_BTN),
        html.Span(id="sl-del-status", style={"marginLeft": "10px", "fontSize": "12px", "color": "#666"}),
    ]),
    shortlist_form,
    dcc.Store(id="sl-version", data=0),
    html.Div(id="sl-table", style={"marginTop": "10px"}),
])


_SL_CELL_ST = {"padding": "5px 8px", "fontSize": "12px", "fontFamily": FONT, "whiteSpace": "nowrap",
               "overflow": "hidden", "textOverflow": "ellipsis", "borderBottom": "1px solid #eee"}


def _sl_cell(text, i):
    return html.Div("—" if text in (None, "") else str(text),
                    style={**_SL_CELL_ST, "fontWeight": 700 if i == 0 else 400})


def _sl_version_block(v, role):
    gl = scouting.grades_line(role, v["grades"])
    return html.Div([
        html.Div(f"{scouting.fmt_date(v.get('report_date')) or scouting.stamp(v['created_at'])} · "
                 f"OVR {v['ovr'] if v['ovr'] is not None else '—'}",
                 style={"fontSize": "12px", "fontWeight": 600, "color": MAROON, "marginTop": "6px"}),
        html.Div(gl, style={"fontSize": "12px", "color": "#444"}) if gl else None,
        html.Div(v["body"] or "", style={"fontSize": "13px", "whiteSpace": "pre-wrap", "marginTop": "2px"}),
    ], style={"borderTop": "1px dashed #ddd", "paddingTop": "2px"})


_SL_SORT_KEY = {"Player": "player_name", "School": "school", "Pos": "position",
                "OVR": "ovr_max", "Author": "authors_label"}


def _sl_sort_rows(rows, sort):
    """Sort the shortlist by the clicked column; nulls always sink to the bottom."""
    key = _SL_SORT_KEY.get(sort.get("col"), "ovr_max")
    present = [r for r in rows if r.get(key) not in (None, "")]
    absent = [r for r in rows if r.get(key) in (None, "")]
    present.sort(key=lambda r: r[key] if key == "ovr_max" else str(r[key]).lower(),
                 reverse=(sort.get("dir") == "desc"))
    return present + absent


def _sl_header(sort):
    """Clickable column headers; the active column carries a ▲/▼ arrow."""
    cells = []
    for name, _w in _SL_COLS:
        arrow = (" ▲" if sort.get("dir") == "asc" else " ▼") if sort.get("col") == name else ""
        cells.append(html.Div(name + arrow, id={"type": "sl-hdr", "col": name}, n_clicks=0,
                              style={**_SL_HDR, "cursor": "pointer", "userSelect": "none"}))
    return html.Div(cells, style=_SL_GRID)


def _sl_report_section(entry, rep, auth):
    """One author's report inside a player's expansion: header (author · dates · buttons),
    grades, body, and that author's own version history."""
    versions = scouting.versions_for_thread(entry["player_key"], rep["thread"]) or [rep]
    latest, first = versions[0], versions[-1]
    who = rep.get("author_name") or rep.get("author") or "—"
    own = bool(auth) and rep.get("author_id") is not None and auth.get("id") == rep.get("author_id")
    created = scouting.fmt_date(first.get("report_date")) or scouting.stamp(first["created_at"])
    edited = scouting.fmt_date(latest.get("report_date")) or scouting.stamp(latest["created_at"])
    meta = f"Created {created}" + (f" · Edited {edited}" if len(versions) > 1 else "")
    bt = latest.get("bats_throws")
    head = [html.Span(who, style={"fontWeight": 700, "color": MAROON, "fontSize": "13px"}),
            html.Span(f"OVR {latest['ovr']}" if latest["ovr"] is not None else "",
                      style={"fontWeight": 600, "fontSize": "12px", "marginLeft": "10px"}),
            html.Span(f"B/T {bt}" if bt else "", style={"fontSize": "12px", "marginLeft": "10px"}),
            html.Span(meta, style={"fontSize": "11px", "color": "#888", "marginLeft": "10px"})]
    if own:
        head.append(html.Button("✎ Edit", n_clicks=0, style=_BTN,
                                id={"type": "sl-edit", "key": entry["player_key"], "thread": rep["thread"]}))
    if own or (bool(auth) and auth.get("is_admin")):
        head.append(dcc.ConfirmDialogProvider(
            html.Button("Remove", style=_BTN),
            id={"type": "sl-del", "key": entry["player_key"], "thread": rep["thread"]},
            message=f"Remove {who}'s report on {entry['player_name']}? History stays in the database."))
    gl = scouting.grades_line(rep["role"], latest["grades"])
    section = [
        html.Div(head, style={"display": "flex", "alignItems": "center", "flexWrap": "wrap", "gap": "2px"}),
        html.Div(gl, style={"fontSize": "13px", "fontWeight": 600, "margin": "4px 0"}) if gl else None,
        html.Div(latest["body"] or "", style={"fontSize": "13px", "whiteSpace": "pre-wrap"}),
    ]
    if len(versions) > 1:
        section.append(html.Details([
            html.Summary(f"Previous versions ({len(versions) - 1})",
                         style={"cursor": "pointer", "fontSize": "12px", "color": MAROON, "marginTop": "6px"}),
            *[_sl_version_block(v, rep["role"]) for v in versions[1:]]]))
    return html.Div(section, style={"padding": "6px 0 8px", "borderBottom": "1px dashed #e2c9cc"})


def _shortlist_table(entries, sort, auth):
    if not entries:
        return html.I("No reports match. Sign in and click “+ Add report” to start one.",
                      style={"fontSize": "13px", "color": "#777"})
    blocks = [_sl_header(sort)]
    for e in entries:
        player_cell = html.Div([
            html.Span("▶", className="fam-arrow",
                      style={"display": "inline-block", "marginRight": "6px", "fontSize": "10px", "color": MAROON}),
            html.Span(e["player_name"] or "")], style={**_SL_CELL_ST, "fontWeight": 700})
        summ = html.Div([player_cell, _sl_cell(e.get("school"), 1),
                         _sl_cell(e.get("position"), 2), _sl_cell(e.get("ovr_label"), 3),
                         _sl_cell(e.get("authors_label"), 4)], style=_SL_GRID)
        sections = [_sl_report_section(e, rep, auth) for rep in e["reports"]]
        blocks.append(html.Details([html.Summary(summ), html.Div(sections, style={"padding": "0 10px 4px"})],
                                   className="fam"))
    return html.Div(blocks, style={"border": "1px solid #e2e2e2", "maxWidth": "700px"})


@app.callback(Output("sl-table", "children"), Output("sl-author-filter", "options"),
              Input("role-tabs", "value"), Input("sl-version", "data"),
              Input("sl-role", "value"), Input("sl-sort", "data"), Input("sl-search", "value"),
              Input("sl-author-filter", "value"), Input("sl-auth", "data"))
def cb_sl_table(tab, _v, role, sort, query, author, auth):
    if tab != "shortlist":
        raise PreventUpdate
    sort = sort or {"col": "OVR", "dir": "desc"}
    entries = scouting.players(role if role in ("pitcher", "batter") else None, author or None)
    q = (query or "").strip().lower()
    if q:                                         # match across name / school / position / authors
        entries = [e for e in entries if q in " ".join(str(e.get(f) or "").lower()
                   for f in ("player_name", "school", "position", "authors_label"))]
    entries = _sl_sort_rows(entries, sort)
    a = auth if isinstance(auth, dict) and auth.get("id") else None
    return (_shortlist_table(entries, sort, a),
            [{"label": au["name"], "value": au["initials"]} for au in scouting.report_authors()])


@app.callback(Output("sl-sort", "data"), Input({"type": "sl-hdr", "col": ALLPM}, "n_clicks"),
              State("sl-sort", "data"), prevent_initial_call=True)
def cb_sl_sort(_clicks, cur):
    trig = ctx.triggered_id
    if not trig or not ctx.triggered or not ctx.triggered[0]["value"]:
        raise PreventUpdate                       # ignore the re-render that recreates the headers
    col, cur = trig["col"], (cur or {"col": "OVR", "dir": "desc"})
    if cur.get("col") == col:                     # same column -> flip direction
        return {"col": col, "dir": "asc" if cur.get("dir") == "desc" else "desc"}
    return {"col": col, "dir": "desc" if col == "OVR" else "asc"}   # OVR defaults high-first


@app.callback(Output("sl-form-pick", "options"), Input("sl-form-pick", "search_value"))
def cb_sl_pick_search(search):
    if not search or len(search) < 2:
        raise PreventUpdate
    return [{"label": f"{p['name']} · {p['school']} ({'P' if p['role'] == 'pitcher' else 'H'})",
             "value": json.dumps(p)} for p in data.search_players(search)]


@app.callback(Output("sl-form-name", "value"), Output("sl-form-school", "value"),
              Output("sl-form-role", "value"), Input("sl-form-pick", "value"), prevent_initial_call=True)
def cb_sl_autofill(val):
    if not val:
        raise PreventUpdate
    p = json.loads(val)
    return p["name"], p["school"], p["role"]


@app.callback(Output("sl-hitter-grades", "style"), Output("sl-pitcher-grades", "style"),
              Input("sl-form-role", "value"))
def cb_sl_grade_toggle(role):
    show, hide = {"display": "block", "margin": "6px 0"}, {"display": "none"}
    return (show, hide) if role == "batter" else (hide, show)


@app.callback(Output("sl-auth", "data"),
              Input("sl-login-btn", "n_clicks"), Input("sl-logout-btn", "n_clicks"),
              State("sl-login-initials", "value"), State("sl-login-pw", "value"),
              prevent_initial_call=True)
def cb_sl_login(_in, _out, initials, pw):
    if ctx.triggered_id == "sl-logout-btn":
        return None
    a = scouting.verify_author(initials, pw)
    if a is None:
        return {"error": ("No authors are registered yet — add one with "
                          "`python -m delispice_app.scouting add`." if not scouting.any_authors()
                          else "Wrong initials or password.")}
    return {"id": a["id"], "initials": a["initials"], "name": a["display_name"],
            "is_admin": a["is_admin"], "pw": pw}


@app.callback(Output("sl-editor", "style"), Output("sl-login-status", "children"),
              Output("sl-form", "style", allow_duplicate=True),
              Input("sl-auth", "data"), prevent_initial_call="initial_duplicate")
def cb_sl_authview(auth):
    hidden_form = {**_SL_FORM_STYLE, "display": "none"}
    if not auth:
        return {"display": "none"}, "", hidden_form
    if auth.get("error"):
        return {"display": "none"}, f"✗ {auth['error']}", hidden_form
    a = scouting.verify_author(auth.get("initials"), auth.get("pw"))   # re-check (deactivations bite)
    if a is None:
        return {"display": "none"}, "Session no longer valid — sign in again.", hidden_form
    label = f"Signed in as {a['display_name']} ({a['initials']})" + (" · admin" if a["is_admin"] else "")
    return {"display": "block"}, label, no_update


@app.callback(Output("sl-form", "style"), Input("sl-add-btn", "n_clicks"),
              State("sl-form", "style"), prevent_initial_call=True)
def cb_sl_toggle_form(_n, st):
    shown = (st or {}).get("display") == "block"
    return {**_SL_FORM_STYLE, "display": "none" if shown else "block"}


@app.callback(Output("sl-save-status", "children"), Output("sl-version", "data"),
              Output("sl-form", "style", allow_duplicate=True),
              Input("sl-save-btn", "n_clicks"),
              State("sl-auth", "data"), State("sl-form-role", "value"), State("sl-form-name", "value"),
              State("sl-form-school", "value"), State("sl-form-pos", "value"),
              State("sl-form-bt", "value"), State("sl-form-ovr", "value"),
              State("sl-form-body", "value"), State("sl-form-date", "value"),
              *[State(f"sl-g-{g}", "value") for g in scouting.HITTER_GRADES],
              State("sl-g-Control", "value"),
              *[State(f"sl-p{i}-name", "value") for i in range(1, 7)],
              *[State(f"sl-p{i}-grade", "value") for i in range(1, 7)],
              State("sl-version", "data"), prevent_initial_call=True)
def cb_sl_save(_n, auth, role, name, school, pos, bt, ovr, body, report_date, *rest):
    a = scouting.verify_author((auth or {}).get("initials"), (auth or {}).get("pw"))
    if a is None:                                 # the REAL gate — auth store alone is forgeable
        return "✗ Not signed in (or session expired) — not saved.", no_update, no_update
    if not name or not str(name).strip():
        return "✗ Player name is required.", no_update, no_update
    hitter_vals, control = rest[:5], rest[5]
    pnames, pgrades, version = rest[6:12], rest[12:18], rest[18]
    if role == "batter":
        grades = {g: v for g, v in zip(scouting.HITTER_GRADES, hitter_vals) if v is not None}
    else:
        grades = {"pitches": [{"name": pn.strip(), "grade": pg} for pn, pg in zip(pnames, pgrades)
                              if pn and pn.strip()]}
        if control is not None:
            grades["Control"] = control
    scouting.save_version(player_name=name.strip(), role=role, school=school, position=pos,
                          bats_throws=bt, ovr=ovr, author=a["initials"], author_id=a["id"],
                          body=body, grades=grades, report_date=report_date)
    return f"✓ Saved {name.strip()} · {a['display_name']}.", (version or 0) + 1, {**_SL_FORM_STYLE, "display": "none"}


# After any save/delete (sl-version bumps for this client), wipe the form so the next
# "+ Add report" starts from a blank slate instead of the player you just entered.
@app.callback(
    Output("sl-form-role", "value", allow_duplicate=True),
    Output("sl-form-name", "value", allow_duplicate=True),
    Output("sl-form-school", "value", allow_duplicate=True),
    Output("sl-form-pos", "value", allow_duplicate=True),
    Output("sl-form-bt", "value", allow_duplicate=True),
    Output("sl-form-ovr", "value", allow_duplicate=True),
    Output("sl-form-date", "value", allow_duplicate=True),
    Output("sl-form-body", "value", allow_duplicate=True),
    *[Output(f"sl-g-{g}", "value", allow_duplicate=True) for g in scouting.HITTER_GRADES],
    Output("sl-g-Control", "value", allow_duplicate=True),
    *[Output(f"sl-p{i}-name", "value", allow_duplicate=True) for i in range(1, 7)],
    *[Output(f"sl-p{i}-grade", "value", allow_duplicate=True) for i in range(1, 7)],
    Input("sl-version", "data"), prevent_initial_call=True)
def cb_sl_clear_form(_v):
    return ("pitcher", "", "", "", "", None, date.today().isoformat(), "",
            *[None] * len(scouting.HITTER_GRADES), None,
            *[""] * 6, *[None] * 6)


@app.callback(Output("sl-form-role", "value", allow_duplicate=True),
              Output("sl-form-name", "value", allow_duplicate=True),
              Output("sl-form-school", "value", allow_duplicate=True),
              Output("sl-form-pos", "value"), Output("sl-form-bt", "value"), Output("sl-form-ovr", "value"),
              Output("sl-form-date", "value"), Output("sl-form-body", "value"),
              *[Output(f"sl-g-{g}", "value") for g in scouting.HITTER_GRADES],
              Output("sl-g-Control", "value"),
              *[Output(f"sl-p{i}-name", "value") for i in range(1, 7)],
              *[Output(f"sl-p{i}-grade", "value") for i in range(1, 7)],
              Output("sl-form", "style", allow_duplicate=True),
              Input({"type": "sl-edit", "key": ALLPM, "thread": ALLPM}, "n_clicks"),
              prevent_initial_call=True)
def cb_sl_edit(_clicks):
    trig = ctx.triggered_id
    if not trig or not ctx.triggered or not ctx.triggered[0]["value"]:
        raise PreventUpdate                       # table re-renders recreate the buttons
    versions = scouting.versions_for_thread(trig["key"], trig["thread"])
    if not versions:
        raise PreventUpdate
    v = versions[0]
    g = v["grades"] or {}
    pitches = (g.get("pitches") or [])[:6]
    pnames = [p.get("name") for p in pitches] + [None] * (6 - len(pitches))
    pgrades = [p.get("grade") for p in pitches] + [None] * (6 - len(pitches))
    return (v["role"], v["player_name"], v["school"], v["position"], v.get("bats_throws"), v["ovr"],
            v.get("report_date") or date.today().isoformat(), v["body"],
            *[g.get(x) for x in scouting.HITTER_GRADES], g.get("Control"),
            *pnames, *pgrades, {**_SL_FORM_STYLE, "display": "block"})


@app.callback(Output("sl-del-status", "children"), Output("sl-version", "data", allow_duplicate=True),
              Input({"type": "sl-del", "key": ALLPM, "thread": ALLPM}, "submit_n_clicks"),
              State("sl-auth", "data"), State("sl-version", "data"), prevent_initial_call=True)
def cb_sl_delete(_clicks, auth, version):
    trig = ctx.triggered_id
    if not trig or not ctx.triggered or not ctx.triggered[0]["value"]:
        raise PreventUpdate
    a = scouting.verify_author((auth or {}).get("initials"), (auth or {}).get("pw"))
    if a is None:
        return "✗ Not signed in — nothing removed.", no_update
    thread = trig["thread"]
    own = thread == f"a{a['id']}"
    if not (own or a["is_admin"]):                # authors remove their own; admins remove any
        return "✗ You can only remove your own report.", no_update
    scouting.delete_thread(trig["key"], thread)
    return "✓ Report removed.", (version or 0) + 1


app.layout = html.Div([dcc.Store(id="retag-version", data=0),
                       dcc.Store(id="cluster-skip", data=[]), dcc.Store(id="cluster-current-uid"),
                       dcc.Store(id="bio-version", data=0), dcc.Store(id="bio-target"),
                       dcc.Store(id="bbio-version", data=0), dcc.Store(id="bbio-target"), selection,
                       html.Div([about_panel, shortlist_panel, pitcher_report, batter_report], style={"padding": "12px 16px"})],
                      style={"fontFamily": FONT})


# ── Player-role label ────────────────────────────────────────────────────────────────────────────
@app.callback(Output("player-label", "children"), Input("role-tabs", "value"))
def cb_label(role):
    return "Batter" if role == "batter" else "Pitcher"


# ── About tab: show the About panel, hide the picker controls (reports hide via the player reset) ──
@app.callback(Output("about-panel", "style"), Output("picker-controls", "style"),
              Output("shortlist-panel", "style"), Input("role-tabs", "value"))
def cb_about_toggle(role):
    about = {"padding": "8px 4px"} if role == "about" else {"display": "none"}
    shortlist = {"padding": "8px 4px"} if role == "shortlist" else {"display": "none"}
    picker = {"display": "none"} if role in ("about", "shortlist") else {"display": "block"}
    return about, picker, shortlist


# ── Cascading pickers (role-aware; all off the small in-memory index) ─────────────────────────────
@app.callback(Output("conf-dd", "options"), Output("conf-dd", "value"),
              Input("role-tabs", "value"), Input("year-check", "value"), Input("level-dd", "value"))
def cb_conf(role, years_sel, level):
    if role not in ("pitcher", "batter"):
        return [], ALL
    return [{"label": c, "value": c} for c in data.conference_options(role, years_sel, level)], ALL


@app.callback(Output("team-dd", "options"), Output("team-dd", "value"),
              Input("role-tabs", "value"), Input("year-check", "value"),
              Input("level-dd", "value"), Input("conf-dd", "value"))
def cb_team(role, years_sel, level, conf):
    if role not in ("pitcher", "batter"):
        return [], ALL
    return data.team_options(role, years_sel, level, conf), ALL


@app.callback(Output("player-dd", "options"), Output("player-dd", "value"),
              Input("role-tabs", "value"), Input("year-check", "value"), Input("level-dd", "value"),
              Input("conf-dd", "value"), Input("team-dd", "value"))
def cb_player(role, years_sel, level, conf, team):
    if role not in ("pitcher", "batter"):
        return [], None
    return data.player_options(role, years_sel, level, conf, team), None


# ── Build the PITCHER report ─────────────────────────────────────────────────────────────────────
@app.callback(
    Output("report", "style"), Output("report-header", "children"), Output("summary", "children"),
    Output("move-graph", "figure"), Output("velo-graph", "figure"),
    Output("arsenal", "children"), Output("heat-graph", "figure"), Output("pick-status", "children"),
    Output("batter-check", "value"), Output("count-check", "value"), Output("pct-panel", "children"),
    Output("bio-target", "data"),
    Input("player-dd", "value"), Input("retag-version", "data"), Input("bio-version", "data"),
    State("role-tabs", "value"), State("year-check", "value"),
    State("level-dd", "value"), State("team-dd", "value"),
)
def cb_build(pitcher, _rv, _bv, role, years_sel, level, team):
    hidden = {"display": "none"}
    if role != "pitcher":
        return (hidden, *([no_update] * 11))
    if not pitcher:
        return (hidden, "", "", go.Figure(), go.Figure(), "", go.Figure(),
                "Pick filters, then choose a pitcher.", [], [], "", None)

    pitches = data.get_rows("pitcher", pitcher, level, team, years_sel)
    if pitches.height == 0:
        msg = html.Div(html.I(f"No rows for '{pitcher}' under the current filters."))
        return {"display": "block"}, msg, "", go.Figure(), go.Figure(), "", go.Figure(), "", [], [], "", None

    hand = report.hand_of(pitches)
    teams = ", ".join(data.team_label(t) for t in _uniq(pitches["PitcherTeam"]))
    yrs = ", ".join(_uniq(pitches["Year"]))
    sub = [teams, yrs, f"{pitches.height:,} pitches"]
    # AutoCluster view: while active, the report's tags are this pitcher's cluster labels/renames.
    ent = data.cluster_state(pitcher)
    colors = None
    if ent:
        pitches = data.cluster_view(pitches, pitcher)
        colors = _cluster_palette(ent)
        sub.append(f"AutoCluster view · k={ent['k']}")
    pid = _modal_id(pitches, "PitcherId")
    bio = data.bio_lookup(pitcher, pid)
    header = _player_header(f"{pitcher} ({hand})", sub,
                            _bio_line(bio, sorted(int(y) for y in _uniq(pitches["Year"]))))
    # A new pitcher starts fresh at All batters / all counts (like the notebook's per-run splits).
    return ({"display": "block"}, header, html_table(report.build_summary(pitches)),
            report.movement_fig(pitches, colors), report.velocity_fig(pitches, pitcher, hand, colors),
            _arsenal_block(pitches, [], []), report.heatmap_fig(pitches), "", [], [],
            _pct_panel(pitcher, level, years_sel), {"name": pitcher, "id": pid})


def _modal_id(df, col):
    """The most common non-null id in ``col`` (a name filter usually maps to one TrackMan id)."""
    if col not in df.columns:
        return None
    s = df[col].drop_nulls()
    return s.mode()[0] if s.len() else None


def _save_bio(n, height_raw, bday_raw, target, ver):
    """Shared by the pitcher + batter edit forms. Returns (status message, new version | no_update);
    bumping the version reruns that report's build callback so the header re-renders with the edit."""
    if not n or not target or not target.get("name"):
        raise PreventUpdate
    height_in, err = _parse_height_input(height_raw)
    if err:
        return err, no_update
    bday, err = _parse_bday_input(bday_raw)
    if err:
        return err, no_update
    if height_in is None and bday is None:
        return "Enter a height and/or birthday.", no_update
    data.save_manual_bio(target["name"], target.get("id"), height_in, bday)
    return "Saved.", (ver or 0) + 1


@app.callback(
    Output("bio-save-status", "children"), Output("bio-version", "data"),
    Input("bio-save-btn", "n_clicks"),
    State("bio-height-input", "value"), State("bio-bday-input", "value"),
    State("bio-target", "data"), State("bio-version", "data"),
    prevent_initial_call=True,
)
def cb_save_bio(n, height_raw, bday_raw, target, ver):
    return _save_bio(n, height_raw, bday_raw, target, ver)


@app.callback(
    Output("bbio-save-status", "children"), Output("bbio-version", "data"),
    Input("bbio-save-btn", "n_clicks"),
    State("bbio-height-input", "value"), State("bbio-bday-input", "value"),
    State("bbio-target", "data"), State("bbio-version", "data"),
    prevent_initial_call=True,
)
def cb_save_batter_bio(n, height_raw, bday_raw, target, ver):
    return _save_bio(n, height_raw, bday_raw, target, ver)


# ── Build the BATTER report ──────────────────────────────────────────────────────────────────────
@app.callback(
    Output("batter-report", "style"), Output("batter-header", "children"),
    Output("batter-summary", "children"), Output("batter-pitchtable", "children"),
    Output("spray-graph", "figure"), Output("launch-graph", "figure"), Output("bheat-graph", "figure"),
    Output("batter-vs", "value"), Output("batter-family", "value"),
    Output("pick-status", "children", allow_duplicate=True), Output("bbio-target", "data"),
    Input("player-dd", "value"), Input("retag-version", "data"), Input("bbio-version", "data"),
    State("role-tabs", "value"), State("year-check", "value"),
    State("level-dd", "value"), State("team-dd", "value"),
    prevent_initial_call=True,
)
def cb_build_batter(batter, _rv, _bv, role, years_sel, level, team):
    hidden = {"display": "none"}
    if role != "batter" or not batter:
        return hidden, *([no_update] * 10)

    rows = data.get_rows("batter", batter, level, team, years_sel)
    if rows.height == 0:
        return ({"display": "block"}, html.Div(html.I(f"No rows for '{batter}'.")),
                "", "", go.Figure(), go.Figure(), go.Figure(), "All", "All", "", None)

    bats = report.bats_of(rows)
    teams = ", ".join(data.team_label(t) for t in _uniq(rows["BatterTeam"]))
    yrs = ", ".join(_uniq(rows["Year"]))
    summ = report.build_batter_summary(rows)
    pa = summ["PA"][0]
    bid = _modal_id(rows, "BatterId")
    bio = data.bio_lookup(batter, bid)
    header = _player_header(f"{batter} ({bats})", [teams, yrs, f"{pa} PA", f"{rows.height:,} pitches seen"],
                            _bio_line(bio, sorted(int(y) for y in _uniq(rows["Year"]))))
    return ({"display": "block"}, header, _scroll(html_table(summ)),
            _batter_pitchtable_block(rows, "All"), report.spray_fig(rows, batter, bats),
            report.batter_launch_fig(rows),
            report.batter_heatmap_fig(rows, "All"), "All", "All", "", {"name": batter, "id": bid})


# ── Batter pitch-type table: vs All / RHP / LHP ──────────────────────────────────────────────────
@app.callback(
    Output("batter-pitchtable", "children", allow_duplicate=True),
    Output("spray-graph", "figure", allow_duplicate=True),
    Output("launch-graph", "figure", allow_duplicate=True),
    Input("batter-vs", "value"),
    State("role-tabs", "value"), State("player-dd", "value"), State("year-check", "value"),
    State("level-dd", "value"), State("team-dd", "value"),
    prevent_initial_call=True,
)
def cb_batter_vs(vs, role, batter, years_sel, level, team):
    if role != "batter" or not batter:
        raise PreventUpdate
    rows = data.get_rows("batter", batter, level, team, years_sel)
    dff = rows.filter(rows["PitcherThrows"] == vs) if vs in ("Right", "Left") else rows
    return (_batter_pitchtable_block(rows, vs),
            report.spray_fig(dff, batter, report.bats_of(rows)),
            report.batter_launch_fig(dff))


# ── Batter location chart: Family × vs-hand -> EV/Whiff surfaces (in place, keeps stat toggle) ────
@app.callback(
    Output("bheat-graph", "figure", allow_duplicate=True),
    Input("batter-family", "value"), Input("batter-vs", "value"),
    State("role-tabs", "value"), State("player-dd", "value"), State("year-check", "value"),
    State("level-dd", "value"), State("team-dd", "value"),
    prevent_initial_call=True,
)
def cb_batter_heat(family, vs, role, batter, years_sel, level, team):
    if role != "batter" or not batter:
        raise PreventUpdate
    rows = data.get_rows("batter", batter, level, team, years_sel)
    if vs in ("Right", "Left"):
        rows = rows.filter(rows["PitcherThrows"] == vs)
    surfaces, n = report.batter_heat_data(rows, family or "All")
    patch = Patch()
    for i, (x, y, z) in enumerate(surfaces):
        patch["data"][i]["x"] = x
        patch["data"][i]["y"] = y
        patch["data"][i]["z"] = z
    hand = {"Right": " vs RHP", "Left": " vs LHP"}.get(vs, "")
    patch["layout"]["title"]["text"] = f"Location · {family or 'All'}{hand} · {n:,} pitches"
    return patch


# ── Batter/Count splits -> arsenal table + heatmap (pitcher report only) ──────────────────────────
@app.callback(
    Output("arsenal", "children", allow_duplicate=True),
    Output("heat-graph", "figure", allow_duplicate=True),
    Input("batter-check", "value"), Input("count-check", "value"),
    State("role-tabs", "value"), State("player-dd", "value"), State("year-check", "value"),
    State("level-dd", "value"), State("team-dd", "value"),
    prevent_initial_call=True,
)
def cb_splits(batter, count, role, pitcher, years_sel, level, team):
    if role != "pitcher" or not pitcher:
        raise PreventUpdate
    pitches = data.get_rows("pitcher", pitcher, level, team, years_sel)
    if data.cluster_state(pitcher):
        pitches = data.cluster_view(pitches, pitcher)     # keep the arsenal in cluster labels too
    dff = pitches.filter(report.hand_mask(batter) & report.count_mask(count))
    return _arsenal_block(dff, batter, count), _heat_patch(dff)


# ── Rebuild the active role's cached picker index (e.g. after new games land) ─────────────────────
@app.callback(
    Output("pick-status", "children", allow_duplicate=True),
    Input("refresh-btn", "n_clicks"), State("role-tabs", "value"), prevent_initial_call=True,
)
def cb_refresh(_n, role):
    data.get_index(role, force_rebuild=True)
    data.clear_percentile_pools()          # new games must flow into the percentile pools too
    s = data.index_stats(role)
    return f"{role.title()} index rebuilt — {s['players']:,} {role}s · {s['teams']:,} teams · {s['combos']:,} combos."


# ── Retag tool ───────────────────────────────────────────────────────────────────────────────────
@app.callback(Output("retag-lasso-status", "children"), Input("move-graph", "selectedData"))
def cb_lasso_status(sel):
    n = len(sel["points"]) if sel and sel.get("points") else 0
    return f"{n:,} selected" if n else "(none selected)"


# The lasso retargets clusters while AutoCluster is active (cluster labels replace tags on the chart),
# and pitch types otherwise — so its dropdown options + label follow the current view.
@app.callback(Output("retag-lasso-to", "options"), Output("retag-lasso-to", "placeholder"),
              Output("retag-lasso-label", "children"),
              Input("retag-version", "data"), Input("player-dd", "value"), State("role-tabs", "value"))
def cb_lasso_options(_rv, pitcher, role):
    ent = data.cluster_state(pitcher) if (role == "pitcher" and pitcher) else None
    if ent:
        return (_cluster_options(ent), "cluster…",
                "① Lasso-select pitches on the movement chart, then move them to")
    return (RETAG_TYPE_OPTS, "pitch type…",
            "① Box/lasso-select pitches on the movement chart, then assign to")


@app.callback(Output("retag-info", "children"),
              Input("retag-version", "data"), Input("player-dd", "value"), State("role-tabs", "value"))
def cb_retag_info(_rv, pitcher, role):
    s = data.retag_summary(pitcher if role == "pitcher" else None)
    return f"Active: {s['pitches']:,} individual pitch(es) retagged." if s["pitches"] else "No retags yet."


@app.callback(Output("retag-version", "data"),
              Input("retag-lasso-apply", "n_clicks"),
              State("move-graph", "selectedData"), State("retag-lasso-to", "value"),
              State("player-dd", "value"), State("role-tabs", "value"), State("retag-version", "data"),
              prevent_initial_call=True)
def cb_retag_lasso(_n, sel, to, pitcher, role, ver):
    if role != "pitcher" or not pitcher or to is None or not (sel and sel.get("points")):
        raise PreventUpdate
    uids = [p["customdata"][6] for p in sel["points"] if p.get("customdata")]
    if not uids:
        raise PreventUpdate
    ent = data.cluster_state(pitcher)
    if ent:                                    # AutoCluster active: lasso bulk-reassigns to a cluster
        try:                                   # (guards against a stale tag value left in the dropdown)
            ci = int(to)
        except (TypeError, ValueError):
            raise PreventUpdate
        if not (0 <= ci < ent["k"]):
            raise PreventUpdate
        data.set_cluster_assignments(pitcher, uids, ci)
    else:                                       # normal view: pitch-type retag override
        if not isinstance(to, str):
            raise PreventUpdate
        data.set_pitch_overrides(uids, to, pitcher)
    return (ver or 0) + 1


@app.callback(Output("retag-version", "data", allow_duplicate=True),
              Input("retag-reset-pitcher", "n_clicks"), Input("retag-reset-all", "n_clicks"),
              State("player-dd", "value"), State("retag-version", "data"), prevent_initial_call=True)
def cb_retag_reset(_np, _na, pitcher, ver):
    if ctx.triggered_id == "retag-reset-all":
        data.clear_retags(None)
    elif ctx.triggered_id == "retag-reset-pitcher" and pitcher:
        data.clear_retags(pitcher)
    else:
        raise PreventUpdate
    return (ver or 0) + 1


# ── AutoCluster (GMM) ────────────────────────────────────────────────────────────────────────────
@app.callback(Output("retag-version", "data", allow_duplicate=True),
              Output("cluster-status", "children", allow_duplicate=True),
              Output("cluster-skip", "data", allow_duplicate=True),
              Input("cluster-run", "n_clicks"), Input("cluster-revert", "n_clicks"),
              State("cluster-release", "value"), State("player-dd", "value"), State("role-tabs", "value"),
              State("year-check", "value"), State("level-dd", "value"), State("team-dd", "value"),
              State("retag-version", "data"), prevent_initial_call=True)
def cb_cluster_run(_nr, _nv, release, pitcher, role, years_sel, level, team, ver):
    if role != "pitcher" or not pitcher:
        raise PreventUpdate
    if ctx.triggered_id == "cluster-revert":
        if not data.cluster_state(pitcher):
            raise PreventUpdate
        data.clear_autocluster(pitcher)
        return (ver or 0) + 1, no_update, []          # fresh run -> clear any skipped-pitch memory
    rows = data.get_rows("pitcher", pitcher, level, team, years_sel)
    try:
        data.run_autocluster(pitcher, rows, use_release=bool(release))
    except ValueError as e:
        return no_update, str(e), no_update
    return (ver or 0) + 1, no_update, []


@app.callback(Output("cluster-status", "children"), Output("cluster-rename-area", "children"),
              Input("retag-version", "data"), Input("player-dd", "value"), State("role-tabs", "value"))
def cb_cluster_ui(_rv, pitcher, role):
    if role != "pitcher" or not pitcher:
        return "", ""
    ent = data.cluster_state(pitcher)
    if not ent:
        return "No clustering for this pitcher.", ""
    feats = "6 features (+release)" if len(ent["features"]) == 6 else "4 features"
    status = (f"k={ent['k']} clusters · {ent['n']:,} pitches clustered"
              + (f" · {ent['n_unclustered']:,} unclustered" if ent["n_unclustered"] else "")
              + f" · {feats}. Name the clusters below — the report updates live.")
    return status, _cluster_rename_area(ent)


# ── AutoCluster: one-at-a-time review of low-confidence pitches ───────────────────────────────────
@app.callback(Output("cluster-review-area", "children"), Output("cluster-current-uid", "data"),
              Input("retag-version", "data"), Input("player-dd", "value"), Input("cluster-skip", "data"),
              State("role-tabs", "value"), State("year-check", "value"),
              State("level-dd", "value"), State("team-dd", "value"))
def cb_cluster_review(_rv, pitcher, skipped, role, years_sel, level, team):
    if role != "pitcher" or not pitcher:
        return "", None
    ent = data.cluster_state(pitcher)
    if not ent or not ent.get("conf"):          # nothing to review (or a pre-confidence run)
        return "", None
    rows = data.get_rows("pitcher", pitcher, level, team, years_sel)
    queue = [p for p in data.unsure_pitches(pitcher, rows) if p["uid"] not in (skipped or [])]
    if not queue:
        return html.Div("✓ No unsure pitches to review.",
                        style={"fontSize": "12px", "color": "#2a7a3a", "margin": "2px 0 6px 14px"}), None
    return _review_area(ent, queue[0], len(queue)), queue[0]["uid"]


@app.callback(Output("retag-version", "data", allow_duplicate=True),
              Input("clu-assign-apply", "n_clicks"),
              State("clu-assign-to", "value"), State("cluster-current-uid", "data"),
              State("player-dd", "value"), State("retag-version", "data"), prevent_initial_call=True)
def cb_cluster_assign(_n, cluster_idx, uid, pitcher, ver):
    if not pitcher or uid is None or cluster_idx is None:
        raise PreventUpdate
    data.set_cluster_assignment(pitcher, uid, cluster_idx)     # bumping the version re-renders the report + card
    return (ver or 0) + 1


@app.callback(Output("cluster-skip", "data", allow_duplicate=True),
              Input("clu-assign-skip", "n_clicks"),
              State("cluster-current-uid", "data"), State("cluster-skip", "data"),
              prevent_initial_call=True)
def cb_cluster_skip(_n, uid, skipped):
    if uid is None:
        raise PreventUpdate
    skipped = list(skipped or [])
    if uid not in skipped:
        skipped.append(uid)                                    # session-only: advances past this pitch
    return skipped


@app.callback(Output("retag-version", "data", allow_duplicate=True),
              Input({"type": "clu-name", "index": ALLPM}, "value"),
              State("player-dd", "value"), State("retag-version", "data"), prevent_initial_call=True)
def cb_cluster_rename(_values, pitcher, ver):
    if not pitcher:
        raise PreventUpdate
    ent = data.cluster_state(pitcher)
    if not ent:
        raise PreventUpdate
    # Map submitted dropdown values by their pattern index, then only save real changes —
    # this also breaks the render -> input-fires -> render loop of pattern components.
    submitted = {str(item["id"]["index"]): (item.get("value") or None) for item in ctx.inputs_list[0]}
    changed = {i: v for i, v in submitted.items() if i in ent["names"] and ent["names"][i] != v}
    if not changed:
        raise PreventUpdate
    for i, v in changed.items():
        data.set_cluster_name(pitcher, int(i), v)
    return (ver or 0) + 1


@app.callback(Output("cluster-download", "data"), Output("cluster-dl-status", "children"),
              Input("cluster-dl-csv", "n_clicks"), Input("cluster-dl-parquet", "n_clicks"),
              State("player-dd", "value"), State("role-tabs", "value"),
              State("year-check", "value"), State("level-dd", "value"), State("team-dd", "value"),
              prevent_initial_call=True)
def cb_cluster_download(_nc, _np, pitcher, role, years_sel, level, team):
    if role != "pitcher" or not pitcher:
        raise PreventUpdate
    if not data.cluster_state(pitcher):
        return no_update, "Run AutoCluster first — the export includes the ClusterTag column."
    df = data.download_frame(pitcher, level, team, years_sel)
    if df.height == 0:
        return no_update, "No rows to export under the current filters."
    base = _safe_name(pitcher) + "_autocluster"
    if ctx.triggered_id == "cluster-dl-parquet":
        buf = io.BytesIO()
        df.write_parquet(buf)
        return dcc.send_bytes(buf.getvalue(), f"{base}.parquet"), f"Exported {df.height:,} rows."
    return dcc.send_string(df.write_csv(), f"{base}.csv"), f"Exported {df.height:,} rows."


def main():
    host = os.environ.get("PITCHER_HOST", "127.0.0.1")
    port = int(os.environ.get("PITCHER_PORT", "8765"))
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
