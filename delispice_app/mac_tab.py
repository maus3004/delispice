"""MAC view, shown inside the USC tab: the matchup analysis calculator (backend/research/mac/mac.md §10, §12).

Pick pitchers and hitters. The table shows every pitcher x hitter matchup in xRV per 100 pitches, from the batter's
side (MAC4: negative = the pitcher's edge), in one of two grades: Pitching+ as thrown, or stuff (every pitch at the
league's typical locations for its group). Fall TrackMan files add fall pitchers, graded on stuff only. Clicking a cell
opens that pair: the parts, his pitch types against this hitter, and his best pitch + location in each count bucket.

The engine and its store live in backend/research/mac (``mac_app.py``, ``mac_fall.py``, git-ignored). This file is
loaded by local_tabs.py into the USC tab. The engine stays on the Mac (backend/research is git-ignored), so on a
machine without it the view says so instead of loading.
"""
from __future__ import annotations

import base64
import importlib
import sys
import threading
from pathlib import Path

import polars as pl
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from . import data, game_report, pitch_lab

TABS = [("MAC", "mac")]
MAC_DIR = Path(__file__).resolve().parents[1] / "backend" / "research" / "mac"
MAX_P, MAX_H = 20, 25                     # table size cap (the first click on a new pitcher costs about a second)
STAFF_MIN, LINEUP_N = 100, 9              # "+ Staff": 2026 arms with 100+ pitches · "+ Lineup": the 9 who saw the most 2026 pitches
SEARCH_N = 40
LOC_MIN_SHARE = 0.03                      # best spot: a location bucket needs 3% of the pitch type's landings in that count
# cell shows -> (label, |value| at full color, decimals)
MODES = {"matchup": ("Matchup", 2.5, 1), "mainP": ("Pitcher level", 2.0, 1), "mainX": ("Hitter level", 2.0, 1),
         "pair": ("Pair-specific", 0.3, 2)}
GRADE_OPTS = [{"label": "Pitching+ (as thrown)", "value": "thrown"}, {"label": "Stuff (league locations)", "value": "stuff"}]
TYPE_NAME = {**pitch_lab.NAME, "FB": "Fastball", "BB": "Breaking ball", "OS": "Offspeed"}
RED, BLUE = (178, 34, 34), (33, 102, 172)          # red = the pitcher's edge (as on the Pitching+ cards), blue = the hitter's
ERR = {"color": "#a33", "fontSize": "13px"}
_LOCK = threading.Lock()                           # the engine's caches aren't thread-safe; one table or pair at a time
_mod = None


def _ma():
    """backend/research/mac/mac_app.py, imported on first use (it pulls in the research build and its models)."""
    global _mod
    if _mod is None:
        if str(MAC_DIR) not in sys.path:
            sys.path.insert(0, str(MAC_DIR))
        _mod = importlib.import_module("mac_app")
    return _mod


def _missing() -> str | None:
    if not (MAC_DIR / "mac_app.py").exists():
        return "backend/research/mac/mac_app.py isn't on this machine."
    try:
        if not _ma().store_ready():
            return "The MAC store isn't built for this version yet. Run: .venv/bin/python backend/research/mac/mac_app.py build"
    except Exception as e:                         # noqa: BLE001 — shown in the tab, not raised
        return f"The MAC engine didn't load: {e}"
    return None


def _E():
    return _ma().engine()


def _team(code) -> str:
    return data.team_label(code) if code else "?"


def _side(h: dict) -> str:
    return "S" if h["switch"] else ("L" if h["side_R"] == 1 else "R")


def _fall_tag(p: dict) -> str:
    return ("fall + 2026" if p.get("link") else "fall only") if p.get("fall") else ""


def _p_label(p: dict) -> str:
    tag = _fall_tag(p)
    return f"{p['name']} · {_team(p['team'])} · {p['hand'][0]}HP" + (f" · {tag}" if tag else "")


def _h_label(h: dict) -> str:
    return f"{h['name']} · {_team(h['team'])} · {_side(h)}"


def _fmt(v: float, d: int = 1) -> str:
    """Signed, with no sign on a value that rounds to zero (no '−0.0')."""
    s = f"{v:+.{d}f}"
    return s[1:] if float(s) == 0 else s


