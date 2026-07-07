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
import os
import re
from datetime import date, datetime

import numpy as np
import plotly.graph_objects as go
from dash import ALL as ALLPM, Dash, Input, Output, Patch, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from . import data, report

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
         "Chase %": 66, "Ground Ball %": 98, "Fly Ball %": 76, "Line Drive %": 90, "Pop Up %": 74,
         "Hard Hit %": 84, "Avg EV": 68}


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
        if kind == "family":
            st |= {"fontWeight": 700, "background": "#faf3f4"}
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
        first = html.Div([html.Span("▶", className="fam-arrow",
                                    style={"display": "inline-block", "marginRight": "7px", "fontSize": "10px", "color": MAROON}),
                          html.Span(fam["agg"]["Pitch"])],
                         style={"padding": "4px 8px", "fontSize": "12px", "fontFamily": FONT, "fontWeight": 700,
                                "background": "#faf3f4", "borderBottom": "1px solid #eee", "whiteSpace": "nowrap"})
        fam_cells = [first] + [_bt_cell(fam["agg"][c], i, "family") for i, c in enumerate(cols) if i > 0]
        summary = html.Summary(html.Div(fam_cells, style=grid))
        subs = [html.Div([_bt_cell(sub[c], i, "sub") for i, c in enumerate(cols)], style=grid) for sub in fam["subs"]]
        blocks.append(html.Details([summary, *subs], className="fam"))
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
app = Dash(__name__, title="delispice_app")
server = app.server

_default_level = "D1" if "D1" in data.levels() else ALL


def _checklist(cid, options, inline=True):
    return dcc.Checklist(id=cid, options=[{"label": o, "value": o} for o in options], value=[],
                         inline=inline, inputStyle={"marginRight": "4px", "marginLeft": "10px"},
                         style={"fontFamily": FONT, "fontSize": "13px", "display": "inline-block"})


def _dropdown(did, options, value, width=None):
    style = {**DD, "width": width} if width else DD
    return dcc.Dropdown(id=did, options=options, value=value, clearable=False, style=style)


role_tabs = dcc.Tabs(id="role-tabs", value="about", style={"width": "285px", "marginBottom": "10px"},
                     colors={"primary": MAROON, "background": "#faf7f7", "border": "#e2c9cc"},
                     children=[dcc.Tab(label="About", value="about"),
                               dcc.Tab(label="Pitchers", value="pitcher"),
                               dcc.Tab(label="Batters", value="batter")])

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
        html.Div([html.Span("① Box/lasso-select pitches on the movement chart, then assign to", style={"fontSize": "12px"}),
                  dcc.Dropdown(id="retag-lasso-to", options=RETAG_TYPE_OPTS, placeholder="pitch type…", style=_RDD),
                  html.Button("Assign selected", id="retag-lasso-apply", n_clicks=0, style=_BTN),
                  html.Span(id="retag-lasso-status", style={"marginLeft": "6px", "color": "#666", "fontSize": "12px"})],
                 style=_ROW),
        html.Div([html.Span("② Remap", style={"fontSize": "12px"}),
                  dcc.Dropdown(id="retag-from", options=RETAG_TYPE_OPTS, placeholder="from…", style=_RDD),
                  html.Span("→", style={"fontSize": "13px"}),
                  dcc.Dropdown(id="retag-to", options=RETAG_TYPE_OPTS, placeholder="to…", style=_RDD),
                  dcc.Checklist(id="retag-global", options=[{"label": " all pitchers (global)", "value": "g"}],
                                value=[], style={"display": "inline-block", "fontSize": "12px", "fontFamily": FONT}),
                  html.Button("Apply remap", id="retag-remap-apply", n_clicks=0, style=_BTN)],
                 style=_ROW),
        html.Div([html.Span(id="retag-info", style={"color": "#666", "fontSize": "12px", "marginRight": "10px"}),
                  html.Button("Reset this pitcher", id="retag-reset-pitcher", n_clicks=0, style=_BTN),
                  html.Button("Reset all", id="retag-reset-all", n_clicks=0, style=_BTN)],
                 style={"display": "flex", "alignItems": "center", "flexWrap": "wrap"}),
        html.Hr(style={"margin": "8px 0"}),
        html.Div([html.Span("③ AutoCluster (GMM)", style={"fontSize": "12px", "fontWeight": 700}),
                  dcc.Checklist(id="cluster-release",
                                options=[{"label": " + RelHeight & Extension", "value": "r"}], value=[],
                                style={"display": "inline-block", "fontSize": "12px", "fontFamily": FONT}),
                  html.Button("Run AutoCluster", id="cluster-run", n_clicks=0, style=_BTN),
                  html.Button("Revert clustering", id="cluster-revert", n_clicks=0, style=_BTN),
                  html.Span(id="cluster-status", style={"marginLeft": "6px", "color": "#666", "fontSize": "12px"})],
                 style=_ROW),
        html.Div(id="cluster-rename-area"),
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
        html.Div([_graph("spray-graph"), _graph("bheat-graph")],
                 style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),
    ]),
])

