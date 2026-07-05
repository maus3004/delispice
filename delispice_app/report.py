"""Report builders + Plotly figures for delispice_app.

Pure compute + Plotly only — no Dash, no DuckDB imports. Ported verbatim (logic-wise) from
``interactive_pitcher.ipynb`` Cell 2. The only change is ``go.FigureWidget`` -> ``go.Figure`` so
the charts render inside Dash; every stat, colour, axis range and hover template is unchanged.

The heatmap keeps its on-chart stat dropdown (Plotly ``updatemenus``). Batter/Count splits update
the four stat surfaces in place (a Dash ``Patch`` in app.py feeds ``heat_surface_data`` back in),
which mirrors the notebook's ``FigureWidget.batch_update``.
"""
from __future__ import annotations

import math

import numpy as np
import polars as pl
import plotly.graph_objects as go
from scipy.stats import gaussian_kde
from scipy.ndimage import gaussian_filter

# ── Vocabulary (unchanged from the notebook) ────────────────────────────────────────────────────
STRIKE_CALLS = ["StrikeCalled", "StrikeSwinging", "FoulBallNotFieldable", "FoulBallFieldable", "InPlay"]
SWING_CALLS  = ["StrikeSwinging", "FoulBallNotFieldable", "FoulBallFieldable", "InPlay"]
HIT_RESULTS  = ["Single", "Double", "Triple", "HomeRun"]
HARD_HIT_MPH = 95
PITCH_NAMES  = {"FourSeamFastBall": "Four-Seam", "TwoSeamFastBall": "Two-Seam", "ChangeUp": "Changeup"}
NON_PITCHES  = ["Undefined", "Other"]
PITCH_COLORS = {"Four-Seam": "#d62728", "Fastball": "#d62728", "Two-Seam": "#ff7f0e", "Sinker": "#ff7f0e",
                "Cutter": "#8c564b", "Slider": "#e6b800", "Sweeper": "#bcbd22", "Curveball": "#1f77b4",
                "Changeup": "#2ca02c", "Splitter": "#9467bd", "Knuckleball": "#7f7f7f"}
# Distinct colours for AutoCluster labels (Cluster 0, Cluster 1, …) on the movement/velocity charts.
CLUSTER_COLORS = ["#d62728", "#1f77b4", "#2ca02c", "#e6b800", "#9467bd", "#ff7f0e", "#17becf"]


def num(x, d=2):
    return f"{x:.{d}f}" if x is not None else ""


def pct(x):
    return f"{x:.0%}" if x is not None else ""


def spin_clock(axis_deg):
    """Average spin axis (degrees) -> tilt clock. In this TrackMan data a pure-backspin fastball
    sits at ~180°, which reads 12:00; a RHP four-seam (~215°) ≈ 1:00, a LHP (~140°) ≈ 11:00.
    (0°/360° is topspin -> 6:00.) Rounded to the nearest 15 minutes."""
    if axis_deg is None:
        return ""
    minutes = round(((axis_deg + 180) % 360) / 360 * 720 / 15) * 15 % 720
    h, m = divmod(int(minutes), 60)
    return f"{h or 12}:{m:02d}"


# ── Summary + Arsenal tables ─────────────────────────────────────────────────────────────────────
def build_summary(df: pl.DataFrame) -> pl.DataFrame:
    n = df.height
    bf = df.filter(pl.col("PitchofPA") == 1).height
    so = df.filter(pl.col("KorBB") == "Strikeout").height
    bb = df.filter(pl.col("KorBB") == "Walk").height
    hbp = df.filter(pl.col("PitchCall") == "HitByPitch").height
    hits = df.filter(pl.col("PlayResult").is_in(HIT_RESULTS)).height
    outs = int(df["OutsOnPlay"].sum() or 0) + so
    retired = so + df.filter(pl.col("PlayResult").is_in(["Out", "Sacrifice"])).height
    return pl.DataFrame([{
        "IP": f"{outs // 3}.{outs % 3}", "Pitches Thrown": n,
        "Pitches per PA": num(n / bf) if bf else "", "Batters Faced": bf, "Batters Retired": retired,
        "Walks + HBP": bb + hbp, "Strikeouts": so, "Hits": hits,
        "Rel Height": num(df["RelHeight"].mean()), "Rel Side": num(df["RelSide"].mean()),
        "Extension": num(df["Extension"].mean()),
        "K%": pct(so / bf) if bf else "", "BB%": pct(bb / bf) if bf else "",
    }])