def _color(v: float, cap: float) -> tuple[str, str]:
    a = min(abs(v) / cap, 1.0) * 0.85
    r, g, b = RED if v < 0 else BLUE
    return f"rgba({r},{g},{b},{a:.2f})", ("#fff" if a > 0.5 else "#222")


def _names(n: str) -> str:
    """'Last, First' -> 'First Last' for the table's tight headers."""
    if "," in n:
        last, first = (s.strip() for s in n.split(",", 1))
        return f"{first} {last}"
    return n


def _team_options(E) -> list[dict]:
    codes = set(E.pitchers["team"].drop_nulls().to_list()) | set(E.hitters["team"].drop_nulls().to_list())
    return sorted(({"label": _team(t), "value": t} for t in codes), key=lambda o: o["label"])


def _options(frame: pl.DataFrame, key: str, label, team, search, sel, order: str) -> list[dict]:
    sel = sel or []
    parts = [frame.filter(pl.col(key).is_in(sel))]
    if team:
        parts.append(frame.filter(pl.col("team") == team).sort(order, descending=True))
    if search and len(search) >= 2:
        parts.append(frame.filter(pl.col("name").str.to_lowercase().str.contains(search.lower(), literal=True))
                          .sort(order, descending=True).head(SEARCH_N))
    seen, out = set(), []
    for part in parts:
        for r in part.iter_rows(named=True):
            if r[key] not in seen:
                seen.add(r[key])
                out.append({"label": label(r), "value": r[key]})
    return out


# ── the table ────────────────────────────────────────────────────────────────────────────────────
def _table(m, E, M: pl.DataFrame, pids: list[str], hids: list[str], mode: str, pair: dict | None, grade: str):
    _label, cap, dec = MODES[mode]
    v = {(r["pid"], r["hid"]): r for r in M.iter_rows(named=True)}
    th = {**m.TH, "whiteSpace": "normal", "padding": "4px 6px", "fontSize": "12px", "verticalAlign": "bottom", "minWidth": "62px"}
    sub = {"fontWeight": 400, "fontSize": "10px", "opacity": 0.85}
    head = [html.Th("Pitcher \\ Hitter", style={**th, "textAlign": "left"})]
    for h in hids:
        H = E.hinfo[h]
        thin = " †" if H["rel_min"] < E.meta["rel_min"] else ""
        head.append(html.Th([html.Div(_names(H["name"]) + thin), html.Div(f"{_team(H['team'])} · {_side(H)}", style=sub)], style=th))
    head.append(html.Th("Avg", style=th))
    rows = [html.Tr(head)]
    td = {**m.TD, "padding": "4px 6px", "fontVariantNumeric": "tabular-nums", "cursor": "pointer"}
    avg_td = {**m.TD, "padding": "4px 6px", "background": "#f3eeee", "fontWeight": 600, "fontVariantNumeric": "tabular-nums"}
    stuff = grade == "stuff"
    for p in pids:
        P = E.pinfo[p]
        tag = _fall_tag(P)
        cells = [html.Td([html.Div(_names(P["name"]), style={"fontWeight": 600}),
                          html.Div(f"{_team(P['team'])} · {P['hand'][0]}HP" + (f" · {tag}" if tag else ""), style={"fontSize": "10px", "color": "#666"})],
                         style={**m.TD, "textAlign": "left", "whiteSpace": "nowrap", "padding": "3px 8px"})]
        for h in hids:
            r = v[(p, h)]
            x = r[mode]
            bg, fg = _color(x, cap)
            thin = r["prel"] < E.meta["rel_min"] or r["hrel"] < E.meta["rel_min"]
            sel = bool(pair) and pair.get("pid") == p and pair.get("hid") == h
            vs = f"{_names(P['name'])} vs {_names(E.hinfo[h]['name'])} ({'LHB' if r['bside'] else 'RHB'})"
            tip = (f"{vs}, stuff: matchup {r['matchup']:+.2f} = pitcher {r['mainP']:+.2f} + hitter {r['mainX']:+.2f} runs/100 (levels only)" if stuff else
                   f"{vs}: matchup {r['matchup']:+.2f} = pitcher {r['mainP']:+.2f} + hitter {r['mainX']:+.2f} + pair {r['pair']:+.2f} runs/100")
            cells.append(html.Td(_fmt(x, dec) + (" †" if thin else ""), id={"type": "mac-cell", "p": p, "h": h}, n_clicks=0, title=tip,
                                 style={**td, "background": bg, "color": fg, "fontStyle": "italic" if thin else "normal",
                                        "outline": "2.5px solid #111" if sel else "none", "outlineOffset": "-2px"}))
        cells.append(html.Td(_fmt(sum(v[(p, h)][mode] for h in hids) / len(hids), dec), style=avg_td))
        rows.append(html.Tr(cells))
    foot = [html.Td("Avg", style={**avg_td, "textAlign": "left"})]
    foot += [html.Td(_fmt(sum(v[(p, h)][mode] for p in pids) / len(pids), dec), style=avg_td) for h in hids]
    foot.append(html.Td(_fmt(M[mode].mean(), dec), style=avg_td))
    rows.append(html.Tr(foot))
    return html.Div(html.Table(rows, style={**m.TABLE, "fontSize": "13px"}), style={"overflowX": "auto"})