# About tab landing panel (default view; the tab hides the picker + reports)
about_panel = html.Div(id="about-panel", style={"padding": "8px 4px"}, children=[
    html.H2("delispice", style={"fontFamily": FONT, "color": MAROON, "margin": "0 0 4px"}),
    html.Div("TrackMan pitcher & batter scouting reports.",
             style={"fontFamily": FONT, "fontSize": "15px", "color": "#444", "marginBottom": "14px"}),
    html.P(["Pick the ", html.B("Pitchers"), " or ", html.B("Batters"),
            " tab above to begin. Filter by year, level, conference, and team, then choose a "
            "player to build their report."],
           style={"fontFamily": FONT, "fontSize": "14px", "maxWidth": "680px", "lineHeight": "1.5"}),
    html.Ul([
        html.Li([html.B("Pitchers"), " — arsenal table, movement & velocity charts, location "
                 "heatmaps, percentile sliders, plus pitch retagging and AutoCluster tools."]),
        html.Li([html.B("Batters"), " — pitch-type breakdown, spray chart, and location heatmaps "
                 "(exit velo / whiff% / chase%) by pitch family and pitcher hand."]),
    ], style={"fontFamily": FONT, "fontSize": "14px", "maxWidth": "680px", "lineHeight": "1.6"}),
])

app.layout = html.Div([dcc.Store(id="retag-version", data=0),
                       dcc.Store(id="bio-version", data=0), dcc.Store(id="bio-target"),
                       dcc.Store(id="bbio-version", data=0), dcc.Store(id="bbio-target"), selection,
                       html.Div([about_panel, pitcher_report, batter_report], style={"padding": "12px 16px"})],
                      style={"fontFamily": FONT})


# ── Player-role label ────────────────────────────────────────────────────────────────────────────
@app.callback(Output("player-label", "children"), Input("role-tabs", "value"))
def cb_label(role):
    return "Batter" if role == "batter" else "Pitcher"


# ── About tab: show the About panel, hide the picker controls (reports hide via the player reset) ──
@app.callback(Output("about-panel", "style"), Output("picker-controls", "style"),
              Input("role-tabs", "value"))
def cb_about_toggle(role):
    if role == "about":
        return {"padding": "8px 4px"}, {"display": "none"}
    return {"display": "none"}, {"display": "block"}


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
    Output("spray-graph", "figure"), Output("bheat-graph", "figure"),
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
        return hidden, *([no_update] * 9)

    rows = data.get_rows("batter", batter, level, team, years_sel)
    if rows.height == 0:
        return ({"display": "block"}, html.Div(html.I(f"No rows for '{batter}'.")),
                "", "", go.Figure(), go.Figure(), "All", "All", "", None)

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
            report.batter_heatmap_fig(rows, "All"), "All", "All", "", {"name": batter, "id": bid})


# ── Batter pitch-type table: vs All / RHP / LHP ──────────────────────────────────────────────────
@app.callback(
    Output("batter-pitchtable", "children", allow_duplicate=True),
    Output("spray-graph", "figure", allow_duplicate=True),
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
            report.spray_fig(dff, batter, report.bats_of(rows)))


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


@app.callback(Output("retag-info", "children"),
              Input("retag-version", "data"), Input("player-dd", "value"), State("role-tabs", "value"))
def cb_retag_info(_rv, pitcher, role):
    s = data.retag_summary(pitcher if role == "pitcher" else None)
    parts = []
    if s["global"]:
        parts.append("global " + ", ".join(f"{k}→{v}" for k, v in s["global"].items()))
    if s["pitcher_tag"]:
        parts.append("this pitcher " + ", ".join(f"{k}→{v}" for k, v in s["pitcher_tag"].items()))
    if s["pitches"]:
        parts.append(f"{s['pitches']:,} individual pitch(es)")
    return "Active: " + " · ".join(parts) if parts else "No retags yet."


@app.callback(Output("retag-version", "data"),
              Input("retag-lasso-apply", "n_clicks"),
              State("move-graph", "selectedData"), State("retag-lasso-to", "value"),
              State("player-dd", "value"), State("role-tabs", "value"), State("retag-version", "data"),
              prevent_initial_call=True)
def cb_retag_lasso(_n, sel, to_type, pitcher, role, ver):
    if role != "pitcher" or not pitcher or not to_type or not (sel and sel.get("points")):
        raise PreventUpdate
    uids = [p["customdata"][6] for p in sel["points"] if p.get("customdata")]
    data.set_pitch_overrides(uids, to_type, pitcher)
    return (ver or 0) + 1


@app.callback(Output("retag-version", "data", allow_duplicate=True),
              Input("retag-remap-apply", "n_clicks"),
              State("retag-from", "value"), State("retag-to", "value"), State("retag-global", "value"),
              State("player-dd", "value"), State("role-tabs", "value"), State("retag-version", "data"),
              prevent_initial_call=True)
def cb_retag_remap(_n, frm, to, glob, pitcher, role, ver):
    if not frm or not to:
        raise PreventUpdate
    if glob:
        data.set_global_rule(frm, to)
    elif role == "pitcher" and pitcher:
        data.set_pitcher_tag(pitcher, frm, to)
    else:
        raise PreventUpdate
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
        return (ver or 0) + 1, no_update
    rows = data.get_rows("pitcher", pitcher, level, team, years_sel)
    try:
        data.run_autocluster(pitcher, rows, use_release=bool(release))
    except ValueError as e:
        return no_update, str(e)
    return (ver or 0) + 1, no_update


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
