"""Pitch Lab: grade a hypothetical arsenal with the Stuff+ v1 model. Logic only (no Dash); the layout and
callbacks are in app.py.

The user builds an arsenal from scratch. A primary fastball is required, because every other pitch is also
graded on its velo / HB / IVB differences from it (the model's FB_1 reference). Any other pitch starts at
the D1 average shape for its type. ``grade`` grades whatever numbers are typed in, and ``sensitivity``
sweeps one input at a time across its D1 range. The arsenal lives in the browser session only: the app
keeps it in a session ``dcc.Store``, never on the server.

**Signs.** Inputs use TrackMan's convention, the same numbers the Pitchers tab shows: HorzBreak and RelSide
are + toward a right-hander's arm side, so a left-hander's arm side is −. Grading mirrors them to the
model's convention (+ = arm side for both hands), the same way ``stuff_v1.add_features`` does.

**Acceleration.** The BB model uses spin-induced acceleration, not movement (PR1), and nobody can reason in
ft/s². So the lab rebuilds acceleration from movement. Under constant acceleration, break = ½·a·t², with
the flight time t coming from velo (t = a + b / RelSpeed, fit on D1). Plain physics reads BB Stuff+ about
1.4 points low, because TrackMan's acceleration sits a constant ~0.5 ft/s² (vertical) / ~0.8 (arm side)
above what its break numbers imply. So each axis also gets a slope and an offset fit on D1 breaking balls:
acc = k · break / (6 t²) + c. Tested 2026-10-20 on 100k held-out 2026 BB pitches, graded with real vs.
rebuilt acceleration: Stuff+ r 0.98, mean |Δ| 1.3, bias −0.07. FB and OS use movement directly.

The reference (``build_reference``: the conversion, the D1 average shape per pitch type, and the D1 ranges
behind the warnings) is built once per model from the season data and lives next to the model in
``pipeline_v2/artifacts/`` (git-ignored like it):

    python -m delispice_app.pitch_lab --build        (a few minutes)
"""
from __future__ import annotations

import argparse
import copy
import functools
import json
import math
import time

import numpy as np
import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from backend.models import pitch_groups as pg
from pipeline_v2 import config

from . import report

PRIMARY = "FB1"                                   # the required primary fastball (the model's Fastball_1)
TYPES = pg.TYPES                                  # FS SI CT SL CB CH SP: the model's pitch types
TYPE_GROUP = {PRIMARY: "FB", **pg.TYPE_GROUP}     # cutters are breaking balls
NAME = {PRIMARY: "Fastball", "FS": "Four-Seam", "SI": "Sinker", "CT": "Cutter", "SL": "Slider",
        "CB": "Curveball", "CH": "Changeup", "SP": "Splitter"}
GROUP_PLURAL = {"FB": "fastballs", "BB": "breaking balls", "OS": "offspeed pitches"}
MAX_PITCHES = 8
LAB_VERSION = 1                                   # bump when the session arsenal's shape changes

# The inputs: (field, model column, label, unit, decimals). HB and release side flip with the hand.
FIELDS = [("velo", "RelSpeed", "Velo", "mph", 1), ("ivb", "IVB", "IVB", "in", 1), ("hb", "HB", "HB", "in", 1),
          ("spin", "SpinRate", "Spin", "rpm", 0), ("rel_h", "RelHeight", "Release height", "ft", 2),
          ("rel_s", "RelSideM", "Release side", "ft", 2)]
COL = {f: c for f, c, *_ in FIELDS}
LABEL = {f: lab for f, _, lab, *_ in FIELDS}
UNIT = {f: u for f, _, _, u, _ in FIELDS}
DECIMALS = {f: d for f, *_, d in FIELDS}
MIRRORED = {"hb", "rel_s"}
DIFF_FIELDS = {"velo": "dVelo", "hb": "dHB", "ivb": "dIVB"}      # what the other pitches see of the primary
DIFF_LABEL = {"dVelo": ("Velo vs. fastball", "mph"), "dHB": ("HB vs. fastball", "in"),
              "dIVB": ("IVB vs. fastball", "in")}