# ── one pair ─────────────────────────────────────────────────────────────────────────────────────
def _best_rows(B: pl.DataFrame) -> pl.DataFrame:
    """Drop location buckets his pitch type rarely lands in (in that count), so a best spot is one he really reaches."""
    return (B.with_columns(share=pl.col("dens") / pl.col("dens").sum().over("type", "count"))
             .filter(pl.col("share") >= LOC_MIN_SHARE))


def _spot(r: dict, key: str) -> str:
    return f"{TYPE_NAME.get(r['type'], r['type'])} · {_ma().LOC_LABEL[r['loc']]} · {_fmt(r[key])}"


def _sample_line(E, P: dict, H: dict, r: dict, side: str, grade: str) -> str:
    if P.get("fall"):
        prior = f"{r['n_last']:,} pitches in 2026 (the prior)" if P.get("link") else "no 2026 MAC sample"
        pitcher = (f"Pitcher's sample to {side}: {prior} + {r['n_in']:,} fall pitches, each at {_mf().K_DRAWS} league count × location "
                   f"spots (pitches with no batter split by his side mix) · reliability {r['prel']:.2f}.")
    else:
        pitcher = (f"Pitcher's sample to {side}: {r['n_last']:,} pitches in {E.meta['prior_season']} (the prior) + {r['n_in']:,} in "
                   f"{E.meta['season']}" + (", each at 4 typical cells drawn at its count" if grade == "stuff" else "") + f" · reliability {r['prel']:.2f}.")
    return (f"{pitcher}  Hitter: reliability {r['hrel']:.2f} (lowest of the five skills)"
            f"{'' if H['has_prior'] else ', no ' + str(E.meta['prior_season']) + ' prior'} · {H['n_sw']:,} swings in {E.meta['season']}.")