def build_arsenal(df: pl.DataFrame) -> pl.DataFrame:
    n = df.height
    agg = (df.filter(pl.col("TaggedPitchType").is_not_null() & ~pl.col("TaggedPitchType").is_in(NON_PITCHES))
             .group_by("TaggedPitchType").agg(
                 pl.len().alias("count"),
                 pl.col("RelSpeed").mean().alias("velo"), pl.col("RelSpeed").max().alias("velo_max"),
                 pl.col("SpinRate").mean().alias("spin"),
                 pl.col("PitchCall").is_in(STRIKE_CALLS).mean().alias("strike"),
                 (pl.col("PitchCall") == "StrikeSwinging").sum().alias("whiffs"),
                 pl.col("PitchCall").is_in(SWING_CALLS).sum().alias("swings"),
                 pl.col("InducedVertBreak").mean().alias("vb"), pl.col("HorzBreak").mean().alias("hb"),
                 # circular mean of the spin axis (robust to the 0/360 wrap), later shown as a clock
                 pl.col("SpinAxis").radians().sin().mean().alias("ax_sin"),
                 pl.col("SpinAxis").radians().cos().mean().alias("ax_cos"),
                 pl.col("RelHeight").mean().alias("rh"),
                 pl.col("RelSide").mean().alias("rs"), pl.col("Extension").mean().alias("ext"),
                 pl.col("ExitSpeed").mean().alias("ev"),
                 (pl.col("ExitSpeed") >= HARD_HIT_MPH).filter(pl.col("ExitSpeed").is_not_null()).mean().alias("hh"),
             ).sort("count", descending=True))
    rows = []
    for r in agg.iter_rows(named=True):
        whiff = r["whiffs"] / r["swings"] if r["swings"] else None
        axis = (math.degrees(math.atan2(r["ax_sin"], r["ax_cos"])) % 360
                if r["ax_sin"] is not None and r["ax_cos"] is not None else None)
        rows.append({"Pitch": PITCH_NAMES.get(r["TaggedPitchType"], r["TaggedPitchType"]),
                     "Pitch Count": r["count"], "Pitch Usage %": pct(r["count"] / n) if n else "",
                     "Avg Velo": num(r["velo"], 1), "Max Velo": num(r["velo_max"], 1), "Avg Spin": num(r["spin"], 0),
                     "Strike %": pct(r["strike"]), "WHIFF %": pct(whiff), "Vert Break": num(r["vb"]),
                     "Horz Break": num(r["hb"]), "Tilt": spin_clock(axis), "Rel Height": num(r["rh"]),
                     "Rel Side": num(r["rs"]), "Extension": num(r["ext"]), "Hard Hit %": pct(r["hh"]),
                     "Avg EV": num(r["ev"])})
    return pl.DataFrame(rows)


# ── Batter report (starting scaffold — tables/charts to be extended) ──────────────────────────────
def _avg3(v):
    """Rate stat formatted baseball-style: 0.312 -> ".312", 1.050 -> "1.050"."""
    if v is None:
        return ""
    s = f"{v:.3f}"
    return s[1:] if s.startswith("0.") else s


def bats_of(df: pl.DataFrame) -> str:
    """A batter's handedness from BatterSide: L / R / S (switch)."""
    s = df["BatterSide"].drop_nulls()
    if len(s) == 0:
        return ""
    counts = {r["BatterSide"]: r["count"] for r in s.value_counts().iter_rows(named=True)}
    lft, rgt = counts.get("Left", 0), counts.get("Right", 0)
    tot = lft + rgt
    if tot and min(lft, rgt) / tot >= 0.2:
        return "S"
    return "L" if lft >= rgt else "R"


def build_batter_summary(df: pl.DataFrame) -> pl.DataFrame:
    cnt = lambda e: df.filter(e).height
    pa = cnt(pl.col("PitchofPA") == 1)
    singles = cnt(pl.col("PlayResult") == "Single")
    doubles = cnt(pl.col("PlayResult") == "Double")
    triples = cnt(pl.col("PlayResult") == "Triple")
    hr = cnt(pl.col("PlayResult") == "HomeRun")
    h = singles + doubles + triples + hr
    bb = cnt(pl.col("KorBB") == "Walk")
    so = cnt(pl.col("KorBB") == "Strikeout")
    hbp = cnt(pl.col("PitchCall") == "HitByPitch")
    sac = cnt(pl.col("PlayResult") == "Sacrifice")
    ab = pa - bb - hbp - sac
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    avg = h / ab if ab else None
    obp_den = ab + bb + hbp + sac
    obp = (h + bb + hbp) / obp_den if obp_den else None
    slg = tb / ab if ab else None
    ops = (obp + slg) if (obp is not None and slg is not None) else None
    bip = df.filter((pl.col("PitchCall") == "InPlay") & pl.col("ExitSpeed").is_not_null())
    ev = bip["ExitSpeed"].mean() if bip.height else None
    hh = (bip["ExitSpeed"] >= HARD_HIT_MPH).mean() if bip.height else None
    la = df.filter(pl.col("PitchCall") == "InPlay")["Angle"].mean()
    return pl.DataFrame([{
        "PA": pa, "AB": ab, "H": h, "2B": doubles, "3B": triples, "HR": hr,
        "BB": bb, "SO": so, "HBP": hbp,
        "AVG": _avg3(avg), "OBP": _avg3(obp), "SLG": _avg3(slg), "OPS": _avg3(ops),
        "K%": pct(so / pa) if pa else "", "BB%": pct(bb / pa) if pa else "",
        "Avg EV": num(ev, 1), "Hard Hit %": pct(hh), "Avg LA": num(la, 1),
    }])