STEP = {"velo": 0.1, "ivb": 0.1, "hb": 0.1, "spin": 10, "rel_h": 0.01, "rel_s": 0.01}   # a slider's step
RANGE_Q = (0.01, 0.99)                            # the D1 range a warning is measured against
SLIDER_Q = (0.001, 0.999)                         # what D1 pitchers of the group throw, less the last 0.1% each
                                                  # side (mostly tracking errors)
SLIDER_PAD = 0.20                                 # a slider runs past that by 20% of the field's largest |value|
NON_NEGATIVE = {"velo", "spin", "rel_h"}


def _sign(hand: str) -> float:
    return -1.0 if hand == "Left" else 1.0


def _path():
    from backend.models import stuff_v1
    return config.ARTIFACTS_DIR / f"pitchlab_{stuff_v1.LEVEL}_{stuff_v1.MODEL_VERSION}.json"


@functools.lru_cache(maxsize=1)
def reference() -> dict | None:
    """The built reference, or None if ``--build`` hasn't been run on this machine."""
    p = _path()
    return json.loads(p.read_text()) if p.exists() else None


# ── Movement -> acceleration ─────────────────────────────────────────────────────────────────────
def acc_from_movement(velo, ivb, hb, conv: dict):
    """Velo (mph) and movement (in, + = arm side) -> the model's acc_v / acc_h (ft/s²). Works on
    numbers, numpy arrays and polars expressions alike."""
    t = conv["t"][0] + conv["t"][1] / velo           # flight time, s
    per_in = 1 / (6 * t ** 2)                        # break/12 = ½·a·t²  ->  a = break / (6 t²)
    return conv["v"][0] * ivb * per_in + conv["v"][1], conv["h"][0] * hb * per_in + conv["h"][1]


# ── The session arsenal (a plain dict, so it can live in a dcc.Store) ──────────────────────────────
# {"v": 1, "hand": "Right", "rel_h": 5.6, "rel_s": 1.9, "next": 2,
#  "pitches": [{"uid": "p1", "type": "FB1", "velo": .., "ivb": .., "hb": .., "spin": .., "rel_h": None, "rel_s": None}]}
# Values are in TrackMan's convention for the current hand. A pitch's blank release uses the arsenal's.
def _rounded(field, v):
    """Snapped to the field's slider step."""
    if v is None:
        return None
    v = round(float(v) / STEP[field]) * STEP[field]
    return int(round(v)) if DECIMALS[field] == 0 else round(v, DECIMALS[field])


def slider_range(ref: dict, ptype: str, f: str, hand: str) -> tuple:
    """A slider's (min, max) in display units: the D1 limits of the pitch's group, each end pushed out by
    SLIDER_PAD x the larger of |min| and |max| (measured arm-side, so both hands get the same reach), out
    to whole steps. Velo, spin and release height stop at 0."""
    lo, hi = ref["limits"][TYPE_GROUP[ptype]][COL[f]]
    pad = SLIDER_PAD * max(abs(lo), abs(hi))
    lo, hi = lo - pad, hi + pad
    if f in NON_NEGATIVE:
        lo = max(lo, 0.0)
    if f in MIRRORED:
        lo, hi = sorted((_sign(hand) * lo, _sign(hand) * hi))
    return (_rounded(f, math.floor(lo / STEP[f]) * STEP[f]), _rounded(f, math.ceil(hi / STEP[f]) * STEP[f]))


def d1_average(ref: dict, ptype: str) -> dict:
    """The D1 average shape a new pitch starts at. The primary fastball starts as the average four-seam:
    the average of all primary fastballs blends four-seamers and sinkers into a shape between the two."""
    return ref["defaults"]["FS" if ptype == PRIMARY else ptype]


def new_pitch(ref: dict, ptype: str, uid: str, hand: str) -> dict:
    d, s = d1_average(ref, ptype), _sign(hand)
    p = {"uid": uid, "type": ptype, "rel_h": None, "rel_s": None}
    for f in ("velo", "ivb", "hb", "spin"):
        p[f] = _rounded(f, (s if f in MIRRORED else 1) * d[COL[f]])
    return p