def _detail(m, E, pid: str, hid: str, grade: str):
    P, H = E.pinfo[pid], E.hinfo[hid]
    M = E.matchups([pid], [hid], grade)
    if not M.height:
        return html.Div(f"{_names(P['name'])} is a fall pitcher, graded on stuff only: switch Grade on to Stuff to open this pair.",
                        style={"color": "#555", "fontSize": "13px", "margin": "14px 0"})
    r = M.row(0, named=True)
    side = "LHB" if r["bside"] else "RHB"
    stuff = grade == "stuff"
    small = {"color": "#555", "fontSize": "12px"}
    tag = _fall_tag(P)
    head = html.Div([html.Span(f"{_names(P['name'])} ({P['hand'][0]}HP, {_team(P['team'])}{', ' + tag if tag else ''})", style={"fontWeight": 700}),
                     html.Span("  vs  ", style={"color": "#888"}),
                     html.Span(f"{_names(H['name'])} ({side}{', switch' if H['switch'] else ''}, {_team(H['team'])})", style={"fontWeight": 700}),
                     html.Span(f"   ·   {'Stuff (league locations)' if stuff else 'Pitching+ (as thrown)'}", style={"color": "#888", "fontSize": "13px"})],
                    style={"fontSize": "17px", "margin": "14px 0 4px"})
    what = ([("His pitches at the league's typical locations for their groups, vs the average P4 hitter, pulled toward the average arm "
              "by his sample (MAC26, row b's prior)"),
             "The average P4 arm's pitches at league locations vs this hitter, at his count mix",
             (f"Not in the stuff grade: int {r['int']:+.2f} failed its 2026 test at league locations (0.39 ± 0.20, z 2.0), so the "
              "matchup is the two levels only (MAC18)"),
             "The sum. Negative = the pitcher's edge"] if stuff else
            ["His pitches vs the average P4 hitter, pulled toward the average arm by his sample (MAC26)",
             "The average P4 arm's pitches vs this hitter, at his count mix",
             f"What is specific to the pair: int {r['int']:+.2f} × {E.s_int(grade)} (its tested size, MAC29)",
             "The sum. Negative = the pitcher's edge"])
    parts = pl.DataFrame({"Part": ["Pitcher level", "Hitter level", "Pair-specific", "Matchup"],
                          "xRV/100": [f"{r['mainP']:+.2f}", f"{r['mainX']:+.2f}", "—" if stuff else f"{r['pair']:+.2f}", f"{r['matchup']:+.2f}"],
                          "What it is": what})
    samp = html.Div(_sample_line(E, P, H, r, side, grade), style={**small, "margin": "4px 0 8px"})
    bt = E.by_type(pid, hid, grade)
    types = pl.DataFrame({"Pitch": [TYPE_NAME.get(t["type"], t["type"]) for t in bt], "Use": [f"{100 * t['use']:.0f}%" for t in bt],
                          "vs this hitter": [_fmt(t["vx"]) for t in bt], "vs avg P4 hitter": [_fmt(t["va"]) for t in bt],
                          "This hitter": [_fmt(t["vx"] - t["va"]) for t in bt]}) if bt else None
    B = _best_rows(E.best(pid, hid))
    best_rows = []
    for cb in ("Ahead", "Even", "Behind", "Full Count"):
        b = B.filter(pl.col("count") == cb)
        if not b.height:
            continue
        bx = b.sort("vx").head(2).to_dicts()
        ba = b.sort("va").row(0, named=True)
        best_rows.append({"Count": cb, "Best vs this hitter": _spot(bx[0], "vx"), "Next best": _spot(bx[1], "vx") if len(bx) > 1 else "",
                          "Best vs avg P4 hitter": _spot(ba, "va"), "This hitter there": _fmt(bx[0]["vx"] - bx[0]["va"])})
    best = pl.DataFrame(best_rows) if best_rows else None
    note = {"color": "#777", "fontSize": "11px", "marginTop": "3px", "maxWidth": "900px"}
    where = "at league locations" if stuff else "as thrown"
    return html.Div([
        head, m.html_table(parts), samp,
        m._title(f"His pitch types vs this hitter ({where}, runs/100 vs the count's average pitch)"),
        m.html_table(types, empty_msg=f"No pitches to {side} in his sample."),
        html.Div("Unshrunk, from his own sample to this side. 'This hitter' = vs this hitter minus vs the average P4 hitter; "
                 "negative = this hitter does worse against that pitch than the average P4 hitter.", style=note),
        m._title("Best pitch + location by count (runs/100 vs that count's average pitch)"),
        m.html_table(best, empty_msg="No pitch type reaches 5% of his pitches to this side."),
        html.Div("Each pitch type's average shape graded at every count × 3-inch cell, averaged into count buckets (PP18) and "
                 "location buckets (PP19), cells weighted by where his pitch group lands in that count. In/away are from the "
                 "hitter's side. 'This hitter there' = the best spot's value vs this hitter minus vs the average P4 hitter. "
                 "The same in both grades.", style=note),
    ])


# ── fall files ───────────────────────────────────────────────────────────────────────────────────
def _mf():
    _ma()                                          # puts backend/research/mac on sys.path
    return importlib.import_module("mac_fall")