# Pitch families for the batter table (raw TaggedPitchType -> family; anything else -> "Others").
PITCH_FAMILY = {
    "Fastball": "Fastballs", "FourSeamFastBall": "Fastballs", "Sinker": "Fastballs",
    "TwoSeamFastBall": "Fastballs", "OneSeamFastBall": "Fastballs",
    "Curveball": "Breaking", "Slider": "Breaking", "Cutter": "Breaking",
    "Slurve": "Breaking", "Sweeper": "Breaking",
    "ChangeUp": "Offspeed", "Splitter": "Offspeed",
}
FAMILY_ORDER = ["Fastballs", "Breaking", "Offspeed", "Others"]
# Raw tag -> display sub-type (OneSeamFastBall folds into "Fastball", per request).
SUB_DISPLAY = {"FourSeamFastBall": "Four-Seam", "TwoSeamFastBall": "Two-Seam",
               "OneSeamFastBall": "Fastball", "ChangeUp": "Changeup"}
BATTER_TABLE_COLS = ["Pitch", "Pitches Seen", "Pitch Seen %", "Swing %", "Contact %",
                     "Good Decision %", "Whiff %", "I-Zone Swing %", "I-Zone Whiff %", "Chase %",
                     "Ground Ball %", "Fly Ball %", "Line Drive %", "Pop Up %", "Hard Hit %", "Avg EV"]
_COMPONENT_KEYS = ("count", "swings", "whiffs", "loc_n", "good", "iz_n", "iz_sw", "iz_whiff",
                   "oz_n", "oz_sw", "gb", "fb", "ld", "pu", "ev_n", "hh", "ev_sum")


def _pitch_components(df: pl.DataFrame) -> list[dict]:
    """Per-(family, sub-type) raw counts/sums used to compute the batter pitch table.

    Good Decision = swing at an in-zone pitch OR take an out-of-zone one (over pitches with a location).
    I-Swing% = swings / in-zone pitches; O-Swing% = swings / out-of-zone pitches (chase).
    """
    swing = pl.col("PitchCall").is_in(SWING_CALLS)
    whiff = pl.col("PitchCall") == "StrikeSwinging"
    loc = pl.col("PlateLocSide").is_not_null() & pl.col("PlateLocHeight").is_not_null()
    in_zone = loc & (pl.col("PlateLocSide").abs() <= ZONE["h"]) & pl.col("PlateLocHeight").is_between(ZONE["b"], ZONE["t"])
    out_zone = loc & ~in_zone
    inplay = pl.col("PitchCall") == "InPlay"
    ht = pl.col("TaggedHitType")
    tp = pl.col("TaggedPitchType").fill_null("Undefined")
    d = df.with_columns(tp.replace_strict(PITCH_FAMILY, default="Others").alias("fam"),
                        tp.replace(SUB_DISPLAY).alias("sub"))
    return d.group_by(["fam", "sub"]).agg(
        pl.len().alias("count"), swing.sum().alias("swings"),
        whiff.sum().alias("whiffs"), loc.sum().alias("loc_n"),
        ((in_zone & swing) | (out_zone & ~swing)).sum().alias("good"),
        in_zone.sum().alias("iz_n"), (in_zone & swing).sum().alias("iz_sw"),
        (in_zone & whiff).sum().alias("iz_whiff"),
        out_zone.sum().alias("oz_n"), (out_zone & swing).sum().alias("oz_sw"),
        (inplay & (ht == "GroundBall")).sum().alias("gb"), (inplay & (ht == "FlyBall")).sum().alias("fb"),
        (inplay & (ht == "LineDrive")).sum().alias("ld"), (inplay & (ht == "Popup")).sum().alias("pu"),
        (inplay & pl.col("ExitSpeed").is_not_null()).sum().alias("ev_n"),
        (inplay & (pl.col("ExitSpeed") >= HARD_HIT_MPH)).sum().alias("hh"),
        pl.col("ExitSpeed").filter(inplay).sum().alias("ev_sum"),
    ).to_dicts()