def default_lab(ref: dict, hand: str = "Right") -> dict:
    rel, s = ref["release"], _sign(hand)
    return {"v": LAB_VERSION, "hand": hand, "rel_h": _rounded("rel_h", rel["RelHeight"]),
            "rel_s": _rounded("rel_s", s * rel["RelSideM"]), "next": 2,
            "pitches": [new_pitch(ref, PRIMARY, "p1", hand)]}


def is_valid(lab) -> bool:
    """A stored arsenal this code can read (an older shape, or a tampered one, starts over)."""
    return (isinstance(lab, dict) and lab.get("v") == LAB_VERSION and lab.get("hand") in ("Right", "Left")
            and isinstance(lab.get("pitches"), list) and lab["pitches"][:1]
            and lab["pitches"][0].get("type") == PRIMARY
            and all(p.get("type") in TYPE_GROUP for p in lab["pitches"]))


def fit_to_sliders(lab: dict, ref: dict) -> dict:
    """Every value on its slider: on the step and inside the range. An arsenal stored before the sliders
    can sit off them, or have a blank, which becomes the D1 average (or, for a release, the arm slot)."""
    out, s = copy.deepcopy(lab), _sign(lab["hand"])

    def fit(v, ptype, f, fallback):
        lo, hi = slider_range(ref, ptype, f, out["hand"])
        return _rounded(f, min(max(fallback if v is None else v, lo), hi))

    for f in ("rel_h", "rel_s"):
        out[f] = fit(out.get(f), PRIMARY, f, (s if f in MIRRORED else 1) * ref["release"][COL[f]])
    for p in out["pitches"]:
        avg = d1_average(ref, p["type"])
        for f in ("velo", "ivb", "hb", "spin"):
            p[f] = fit(p.get(f), p["type"], f, (s if f in MIRRORED else 1) * avg[COL[f]])
        if own_release(p):
            for f in ("rel_h", "rel_s"):
                p[f] = fit(p.get(f), p["type"], f, out[f])
    return out


def own_release(p: dict) -> bool:
    return p.get("rel_h") is not None or p.get("rel_s") is not None


def set_own_release(lab: dict, uid: str, on: bool) -> dict:
    """Give a pitch its own release point (starting at the arm slot), or send it back to the arm slot."""
    out = copy.deepcopy(lab)
    for p in out["pitches"]:
        if p["uid"] == uid:
            p["rel_h"], p["rel_s"] = (out["rel_h"], out["rel_s"]) if on else (None, None)
    return out


def add_pitch(lab: dict, ptype: str, ref: dict) -> dict:
    if len(lab["pitches"]) >= MAX_PITCHES or ptype not in TYPES:
        return lab
    out = copy.deepcopy(lab)
    out["pitches"].append(new_pitch(ref, ptype, f"p{out['next']}", out["hand"]))
    out["next"] += 1
    return out


def remove_pitch(lab: dict, uid: str) -> dict:
    out = copy.deepcopy(lab)
    out["pitches"] = [p for p in out["pitches"] if p["uid"] != uid or p["type"] == PRIMARY]
    return out


def mirror(lab: dict, hand: str) -> dict:
    """The same arsenal from the other arm: HB and release side flip sign."""
    if hand == lab["hand"]:
        return lab
    out = copy.deepcopy(lab)
    out["hand"] = hand
    flip = lambda v: None if v is None else -v
    out["rel_s"] = flip(out["rel_s"])
    for p in out["pitches"]:
        p["hb"], p["rel_s"] = flip(p["hb"]), flip(p["rel_s"])
    return out


def overlay(lab: dict, values: dict) -> dict:
    """Fold the form's values in. ``values``: (uid, field) -> value, with uid "arsenal" for the arm slot."""
    out = copy.deepcopy(lab)
    for (uid, f), v in values.items():
        if uid == "arsenal":
            out[f] = v
        else:
            for p in out["pitches"]:
                if p["uid"] == uid:
                    p[f] = v
    return out


def names(lab: dict) -> dict:
    """uid -> display name. A repeated type is numbered: Slider, Slider 2."""
    seen, out = {}, {}
    for p in lab["pitches"]:
        seen[p["type"]] = seen.get(p["type"], 0) + 1
        out[p["uid"]] = NAME[p["type"]] + (f" {seen[p['type']]}" if seen[p["type"]] > 1 else "")
    return out