def _fall_status(m, E):
    I = E.fall_info
    if not I:
        return None
    P = I["pitchers"]
    n = P.height if P is not None else 0
    n_link = P.filter(~pl.col("Linked to").str.starts_with("no 2026")).height if n else 0
    files = ", ".join(f["file"] for f in I["files"])
    line = (f"{files}: {I['live']:,} Live pitches ({I['warmups']:,} warmups left out"
            + (f", {I['duplicates']:,} repeated pitches" if I["duplicates"] else "") + f"). Graded on stuff: {I['graded']:,}"
            + (f", {I['no_side']:,} of them with no batter (split by his side mix)" if I["no_side"] else "")
            + (f"; {I['not_gradeable']:,} the model can't grade (no shape or hand)" if I["not_gradeable"] else "")
            + f". {n} fall pitchers: {n_link} on 2026 + fall, {n - n_link} fall only.")
    off = I["offsets"].select(pl.col("unit").alias("Date"), pl.col("pitches").alias("Pitches"), pl.col("returners").alias("Returners"),
                              pl.col("off_h").round(2).alias("Height offset (ft)"), pl.col("off_s").round(2).alias("Side offset (ft)"),
                              pl.col("source").alias("From"))
    note = {"color": "#777", "fontSize": "11px", "margin": "6px 0 2px", "maxWidth": "900px"}
    return html.Div([
        html.Div(line),
        html.Details(style={"marginTop": "4px"}, children=[
            html.Summary("Fall pitchers and release offsets", style={"cursor": "pointer", "color": "#8a1520"}),
            m.html_table(P, compact=True, empty_msg="No pitcher could be graded."),
            html.Div("Each date's offset comes off every pitch that day: the staff's shared difference from their 2026 stadium release, "
                     "measured on the returners with each one's own change since 2026 kept (that change is in the table above). A date "
                     "with no returner takes the file's typical offset.", style=note),
            m.html_table(off, compact=True)])])


def _help():
    li = lambda *kids: html.Li(list(kids))
    return html.Details(open=False, style={"margin": "14px 0 4px", "fontSize": "13px", "maxWidth": "900px"}, children=[
        html.Summary("How to read MAC", style={"cursor": "pointer", "color": "#8a1520", "fontSize": "12px"}),
        html.Ul(style={"lineHeight": "1.5", "paddingLeft": "18px"}, children=[
            li(html.B("The number: "), "xRV per 100 pitches from Pitching+'s pieces graded against this hitter's skills, from the batter's "
               "side. Negative = the pitcher's edge. 0 = the average P4 arm's pitch vs the average P4 hitter at the same count."),
            li(html.B("Matchup = pitcher level + hitter level + pair-specific. "), "The levels are what each player brings to any "
               "matchup. The pair-specific part is real but small (about 0.1 runs/100 of real spread, against about 0.7 for each "
               "level), so it is shown at its tested size, × 0.51."),
            li(html.B("Grade on: "), "Pitching+ grades each pitch where he threw it. Stuff grades it at the league's typical "
               "locations for its pitch group (four drawn 3-inch cells, at its own count), so only its shape, release and the hitter "
               "matter: his stuff with neutral command. Stuff shows the two levels only, because its pair-specific part failed the "
               "2026 test (z 2.0). As thrown is the better forecast of games: it beat stuff in every sample bin."),
            li(html.B("Fall files: "), "drag fall TrackMan exports in (Live pitches only). They have no counts, so each fall pitch is "
               "graded on stuff at 12 count × location spots drawn from the league's mix for its group. 2026 is the prior and the "
               "fall is this season; pitchers with no 2026 MAC sample are fall only. Release height and side are corrected by date, "
               "and pitches with no batter are split between RHB and LHB by his side mix. Fall pitchers show in the stuff grade only."),
            li(html.B("Inputs, as of the end of the 2026 season: "), "a hitter's five Pitching+ skills (whiff, foul, contact, zone "
               "swing, chase) blend his 2025 prior with all of 2026. A pitcher's sample is his 2025 + 2026 pitches to that batter "
               "side, 2025 down-weighted, with pitch types he stopped throwing left out."),
            li(html.B("† "), "= below reliability 0.5: a thin sample, already pulled toward average. Read it with care."),
            li(html.B("Model: "), "MAC twin A, the 2022–25 Pitching+ that the 2026 test passed on. Five hitter skills; la_oe "
               "(launch angle) joins with the production refit."),
            li("Switch hitters bat from their usual side against that hand. Values are per 100 pitches, not per plate appearance."),
        ]),
    ])