def _pitch_row(name, c, total) -> dict:
    bb = c["gb"] + c["fb"] + c["ld"] + c["pu"]
    return {
        "Pitch": name, "Pitches Seen": c["count"], "Pitch Seen %": pct(c["count"] / total) if total else "",
        "Swing %": pct(c["swings"] / c["count"]) if c["count"] else "",
        "Contact %": pct((c["swings"] - c["whiffs"]) / c["swings"]) if c["swings"] else "",
        "Good Decision %": pct(c["good"] / c["loc_n"]) if c["loc_n"] else "",
        "Whiff %": pct(c["whiffs"] / c["swings"]) if c["swings"] else "",
        "I-Zone Swing %": pct(c["iz_sw"] / c["iz_n"]) if c["iz_n"] else "",
        "I-Zone Whiff %": pct(c["iz_whiff"] / c["iz_sw"]) if c["iz_sw"] else "",
        "Chase %": pct(c["oz_sw"] / c["oz_n"]) if c["oz_n"] else "",
        "Ground Ball %": pct(c["gb"] / bb) if bb else "", "Fly Ball %": pct(c["fb"] / bb) if bb else "",
        "Line Drive %": pct(c["ld"] / bb) if bb else "", "Pop Up %": pct(c["pu"] / bb) if bb else "",
        "Hard Hit %": pct(c["hh"] / c["ev_n"]) if c["ev_n"] else "",
        "Avg EV": num(c["ev_sum"] / c["ev_n"], 1) if c["ev_n"] else "",
    }


def batter_pitch_families(df: pl.DataFrame, vs=None) -> list[dict]:
    """Batter pitch table grouped into families. Returns, in family order, dicts of
    ``{family, agg (row for the whole family), subs (per-pitch-type rows)}`` — for a collapsible table.
    ``vs`` in {None, 'Right', 'Left'} filters by pitcher hand."""
    if vs in ("Right", "Left"):
        df = df.filter(pl.col("PitcherThrows") == vs)
    comps = _pitch_components(df)
    total = sum(c["count"] for c in comps)
    out = []
    for fam in FAMILY_ORDER:
        members = [c for c in comps if c["fam"] == fam]
        if not members:
            continue
        agg = {k: sum((c[k] or 0) for c in members) for k in _COMPONENT_KEYS}
        subs = sorted(members, key=lambda c: c["count"], reverse=True)
        out.append({"family": fam, "agg": _pitch_row(fam, agg, total),
                    "subs": [_pitch_row(c["sub"], c, total) for c in subs]})
    return out


SPRAY_COLORS = {"Out": "#9aa0a6", "Single": "#1f77b4", "Double": "#2ca02c",
                "Triple": "#ff7f0e", "HomeRun": "#d62728"}


def _add_field(fig: go.Figure, R: int = 400) -> None:
    """Draw a simple baseball field: grass wedge, dirt infield + mound, base lines, foul lines, fence."""
    th = np.radians(np.linspace(-45, 45, 60))
    ax, ay = R * np.sin(th), R * np.cos(th)
    fig.add_trace(go.Scatter(x=[0, *ax, 0], y=[0, *ay, 0], fill="toself", fillcolor="#eaf3e2",
                             line=dict(color="#d3e2c7", width=1), hoverinfo="skip", showlegend=False))
    b = 90 / np.sqrt(2)                                            # base offset (90 ft paths)
    dx, dy = [0, b, 0, -b, 0], [0, b, 2 * b, b, 0]                 # home, 1B, 2B, 3B, home
    fig.add_trace(go.Scatter(x=dx, y=dy, fill="toself", fillcolor="#ecd8b6",
                             line=dict(color="white", width=1.5), hoverinfo="skip", showlegend=False))
    mth = np.radians(np.linspace(0, 360, 32))
    fig.add_trace(go.Scatter(x=9 * np.sin(mth), y=60.5 + 9 * np.cos(mth), fill="toself", fillcolor="#ecd8b6",
                             line=dict(color="#dcc59a", width=1), hoverinfo="skip", showlegend=False))
    for sgn in (-1, 1):                                            # foul lines
        fig.add_trace(go.Scatter(x=[0, sgn * R * np.sin(np.radians(45))], y=[0, R * np.cos(np.radians(45))],
                                 mode="lines", line=dict(color="#c0c0c0", width=1.2), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=ax, y=ay, mode="lines", line=dict(color="#9a9a9a", width=1.5),   # fence
                             hoverinfo="skip", showlegend=False))