def colors(lab: dict) -> dict:
    """uid -> chart colour: the Pitchers tab's colour for the type, a spare colour for a repeat."""
    used, out, spare = set(), {}, iter(c for c in report.CLUSTER_COLORS + ["#7f7f7f"] * MAX_PITCHES)
    for p in lab["pitches"]:
        c = report.PITCH_COLORS.get(NAME[p["type"]] if p["type"] != PRIMARY else "Four-Seam", "#8c8c8c")
        while c in used:
            c = next(spare)
        used.add(c)
        out[p["uid"]] = c
    return out


def effective(lab: dict, p: dict, f: str):
    """A pitch's value for ``f``, with a blank release falling back to the arsenal's."""
    v = p.get(f)
    return lab.get(f) if v is None and f in ("rel_h", "rel_s") else v


def incomplete(lab: dict) -> list[str]:
    """uids of pitches missing a number (they can't be graded)."""
    return [p["uid"] for p in lab["pitches"] if any(effective(lab, p, f) is None for f in COL)]


# ── Grading ──────────────────────────────────────────────────────────────────────────────────────
def _frame(labs: list[dict], ref: dict) -> pl.DataFrame:
    """Arsenals -> one model-space row per complete pitch (``scenario`` = the arsenal's position in
    ``labs``). A scenario whose primary fastball is incomplete has nothing to grade."""
    rows = []
    for i, lab in enumerate(labs):
        s = _sign(lab["hand"])
        for p in lab["pitches"]:
            v = {f: effective(lab, p, f) for f in COL}
            if any(x is None for x in v.values()):
                continue
            rows.append({"scenario": i, "uid": p["uid"], "type": p["type"], "group": TYPE_GROUP[p["type"]],
                         "primary": p["type"] == PRIMARY, "PitcherThrows": lab["hand"],
                         **{COL[f]: (s if f in MIRRORED else 1.0) * float(x) for f, x in v.items()}})
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows)
    fb = df.filter(pl.col("primary")).select("scenario", *[pl.col(c).alias(f"fb_{c}") for c in ("RelSpeed", "HB", "IVB")])
    acc_v, acc_h = acc_from_movement(pl.col("RelSpeed"), pl.col("IVB"), pl.col("HB"), ref["conversion"])
    return (df.join(fb, on="scenario")
              .with_columns(dVelo=pl.col("RelSpeed") - pl.col("fb_RelSpeed"), dHB=pl.col("HB") - pl.col("fb_HB"),
                            dIVB=pl.col("IVB") - pl.col("fb_IVB"), acc_v=acc_v, acc_h=acc_h,
                            PitchUID=pl.format("{}|{}", "scenario", "uid"))
              .drop("fb_RelSpeed", "fb_HB", "fb_IVB"))


def _graded(fr: pl.DataFrame, model) -> pl.DataFrame:
    return fr.join(model.grade(fr), on="PitchUID", how="left")


def grade(lab: dict, model, ref: dict) -> pl.DataFrame:
    """One row per complete pitch: uid, group, the model-space inputs and Stuff+ / Whiff+ / Foul+ /
    Contact+. Empty when the primary fastball is incomplete (every grade needs it)."""
    fr = _frame([lab], ref)
    return _graded(fr, model) if fr.height else fr


GRADES = {"stuff": "Stuff+", "whiff_plus": "Whiff+", "foul_plus": "Foul+", "contact_plus": "Contact+"}


def grade_table(lab: dict, graded: pl.DataFrame) -> pl.DataFrame:
    """The grades table, in arsenal order: Pitch, Group, Stuff+, Whiff+, Foul+, Contact+."""
    nm, by_uid = names(lab), {r["uid"]: r for r in graded.iter_rows(named=True)}
    f0 = lambda v: "" if v is None else f"{v:.0f}"
    return pl.DataFrame([{"Pitch": nm[p["uid"]], "Group": pg.GROUP_NAME[TYPE_GROUP[p["type"]]],
                          **{label: f0(by_uid[p["uid"]][c]) for c, label in GRADES.items()}}
                         for p in lab["pitches"] if p["uid"] in by_uid])