def panels(m, switch="role-tabs", prop="value") -> list:
    """The MAC panel and its callbacks. ``switch``.``prop`` reads "mac" while MAC is showing: the top tab bar's value
    if MAC is a tab of its own, or the USC tab's ``usc-view`` store (its cards), where it lives now."""
    dd = {"fontSize": "13px"}
    btn = m._BTN
    note = {"fontSize": "11px", "color": "#777", "marginTop": "2px"}
    radio = {"inputStyle": {"marginRight": "4px", "marginLeft": "10px"}, "style": m._GR_RADIO}
    panel = html.Div(id="mac-panel", style={"display": "none"}, children=[
        dcc.Store(id="mac-pair", storage_type="memory"),
        html.Div("MAC: matchups", style={"fontSize": "20px", "fontWeight": 700, "fontFamily": m.FONT, "marginBottom": "2px"}),
        html.Div("xRV per 100 pitches, from the batter's side: red = the pitcher's edge, blue = the hitter's. "
                 "Inputs as of the end of the 2026 season. Click a cell for the pair's breakdown and best pitch + location.",
                 style={"color": "#555", "fontSize": "12px", "marginBottom": "10px"}),
        html.Div(id="mac-status", style=ERR),
        html.Div([html.Span("Pitchers", style={**m.LABEL, "width": "62px"}),
                  dcc.Dropdown(id="mac-p-team", options=[], placeholder="Team…", style={**dd, "width": "220px"}),
                  html.Button("＋ Staff", id="mac-p-add", n_clicks=0, style=btn, title=f"Add the team's 2026 arms with {STAFF_MIN}+ pitches"),
                  dcc.Dropdown(id="mac-p", options=[], value=[], multi=True, placeholder="Type a name, or pick a team…",
                               persistence=True, persistence_type="session", style={**dd, "minWidth": "420px", "flex": "1 1 420px"}),
                  html.Button("Clear", id="mac-p-clear", n_clicks=0, style=btn)],
                 style={**m._ROW, "maxWidth": "1200px"}),
        html.Div([html.Span("Hitters", style={**m.LABEL, "width": "62px"}),
                  dcc.Dropdown(id="mac-h-team", options=[], placeholder="Team…", style={**dd, "width": "220px"}),
                  html.Button("＋ Lineup", id="mac-h-add", n_clicks=0, style=btn, title=f"Add the {LINEUP_N} who saw the most 2026 pitches"),
                  dcc.Dropdown(id="mac-h", options=[], value=[], multi=True, placeholder="Type a name, or pick a team…",
                               persistence=True, persistence_type="session", style={**dd, "minWidth": "420px", "flex": "1 1 420px"}),
                  html.Button("Clear", id="mac-h-clear", n_clicks=0, style=btn)],
                 style={**m._ROW, "maxWidth": "1200px"}),
        html.Div([html.Span("Fall file", style={**m.LABEL, "width": "62px"}),
                  dcc.Upload(id="mac-fall-upload", accept=".csv,.parquet", multiple=True, max_size=game_report.UPLOAD_MAX_BYTES,
                             style=m._UPLOAD_BOX,
                             children=html.Div(["Drag fall TrackMan files here (several at once is fine), or ", html.A("choose them"),
                                                html.Div("Live pitches only · graded on stuff · kept until you remove them or the app restarts",
                                                         style=note)])),
                  html.Button("Remove fall pitchers", id="mac-fall-clear", n_clicks=0, style=btn)],
                 style={**m._ROW, "maxWidth": "1200px", "alignItems": "center"}),
        dcc.Loading(html.Div(id="mac-fall-status", style={"fontSize": "12px", "color": "#444", "margin": "4px 0 6px", "maxWidth": "1150px"}), type="dot"),
        html.Div([html.Span("Grade on", style=m.LABEL),
                  dcc.RadioItems(id="mac-grade", value="thrown", inline=True, options=GRADE_OPTS, persistence=True,
                                 persistence_type="session", **radio)],
                 style={**m._ROW, "marginTop": "4px"}),
        html.Div([html.Span("Cells show", style=m.LABEL),
                  dcc.RadioItems(id="mac-mode", value="matchup", inline=True, options=[{"label": v[0], "value": k} for k, v in MODES.items()],
                                 **radio)],
                 style={**m._ROW, "marginTop": "4px"}),
        dcc.Loading(html.Div(id="mac-table", style={"marginTop": "6px"}), type="dot"),
        html.Div(f"† = below reliability 0.5 (a thin sample). Up to {MAX_P} pitchers × {MAX_H} hitters.",
                 style={"color": "#888", "fontSize": "11px", "marginTop": "4px"}),
        dcc.Loading(html.Div(id="mac-detail"), type="dot"),
        _help(),
    ])

    @m.app.callback(Output("mac-panel", "style"), Output("mac-p-team", "options"), Output("mac-h-team", "options"),
                    Output("mac-status", "children"), Input(switch, prop))
    def cb_mac_toggle(tab):
        if tab != "mac":
            return {"display": "none"}, [], [], ""
        style = {"padding": "8px 4px", "fontFamily": m.FONT}
        if (msg := _missing()) is not None:
            return style, [], [], msg
        with _LOCK:
            opts = _team_options(_E())
        return style, opts, opts, ""

    @m.app.callback(Output("mac-p", "options"), Input("mac-p-team", "value"), Input("mac-p", "search_value"), Input("mac-p", "value"),
                    Input(switch, prop))
    def cb_mac_p_opts(team, search, sel, tab):
        if tab != "mac" or _missing():
            raise PreventUpdate
        return _options(_E().pitchers, "pid", _p_label, team, search, sel, "n26")

    @m.app.callback(Output("mac-h", "options"), Input("mac-h-team", "value"), Input("mac-h", "search_value"), Input("mac-h", "value"),
                    Input(switch, prop))
    def cb_mac_h_opts(team, search, sel, tab):
        if tab != "mac" or _missing():
            raise PreventUpdate
        return _options(_E().hitters, "hid", _h_label, team, search, sel, "n_seen")

    @m.app.callback(Output("mac-p", "value"), Input("mac-p-add", "n_clicks"), Input("mac-p-clear", "n_clicks"),
                    State("mac-p-team", "value"), State("mac-p", "value"), prevent_initial_call=True)
    def cb_mac_p_add(_a, _c, team, sel):
        if ctx.triggered_id == "mac-p-clear":
            return []
        if not team:
            raise PreventUpdate
        P = _E().pitchers.filter(pl.col("team") == team, pl.col("n26") >= STAFF_MIN).sort("n26", descending=True)
        return list(dict.fromkeys([*(sel or []), *P["pid"].to_list()]))[:MAX_P]

    @m.app.callback(Output("mac-h", "value"), Input("mac-h-add", "n_clicks"), Input("mac-h-clear", "n_clicks"),
                    State("mac-h-team", "value"), State("mac-h", "value"), prevent_initial_call=True)
    def cb_mac_h_add(_a, _c, team, sel):
        if ctx.triggered_id == "mac-h-clear":
            return []
        if not team:
            raise PreventUpdate
        H = _E().hitters.filter(pl.col("team") == team).sort("n_seen", descending=True).head(LINEUP_N)
        return list(dict.fromkeys([*(sel or []), *H["hid"].to_list()]))[:MAX_H]

    @m.app.callback(Output("mac-fall-status", "children"), Output("mac-p", "value", allow_duplicate=True),
                    Output("mac-p", "options", allow_duplicate=True), Output("mac-grade", "value"),
                    Input("mac-fall-upload", "contents"), State("mac-fall-upload", "filename"), State("mac-p", "value"),
                    prevent_initial_call=True)
    def cb_mac_fall(contents, names, sel):
        """Grade the dropped fall files on stuff, put their pitchers in the table (most fall pitches first) and switch the
        grade to stuff, the only one fall pitchers have. The options go out with the value: Dash 4's dropdown drops any
        value its options don't hold yet."""
        if not contents or _missing():
            raise PreventUpdate
        files = [(n, base64.b64decode(cn.split(",", 1)[1])) for n, cn in zip(names, contents)]
        with _LOCK:
            E = _E()
            try:
                info = E.load_fall(files)
            except _mf().FallError as e:
                return html.Div(str(e), style=ERR), no_update, no_update, no_update
            except Exception as e:                 # noqa: BLE001 — shown under the upload box
                return html.Div(f"Couldn't grade the file(s): {type(e).__name__}: {e}", style=ERR), no_update, no_update, no_update
            keep = [p for p in (sel or []) if not str(p).startswith("fall:")]
            new = list(dict.fromkeys([*info["auto"], *keep]))[:MAX_P]
            return _fall_status(m, E), new, _options(E.pitchers, "pid", _p_label, None, None, new, "n26"), "stuff"

    @m.app.callback(Output("mac-fall-status", "children", allow_duplicate=True), Output("mac-p", "value", allow_duplicate=True),
                    Output("mac-fall-upload", "contents"), Input("mac-fall-clear", "n_clicks"), State("mac-p", "value"),
                    prevent_initial_call=True)
    def cb_mac_fall_clear(_n, sel):
        if _missing():
            raise PreventUpdate
        with _LOCK:
            _E().clear_fall()
        return None, [p for p in (sel or []) if not str(p).startswith("fall:")], None

    @m.app.callback(Output("mac-mode", "options"), Output("mac-mode", "value"), Input("mac-grade", "value"), State("mac-mode", "value"))
    def cb_mac_mode_opts(grade, mode):
        """The stuff grade has no pair-specific part (levels only, MAC18)."""
        stuff = grade == "stuff"
        opts = [{"label": v[0], "value": k, "disabled": stuff and k == "pair"} for k, v in MODES.items()]
        return opts, ("matchup" if stuff and mode == "pair" else no_update)

    @m.app.callback(Output("mac-pair", "data"), Input("mac-p", "value"), Input("mac-h", "value"), State("mac-pair", "data"))
    def cb_mac_default_pair(pids, hids, pair):
        """Keep the open pair while it's still in the table; otherwise open the first cell."""
        if not pids or not hids:
            return None
        if pair and pair.get("pid") in pids[:MAX_P] and pair.get("hid") in hids[:MAX_H]:
            raise PreventUpdate
        return {"pid": pids[0], "hid": hids[0]}

    @m.app.callback(Output("mac-pair", "data", allow_duplicate=True), Input({"type": "mac-cell", "p": ALL, "h": ALL}, "n_clicks"),
                    prevent_initial_call=True)
    def cb_mac_click(_clicks):
        trig = ctx.triggered_id
        if not trig or not ctx.triggered or not ctx.triggered[0]["value"]:
            raise PreventUpdate                   # the re-render that recreates the cells
        return {"pid": trig["p"], "hid": trig["h"]}

    @m.app.callback(Output("mac-table", "children"), Input("mac-p", "value"), Input("mac-h", "value"), Input("mac-mode", "value"),
                    Input("mac-grade", "value"), Input("mac-pair", "data"), Input(switch, prop))
    def cb_mac_table(pids, hids, mode, grade, pair, tab):
        if tab != "mac" or _missing():
            raise PreventUpdate
        if not pids or not hids:
            return html.I("Pick at least one pitcher and one hitter.", style={"fontSize": "13px", "color": "#666"})
        grade = grade or "thrown"
        mode = "matchup" if (grade == "stuff" and mode == "pair") else (mode or "matchup")
        with _LOCK:
            E = _E()
            pids = [p for p in pids[:MAX_P] if p in E.pinfo]
            hids = [h for h in hids[:MAX_H] if h in E.hinfo]
            off = [p for p in pids if p in E.fall and grade != "stuff"]
            use = [p for p in pids if p not in off]
            note = (html.Div(f"{len(off)} fall pitcher{'s are' if len(off) > 1 else ' is'} graded on stuff only: switch Grade on to Stuff "
                             "to see them.", style={"fontSize": "12px", "color": "#8a1520", "margin": "2px 0 4px"}) if off else None)
            if not use or not hids:
                if note is not None:
                    return note
                raise PreventUpdate
            return html.Div([note, _table(m, E, E.matchups(use, hids, grade), use, hids, mode, pair, grade)])

    @m.app.callback(Output("mac-detail", "children"), Input("mac-pair", "data"), Input("mac-grade", "value"), Input(switch, prop))
    def cb_mac_detail(pair, grade, tab):
        if tab != "mac" or _missing():
            raise PreventUpdate
        if not pair:
            return None
        with _LOCK:
            E = _E()
            if pair["pid"] not in E.pinfo or pair["hid"] not in E.hinfo:
                return None
            return _detail(m, E, pair["pid"], pair["hid"], grade or "thrown")

    return [panel]