def spray_fig(df: pl.DataFrame, batter: str, bats: str) -> go.Figure:
    """Batted-ball paths on a field: each ball leaves home along its launch **Direction** and lands
    at (**Distance**, **Bearing**) — drawn as a curve (quadratic Bézier) so hook/slice shows. Coloured
    by result, with a marker at the landing spot."""
    bip = df.filter((pl.col("PitchCall") == "InPlay") & pl.col("Direction").is_not_null()
                    & pl.col("Bearing").is_not_null() & pl.col("Distance").is_not_null())
    fig = go.Figure()
    _add_field(fig)
    t = np.linspace(0, 1, 18)
    h1 = lambda v: "—" if v is None else f"{v:.1f}"
    for res in ("Out", "Single", "Double", "Triple", "HomeRun"):
        s = bip.filter(pl.col("PlayResult") == res)
        if s.height == 0:
            continue
        color = SPRAY_COLORS.get(res, "#555")
        px, py, ex, ey, cd = [], [], [], [], []
        for d, dirn, bear, ev, la in zip(s["Distance"], s["Direction"], s["Bearing"], s["ExitSpeed"], s["Angle"]):
            end = (d * np.sin(np.radians(bear)), d * np.cos(np.radians(bear)))         # landing (Bearing)
            ctrl = (0.5 * d * np.sin(np.radians(dirn)), 0.5 * d * np.cos(np.radians(dirn)))  # leave home along Direction
            px += list(2 * (1 - t) * t * ctrl[0] + t ** 2 * end[0]) + [None]
            py += list(2 * (1 - t) * t * ctrl[1] + t ** 2 * end[1]) + [None]
            ex.append(end[0]); ey.append(end[1]); cd.append([int(round(d)), h1(ev), h1(la)])
        fig.add_trace(go.Scatter(x=px, y=py, mode="lines", line=dict(color=color, width=1.2),
                                 opacity=0.65, legendgroup=res, showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=ex, y=ey, mode="markers", name=f"{res} ({s.height})", legendgroup=res,
            marker=dict(color=color, size=6, line=dict(width=0.5, color="white")), customdata=cd,
            hovertemplate=(f"<b>{res}</b><br>Distance: %{{customdata[0]}} ft<br>"
                           "Exit velo: %{customdata[1]} mph<br>Launch angle: %{customdata[2]}°<extra></extra>")))
    fig.update_layout(width=560, height=520, template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=9)),
        margin=dict(l=10, r=10, t=28, b=10),
        xaxis=dict(range=[-360, 360], showticklabels=False, zeroline=False, showgrid=False),
        yaxis=dict(range=[-20, 430], showticklabels=False, zeroline=False, showgrid=False,
                   scaleanchor="x", scaleratio=1))
    return fig


# ── Splits: Batter handedness + Count ────────────────────────────────────────────────────────────
SIT_EXPR = {
    "First pitch": (pl.col("Balls") == 0) & (pl.col("Strikes") == 0),
    "Ahead":       pl.col("Strikes") > pl.col("Balls"),
    "Behind":      pl.col("Balls") > pl.col("Strikes"),
    "Even":        pl.col("Balls") == pl.col("Strikes"),
    "2-strike":    pl.col("Strikes") == 2,
    "3-ball":      pl.col("Balls") == 3,
}
SITS      = list(SIT_EXPR)
SPECIFICS = [f"{b}-{s}" for b in range(4) for s in range(3)]   # 0-0 … 3-2


def hand_mask(selm):
    if not selm or len(selm) == 2:
        return pl.lit(True)
    return pl.col("BatterSide") == ("Left" if "LHH" in selm else "Right")


def count_mask(selm):
    if not selm:
        return pl.lit(True)
    cs = pl.col("Balls").cast(str) + "-" + pl.col("Strikes").cast(str)
    m = pl.lit(False)
    for s in selm:
        m = m | (SIT_EXPR[s] if s in SIT_EXPR else (cs == s))
    return m


# ── Location heatmap surfaces ────────────────────────────────────────────────────────────────────
ZONE = dict(b=1.5, t=3.3775, h=0.83)                    # strike zone in FEET: bottom, top, half-width
XR, YR = (-2.0, 2.0), (0.5, 4.5)                        # heatmap window (feet)
MOVE_LIM = 30                                           # movement chart static ±30 in axes
_gx = np.linspace(XR[0], XR[1], 70)
_gy = np.linspace(YR[0], YR[1], 70)
_GX, _GY = np.meshgrid(_gx, _gy)
# "Pitch density" is a KDE of locations; the rest are rate surfaces = numerator / population.
STATS = {
    "Pitch density":  None,
    "Whiff%":         (pl.col("PitchCall").is_in(SWING_CALLS),                    pl.col("PitchCall") == "StrikeSwinging"),
    "Called strike%": (pl.col("PitchCall").is_in(["StrikeCalled", "BallCalled"]), pl.col("PitchCall") == "StrikeCalled"),
    "Hard hit%":      (pl.col("ExitSpeed").is_not_null(),                         pl.col("ExitSpeed") >= HARD_HIT_MPH),
}
STAT_NAMES = list(STATS)


def _xy(d):
    d = d.filter(pl.col("PlateLocSide").is_not_null() & pl.col("PlateLocHeight").is_not_null())
    return d["PlateLocSide"].to_numpy(), d["PlateLocHeight"].to_numpy()