def warnings(lab: dict, graded: pl.DataFrame, ref: dict) -> list[tuple[str, str]]:
    """(uid, message) for every input outside the D1 range of its group (1st-99th percentile). The
    differentials vs. the primary are checked too: they're inputs the trees see."""
    if graded.height == 0:
        return []
    s, out, nm = _sign(lab["hand"]), [], names(lab)
    fmt = lambda v, d: f"{v:,.{d}f}"
    for r in graded.iter_rows(named=True):
        rng = ref["ranges"][r["group"]]
        checks = [(f, COL[f], LABEL[f], UNIT[f], DECIMALS[f], f in MIRRORED) for f in COL]
        if not r["primary"]:
            checks += [(c, c, *DIFF_LABEL[c], 1, c == "dHB") for c in DIFF_FIELDS.values()]
        for _, col, label, unit, dec, flips in checks:
            lo, hi = rng[col]
            v = r[col]
            if lo <= v <= hi:
                continue
            k = s if flips else 1.0
            a, b = sorted((k * lo, k * hi))
            out.append((r["uid"], f"{nm[r['uid']]}: {label} {fmt(k * v, dec)} {unit} is outside the range of "
                                  f"98% of D1 {GROUP_PLURAL[r['group']]} ({fmt(a, dec)} to {fmt(b, dec)} {unit})"))
    return out


def sensitivity(lab: dict, uid: str, model, ref: dict, n: int = 25) -> tuple[pl.DataFrame, dict]:
    """Sweep each input of pitch ``uid`` across its group's D1 range (plus its current value), one at
    a time, everything else held. Returns (field, x, uid, stuff) rows and field -> (lo, hi) of the D1
    range in display units. For the primary fastball, velo / HB / IVB also move every other pitch
    (their differentials), so those sweeps carry the whole arsenal."""
    target = next((p for p in lab["pitches"] if p["uid"] == uid), None)
    if target is None or uid in incomplete(lab) or lab["pitches"][0]["uid"] in incomplete(lab):
        return pl.DataFrame(), {}
    s, rng = _sign(lab["hand"]), ref["ranges"][TYPE_GROUP[target["type"]]]
    primary = target["type"] == PRIMARY
    labs, keys, bounds = [], [], {}
    for f in COL:
        lo, hi = rng[COL[f]]
        if f in MIRRORED:
            lo, hi = sorted((s * lo, s * hi))
        cur = float(effective(lab, target, f))
        bounds[f] = (lo, hi)
        xs = sorted(set(np.round(np.linspace(min(lo, cur), max(hi, cur), n), 3).tolist()) | {cur})
        whole = primary and f in DIFF_FIELDS
        keep = lab["pitches"] if whole else [p for p in lab["pitches"] if p["type"] == PRIMARY or p["uid"] == uid]
        for x in xs:
            alt = dict(lab, pitches=[dict(p, **({f: x} if p["uid"] == uid else {})) for p in keep])
            labs.append(alt)
            keys.append((f, x, whole))
    fr = _frame(labs, ref)
    if fr.height == 0:
        return pl.DataFrame(), bounds
    k = pl.DataFrame({"scenario": list(range(len(keys))), "field": [a for a, _, _ in keys],
                      "x": [b for _, b, _ in keys], "whole": [c for *_, c in keys]})
    g = _graded(fr, model).join(k, on="scenario")
    g = g.filter((pl.col("uid") == uid) | pl.col("whole"))
    return g.select("field", "x", "uid", "stuff").sort("field", "uid", "x"), bounds


# ── Figures ──────────────────────────────────────────────────────────────────────────────────────
def empty_fig(msg: str = "", width=460, height=450) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(width=width, height=height, template="plotly_white", xaxis=dict(visible=False),
                      yaxis=dict(visible=False), margin=dict(l=10, r=10, t=30, b=10),
                      annotations=[dict(text=msg, showarrow=False, font=dict(size=13, color="#888"))] if msg else [])
    return fig


def movement_fig(lab: dict, graded: pl.DataFrame, ref: dict) -> go.Figure:
    """The arsenal on the Pitchers tab's movement chart (TrackMan HB x IVB, ±30 in), each pitch labelled
    with its Stuff+, over hollow markers at the D1 average shape of each type in the arsenal."""
    s, nm, col = _sign(lab["hand"]), names(lab), colors(lab)
    fig = go.Figure()
    shown = list(dict.fromkeys(p["type"] for p in lab["pitches"]))
    avg = [(t, d1_average(ref, t)) for t in shown]
    fig.add_trace(go.Scatter(x=[s * d["HB"] for _, d in avg], y=[d["IVB"] for _, d in avg], mode="markers",
        name="D1 average", marker=dict(size=13, color="rgba(0,0,0,0)", line=dict(color="#aaa", width=1.5)),
        customdata=[[NAME[t], d["RelSpeed"], d["SpinRate"]] for t, d in avg],
        hovertemplate="D1 average %{customdata[0]}<br>%{customdata[1]:.1f} mph · %{customdata[2]:,.0f} rpm"
                      "<br>IVB %{y:.1f} · HB %{x:.1f} in<extra></extra>"))
    grades = {r["uid"]: r for r in graded.iter_rows(named=True)} if graded.height else {}
    for p in lab["pitches"]:
        if p.get("hb") is None or p.get("ivb") is None:
            continue
        g = grades.get(p["uid"], {}).get("stuff")
        label = nm[p["uid"]] + (f" {g:.0f}" if g is not None else "")
        fig.add_trace(go.Scatter(x=[p["hb"]], y=[p["ivb"]], mode="markers+text", name=nm[p["uid"]],
            text=[label], textposition="top center", textfont=dict(size=11),
            marker=dict(size=15, color=col[p["uid"]], line=dict(color="#fff", width=1)),
            customdata=[[nm[p["uid"]], p.get("velo"), p.get("spin"), "—" if g is None else f"{g:.0f}"]],
            hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]} mph · %{customdata[2]} rpm"
                          "<br>IVB %{y:.1f} · HB %{x:.1f} in<br>Stuff+ %{customdata[3]}<extra></extra>"))
    lim = report.MOVE_LIM
    fig.update_layout(width=460, height=470, template="plotly_white", showlegend=False,
        title=dict(text="Movement (pitcher's view)", font=dict(size=13)), margin=dict(l=10, r=10, t=36, b=10),
        xaxis=dict(range=[-lim, lim], title="HB (in)", zeroline=True, zerolinecolor="#ccc"),
        yaxis=dict(range=[-lim, lim], title="IVB (in)", zeroline=True, zerolinecolor="#ccc",
                   scaleanchor="x", scaleratio=1))
    return fig


def sensitivity_fig(lab: dict, uid: str, sens: pl.DataFrame, bounds: dict) -> go.Figure:
    """One panel per input: the pitch's Stuff+ as that input moves (solid), the current value (dot),
    and grey outside the D1 range. For the primary fastball's velo / HB / IVB, the other pitches' Stuff+
    are dashed: those inputs move every grade in the arsenal."""
    nm, col = names(lab), colors(lab)
    target = next(p for p in lab["pitches"] if p["uid"] == uid)
    fig = make_subplots(rows=2, cols=3, horizontal_spacing=0.07, vertical_spacing=0.16,
                        subplot_titles=[f"{LABEL[f]} ({UNIT[f]})" for f in COL])
    for i, f in enumerate(COL):
        r, c = i // 3 + 1, i % 3 + 1
        d = sens.filter(pl.col("field") == f)
        if d.height == 0:
            continue
        others = [u for u in d["uid"].unique(maintain_order=True).to_list() if u != uid]
        for u in [uid, *others]:
            du = d.filter(pl.col("uid") == u)
            fig.add_trace(go.Scatter(x=du["x"].to_list(), y=du["stuff"].to_list(), mode="lines",
                name=nm[u], legendgroup=u, showlegend=(f == "velo" and bool(others)) or (i == 0 and u == uid),
                line=dict(color=col[u], width=3 if u == uid else 1.5, dash="solid" if u == uid else "dash"),
                hovertemplate=f"{nm[u]}<br>{LABEL[f]} %{{x}}<br>Stuff+ %{{y:.0f}}<extra></extra>"),
                row=r, col=c)
        cur = float(effective(lab, target, f))
        y = d.filter((pl.col("uid") == uid) & (pl.col("x") == cur))["stuff"]
        if y.len():
            fig.add_trace(go.Scatter(x=[cur], y=[y[0]], mode="markers", showlegend=False,
                marker=dict(size=10, color=col[uid], line=dict(color="#fff", width=1.5)),
                hovertemplate=f"Now: {LABEL[f]} %{{x}}<br>Stuff+ %{{y:.0f}}<extra></extra>"), row=r, col=c)
        # after the traces: plotly skips a shape on a subplot that has no data yet
        (x0, x1), (lo, hi) = (d["x"].min(), d["x"].max()), bounds[f]
        for a, b in ((x0, lo), (hi, x1)):
            if b - a > 1e-3:                 # the grid is rounded to 0.001, so its ends can miss the range by less
                fig.add_vrect(x0=a, x1=b, fillcolor="#e8e8e8", line_width=0, layer="below", row=r, col=c)
    many = sens["uid"].n_unique() > 1
    fig.update_yaxes(title_text="Stuff+", col=1)
    fig.update_layout(width=960, height=580 if many else 550, template="plotly_white", showlegend=many,
                      margin=dict(l=10, r=10, t=70 if many else 40, b=10), hovermode="closest",
                      legend=dict(orientation="h", yanchor="bottom", y=1.08, x=0, font=dict(size=11)))
    fig.update_annotations(font_size=12)
    return fig