def _surface(df, stat):
    if stat == "Pitch density":
        x, y = _xy(df)
        if len(x) < 8:
            return None
        z = gaussian_kde(np.vstack([x, y]))(np.vstack([_GX.ravel(), _GY.ravel()])).reshape(_GX.shape)
        return _gx, _gy, z
    pop_e, num_e = STATS[stat]
    xp, yp = _xy(df.filter(pop_e))
    if len(xp) < 15:
        return None                                      # too sparse to smooth a rate
    xn, yn = _xy(df.filter(pop_e).filter(num_e))
    ex = [np.linspace(XR[0], XR[1], 24), np.linspace(YR[0], YR[1], 24)]
    Hp = gaussian_filter(np.histogram2d(xp, yp, bins=ex)[0], 1.3)
    Hn = gaussian_filter(np.histogram2d(xn, yn, bins=ex)[0], 1.3)
    rate = np.divide(Hn, Hp, out=np.full_like(Hp, np.nan), where=Hp > 0.4) * 100
    xc = (ex[0][:-1] + ex[0][1:]) / 2
    yc = (ex[1][:-1] + ex[1][1:]) / 2
    return xc, yc, rate.T


_EMPTY_SURFACE = ([0.0], [2.4], [[None]])


def heat_surface_data(df):
    """(list of (x, y, z) per stat with fallbacks, n_located) — used to build AND patch the heatmap."""
    surfaces = []
    for stat in STAT_NAMES:
        surf = _surface(df, stat)
        surfaces.append(tuple(surf) if surf else _EMPTY_SURFACE)
    n = df.filter(pl.col("PlateLocHeight").is_not_null()).height
    return surfaces, n


# ── Batter location heatmap: Exit Velo / Whiff% / Chase% by location, filtered by pitch family ───
BATTER_HEAT_STATS = ["Exit Velo", "Whiff %", "Chase %"]
BATTER_FAMILIES = ["All", "Fastballs", "Breaking", "Offspeed"]


def _family_filter(df: pl.DataFrame, family: str) -> pl.DataFrame:
    if not family or family == "All":
        return df
    fam = pl.col("TaggedPitchType").fill_null("Undefined").replace_strict(PITCH_FAMILY, default="Others")
    return df.filter(fam == family)


def _ev_surface(df):
    """Smoothed AVERAGE exit velocity by location: EV-weighted histogram / count histogram."""
    d = df.filter((pl.col("PitchCall") == "InPlay") & pl.col("ExitSpeed").is_not_null()
                  & pl.col("PlateLocSide").is_not_null() & pl.col("PlateLocHeight").is_not_null())
    if d.height < 15:
        return None
    x, y = d["PlateLocSide"].to_numpy(), d["PlateLocHeight"].to_numpy()
    ex = [np.linspace(XR[0], XR[1], 24), np.linspace(YR[0], YR[1], 24)]
    Hw = gaussian_filter(np.histogram2d(x, y, bins=ex, weights=d["ExitSpeed"].to_numpy())[0], 1.3)
    Hn = gaussian_filter(np.histogram2d(x, y, bins=ex)[0], 1.3)
    avg = np.divide(Hw, Hn, out=np.full_like(Hw, np.nan), where=Hn > 0.4)
    xc = (ex[0][:-1] + ex[0][1:]) / 2
    yc = (ex[1][:-1] + ex[1][1:]) / 2
    return xc, yc, avg.T


def _rate_surface(pop: pl.DataFrame, num: pl.DataFrame):
    """Smoothed rate-% surface: (numerator rows / population rows) per location area."""
    xp, yp = _xy(pop)
    if len(xp) < 15:
        return None
    xn, yn = _xy(num)
    ex = [np.linspace(XR[0], XR[1], 24), np.linspace(YR[0], YR[1], 24)]
    Hp = gaussian_filter(np.histogram2d(xp, yp, bins=ex)[0], 1.3)
    Hn = gaussian_filter(np.histogram2d(xn, yn, bins=ex)[0], 1.3)
    rate = np.divide(Hn, Hp, out=np.full_like(Hp, np.nan), where=Hp > 0.4) * 100
    xc = (ex[0][:-1] + ex[0][1:]) / 2
    yc = (ex[1][:-1] + ex[1][1:]) / 2
    return xc, yc, rate.T


def _whiff_surface(df):
    """Whiff% by location: whiffs / swings per area."""
    pop = df.filter(pl.col("PitchCall").is_in(SWING_CALLS))
    return _rate_surface(pop, pop.filter(pl.col("PitchCall") == "StrikeSwinging"))


def _chase_surface(df):
    """Chase% by location: swing rate on OUT-of-zone pitches per area (zone interior stays blank —
    chase is undefined on pitches in the zone)."""
    in_zone = ((pl.col("PlateLocSide").abs() <= ZONE["h"])
               & pl.col("PlateLocHeight").is_between(ZONE["b"], ZONE["t"]))
    pop = df.filter(pl.col("PlateLocSide").is_not_null() & pl.col("PlateLocHeight").is_not_null()
                    & ~in_zone)
    return _rate_surface(pop, pop.filter(pl.col("PitchCall").is_in(SWING_CALLS)))