# ── Building the reference ───────────────────────────────────────────────────────────────────────
def _slim_season(year: str, keep_every: int) -> pl.DataFrame:
    """One season's gradeable D1 pitches in model space (labels from the score table), 1 in
    ``keep_every`` kept. The FB_1 reference is computed on whole outings before the sample."""
    from backend.models import stuff_store, stuff_v1
    raw = stuff_v1.load_pitches([year], with_outcomes=False)
    lab = stuff_store.stuff_lookup(year)
    if lab is None:
        raise FileNotFoundError(f"no Stuff+ score table for {year}; run python -m backend.models.stuff_store --score-only")
    raw = raw.join(lab.select("PitchUID", "group", "retag", "type"), on="PitchUID", how="inner")
    raw, _ = stuff_v1.merge_pitcher_ids(raw)
    P = stuff_v1.add_features(raw).filter(pl.col("gradeable"))
    return (P.filter(pl.col("PitchUID").hash(42) % keep_every == 0)
             .select("PitchUID", "PitcherThrows", "HorzBreak", "group", "retag", "type",
                     *stuff_v1.SHAPE_ALL, *stuff_v1.DIFFS))


def _ols(X: np.ndarray, y: np.ndarray) -> list[float]:
    return np.linalg.lstsq(X, y, rcond=None)[0].tolist()


def fit_conversion(P: pl.DataFrame) -> dict:
    """Flight time from velo on every pitch that moves (implied t = sqrt(2·break / a)), then per axis
    acc = k · break / (6 t²) + c on breaking balls, the only group whose model reads acceleration."""
    mv = P.filter((pl.col("acc_v") ** 2 + pl.col("acc_h") ** 2).sqrt() > 8).with_columns(
        t=(2 * (pl.col("IVB") ** 2 + pl.col("HB") ** 2).sqrt() / 12
           / (pl.col("acc_v") ** 2 + pl.col("acc_h") ** 2).sqrt()).sqrt())
    v = mv["RelSpeed"].to_numpy()
    t = _ols(np.c_[np.ones(len(v)), 1 / v], mv["t"].to_numpy())
    bb = P.filter(pl.col("group") == "BB")
    per_in = 1 / (6 * (t[0] + t[1] / bb["RelSpeed"].to_numpy()) ** 2)
    one = np.ones(bb.height)
    kv = _ols(np.c_[bb["IVB"].to_numpy() * per_in, one], bb["acc_v"].to_numpy())
    kh = _ols(np.c_[bb["HB"].to_numpy() * per_in, one], bb["acc_h"].to_numpy())
    return {"t": t, "v": kv, "h": kh, "n_t": mv.height, "n_bb": bb.height}


def check_conversion(P: pl.DataFrame, conv: dict, model, n: int = 50_000) -> dict:
    """Grade real D1 breaking balls twice, real vs. rebuilt acceleration: how far does Stuff+ move?"""
    bb = P.filter(pl.col("group") == "BB")
    bb = bb.sample(min(n, bb.height), seed=1)
    av, ah = acc_from_movement(pl.col("RelSpeed"), pl.col("IVB"), pl.col("HB"), conv)
    real = model.grade(bb).select("PitchUID", pl.col("stuff").alias("real"))
    rebuilt = model.grade(bb.with_columns(acc_v=av, acc_h=ah)).select("PitchUID", pl.col("stuff").alias("rebuilt"))
    j = real.join(rebuilt, on="PitchUID").with_columns(d=pl.col("rebuilt") - pl.col("real"))
    r, mad, bias = j.select(pl.corr("real", "rebuilt"), pl.col("d").abs().mean().alias("m"), pl.col("d").mean().alias("b")).row(0)
    return {"n": j.height, "r": round(r, 4), "mean_abs": round(mad, 2), "bias": round(bias, 2)}


def build_reference(years: list[str] | None = None, keep_every: int = 4) -> dict:
    from backend.models import stuff_store, stuff_v1
    t0 = time.time()
    model = stuff_store.load()
    if model is None:
        raise FileNotFoundError("no Stuff+ model; train it first: python -m backend.models.stuff_store --train")
    years = years or model.meta.get("years") or stuff_v1.available_years()
    parts = []
    for y in years:
        parts.append(_slim_season(y, keep_every))
        print(f"  {y}: {parts[-1].height:,} pitches kept ({time.time() - t0:.0f}s)", flush=True)
    P = pl.concat(parts)
    shape = [COL[f] for f in COL]

    def means(d):
        return {**{c: float(d[c].mean()) for c in shape}, "n": d.height}

    defaults = {t: means(P.filter(pl.col("type") == t)) for t in TYPES}
    ranges = {g: {c: [float(P.filter(pl.col("group") == g)[c].quantile(q)) for q in RANGE_Q]
                  for c in shape + list(stuff_v1.DIFFS)} for g in stuff_v1.GROUPS}
    limits = {g: {c: [float(P.filter(pl.col("group") == g)[c].quantile(q)) for q in SLIDER_Q] for c in shape}
              for g in stuff_v1.GROUPS}
    conv = fit_conversion(P)
    print(f"  conversion fit: {conv} ({time.time() - t0:.0f}s)", flush=True)
    check = check_conversion(P, conv, model)
    rhp = P.filter(pl.col("retag").is_in(["Fastball_1", "Fastball"]), pl.col("PitcherThrows") == "Right")
    ref = {"version": LAB_VERSION, "model": f"stuff_{stuff_v1.LEVEL}_{stuff_v1.MODEL_VERSION}",
           "built": time.strftime("%Y-%m-%d %H:%M"), "years": years, "keep_every": keep_every, "n": P.height,
           "conversion": conv, "check": check, "defaults": defaults,
           "release": {c: defaults["FS"][c] for c in ("RelHeight", "RelSideM")}, "ranges": ranges, "limits": limits,
           # sign check: a right-hander's primary fastball should have + TrackMan HorzBreak (arm side)
           "rhp_fb_horzbreak": float(rhp["HorzBreak"].mean())}
    _path().write_text(json.dumps(ref, indent=1))
    reference.cache_clear()
    print(f"saved {_path().name}: BB Stuff+ with rebuilt acceleration {check} ({time.time() - t0:.0f}s)")
    return ref


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the Pitch Lab reference (conversion, D1 averages, ranges).")
    p.add_argument("--build", action="store_true", help="build pitchlab_D1_v1.json from the season data")
    p.add_argument("--years", nargs="*", default=None, help="seasons (default: the model's training seasons)")
    p.add_argument("--keep-every", type=int, default=4, help="keep 1 pitch in this many (memory)")
    args = p.parse_args(argv)
    if not args.build:
        p.error("pass --build")
    build_reference(args.years, args.keep_every)


if __name__ == "__main__":
    main()