_BHEAT_FN = {"Exit Velo": _ev_surface, "Whiff %": _whiff_surface, "Chase %": _chase_surface}


def batter_heat_data(df: pl.DataFrame, family: str = "All"):
    """([(x, y, z) per BATTER_HEAT_STATS], n_located) for one family — builds AND patches the chart."""
    d = _family_filter(df, family)
    surfaces = []
    for stat in BATTER_HEAT_STATS:
        surf = _BHEAT_FN[stat](d)
        surfaces.append(tuple(surf) if surf else _EMPTY_SURFACE)
    n = d.filter(pl.col("PlateLocHeight").is_not_null()).height
    return surfaces, n


def batter_heatmap_fig(df: pl.DataFrame, family: str = "All") -> go.Figure:
    """Batter location chart: smoothed Exit Velo / Whiff% surfaces (on-chart stat dropdown),
    strike zone drawn with the white-halo outline. Catcher's view, same window as the pitcher map."""
    surfaces, n0 = batter_heat_data(df, family)
    fig = go.Figure()
    for i, stat in enumerate(BATTER_HEAT_STATS):
        x, y, z = surfaces[i]
        fig.add_trace(go.Heatmap(x=x, y=y, z=z, colorscale="Reds", zsmooth="best", visible=(i == 0),
            hoverinfo="skip", colorbar=dict(title=("mph" if stat == "Exit Velo" else "%"),
                                            thickness=12, len=0.8)))
    for _c, _w in (("white", 4), ("black", 1.6)):
        fig.add_shape(type="rect", x0=-ZONE["h"], y0=ZONE["b"], x1=ZONE["h"], y1=ZONE["t"],
                      line=dict(color=_c, width=_w))
    buttons = [dict(label=stat, method="update",
                    args=[{"visible": [j == i for j in range(len(BATTER_HEAT_STATS))]}])
               for i, stat in enumerate(BATTER_HEAT_STATS)]
    fig.update_layout(width=470, height=520, template="plotly_white",
        title=dict(text=f"Location · {family} · {n0:,} pitches", x=0.62, y=0.97, font=dict(size=13)),
        updatemenus=[dict(buttons=buttons, direction="down", showactive=True,
                          x=0.0, xanchor="left", y=1.12, yanchor="top", pad=dict(l=2, t=2))],
        margin=dict(l=10, r=10, t=64, b=10),
        xaxis=dict(range=list(XR), title="", showticklabels=False, fixedrange=True),
        yaxis=dict(range=list(YR), title="", showticklabels=False, fixedrange=True,
                   scaleanchor="x", scaleratio=1))
    return fig


# ── Figures (go.Figure, unchanged appearance) ────────────────────────────────────────────────────
def hand_of(df: pl.DataFrame) -> str:
    throws = df["PitcherThrows"].drop_nulls()
    return {"Right": "RHP", "Left": "LHP"}.get(throws.mode()[0] if len(throws) else "", "")


def movement_fig(pitches: pl.DataFrame, colors: dict | None = None) -> go.Figure:
    """``colors`` overrides the palette (label -> hex) — used by the AutoCluster view."""
    palette = colors or PITCH_COLORS
    # Include unclassified pitches (Undefined/Other, shown grey) so they can be lasso-selected + retagged.
    dm = (pitches.filter(pl.col("TaggedPitchType").is_not_null()
                         & pl.col("HorzBreak").is_not_null() & pl.col("InducedVertBreak").is_not_null())
                 .with_columns(pl.col("TaggedPitchType").replace(PITCH_NAMES).alias("PT")))
    f1 = lambda v: f"{v:.1f}" if v is not None else "—"
    f0 = lambda v: f"{v:.0f}" if v is not None else "—"
    fig = go.Figure()
    for pt in dm.group_by("PT").len().sort("len", descending=True)["PT"].to_list():
        s = dm.filter(pl.col("PT") == pt)
        cd = [[pt, f1(a), f0(b), f1(c), f1(e), (r or "—"), u] for a, b, c, e, r, u in zip(
              s["RelSpeed"].to_list(), s["SpinRate"].to_list(), s["InducedVertBreak"].to_list(),
              s["HorzBreak"].to_list(), s["PitchCall"].to_list(), s["PitchUID"].to_list())]
        fig.add_trace(go.Scattergl(x=s["HorzBreak"].to_list(), y=s["InducedVertBreak"].to_list(),
            mode="markers", name=f"{pt} ({s.height})",
            marker=dict(color=palette.get(pt, "#8c8c8c"), size=6, opacity=0.6), customdata=cd,
            hovertemplate="<b>%{customdata[0]}</b><br>Velo %{customdata[1]} mph · Spin %{customdata[2]} rpm<br>"
                          "IVB %{customdata[3]} · HB %{customdata[4]} in<br>%{customdata[5]}<extra></extra>"))
    fig.update_layout(width=520, height=470, template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=9)),
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(range=[-MOVE_LIM, MOVE_LIM], title="", zeroline=True, zerolinecolor="#ccc"),
        yaxis=dict(range=[-MOVE_LIM, MOVE_LIM], title="", zeroline=True, zerolinecolor="#ccc",
                   scaleanchor="x", scaleratio=1))
    return fig


def heatmap_fig(pitches: pl.DataFrame) -> go.Figure:
    """One (hidden) heatmap trace per stat + an on-chart stat dropdown (Plotly updatemenus)."""
    surfaces, n0 = heat_surface_data(pitches)
    fig = go.Figure()
    for i, stat in enumerate(STAT_NAMES):
        x, y, z = surfaces[i]
        fig.add_trace(go.Heatmap(x=x, y=y, z=z, colorscale="Reds", zsmooth="best", visible=(i == 0),
            hoverinfo="skip", colorbar=dict(title=("density" if stat == "Pitch density" else "%"),
                                            thickness=12, len=0.8)))
    # Strike zone — drawn on every stat surface (incl. Pitch density). A white halo under a black
    # outline keeps it visible on both the light rate maps and the dark-red density peak.
    for _c, _w in (("white", 4), ("black", 1.6)):
        fig.add_shape(type="rect", x0=-ZONE["h"], y0=ZONE["b"], x1=ZONE["h"], y1=ZONE["t"],
                      line=dict(color=_c, width=_w))
    buttons = [dict(label=stat, method="update", args=[{"visible": [j == i for j in range(len(STAT_NAMES))]}])
               for i, stat in enumerate(STAT_NAMES)]
    fig.update_layout(width=470, height=470, template="plotly_white",
        title=dict(text=f"Location · {n0:,} pitches", x=0.62, y=0.97, font=dict(size=13)),
        updatemenus=[dict(buttons=buttons, direction="down", showactive=True,
                          x=0.0, xanchor="left", y=1.14, yanchor="top", pad=dict(l=2, t=2))],
        margin=dict(l=10, r=10, t=74, b=10),
        xaxis=dict(range=list(XR), title="", showticklabels=False, fixedrange=True),
        yaxis=dict(range=list(YR), title="", showticklabels=False, fixedrange=True,
                   scaleanchor="x", scaleratio=1))
    return fig


def _rgba(hexc, a):
    h = hexc.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def velocity_fig(df: pl.DataFrame, pitcher: str, hand: str, colors: dict | None = None) -> go.Figure:
    palette = colors or PITCH_COLORS
    d = (df.filter(pl.col("TaggedPitchType").is_not_null() & ~pl.col("TaggedPitchType").is_in(NON_PITCHES)
                   & pl.col("RelSpeed").is_not_null())
           .with_columns(pl.col("TaggedPitchType").replace(PITCH_NAMES).alias("PT")))
    fig = go.Figure()
    if d.height >= 5:
        n = d.height
        grid = np.linspace(d["RelSpeed"].min() - 3, d["RelSpeed"].max() + 3, 250)
        total = np.zeros_like(grid)
        for pt in d.group_by("PT").len().sort("len", descending=True)["PT"].to_list():
            v = d.filter(pl.col("PT") == pt)["RelSpeed"].to_numpy()
            if len(v) < 5 or v.std() < 1e-6:
                continue
            y = gaussian_kde(v)(grid) * (len(v) / n) * 100          # weighted by usage -> % of all pitches
            total += y
            c = palette.get(pt, "#555555")
            fig.add_trace(go.Scatter(x=grid, y=y, name=f"{pt} ({len(v)})", mode="lines",
                line=dict(color=c, width=2), fill="tozeroy", fillcolor=_rgba(c, 0.22),
                hovertemplate="%{y:.1f}%<extra>%{fullData.name}</extra>"))
        fig.add_trace(go.Scatter(x=grid, y=total, name="All Pitches", mode="lines",
            line=dict(color="#444", width=1.4, dash="dash"), hoverinfo="skip"))
    fig.update_layout(width=940, height=420, template="plotly_white", hovermode="x unified",
        title=dict(text=f"{pitcher} ({hand}) — Frequency of Pitches by Pitch Speed", x=0.5, font=dict(size=14)),
        margin=dict(l=20, r=20, t=50, b=80),
        legend=dict(orientation="h", yanchor="top", y=-0.2, x=0.5, xanchor="center"),
        xaxis=dict(title="Pitch Speed (MPH)", ticksuffix=" MPH", showspikes=True, spikemode="across",
                   spikethickness=1, spikecolor="#999", spikedash="dot"),
        yaxis=dict(title="Frequency of Speed", ticksuffix="%", rangemode="tozero"))
    return fig
