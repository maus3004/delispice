"""GMM pitch auto-tagger — the reusable module extracted from ``autotagger.ipynb``.

Fits per-pitcher Gaussian mixtures over velo/spin/movement (optionally + release) features and
picks the number of pitches k by **ICL** (BIC + 2 * total assignment entropy), which prefers
well-separated clusters over raw BIC. Faithful to the notebook: StandardScaler ->
GaussianMixture(covariance_type="full", n_init=20, random_state=42), k swept 1..6, HorzBreak
reflected for left-handed pitchers (anchoring).

Use it from other models / reports:

    import autotagger                                  # backend/models on sys.path (notebooks), or
    res  = autotagger.autotag_pitcher(one_pitcher_df)  # any frame with the feature columns
    tbl  = autotagger.cluster_means(res)               # interpret clusters in original units
    runs = autotagger.autotag_by_pitcher(df)           # batch: every pitcher with >=300 pitches

The pitcher app consumes this through the sibling adapter ``cluster.py`` (PitchUID-keyed labels).
Cluster ids are renumbered by usage (Cluster 0 = most thrown) unless ``relabel=False``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

ROOT     = Path(__file__).resolve().parent.parent.parent            # repo root (robust to CWD)
PIPELINE = ROOT / "data_pipeline" / "wbaserunners"

FEATS_DEFAULT = ["RelSpeed", "SpinRate", "InducedVertBreak", "HorzBreak"]
FEATS_RELEASE = ["RelSpeed", "SpinRate", "RelHeight", "InducedVertBreak", "HorzBreak", "Extension"]
LOAD_COLS     = ["Pitcher", "PitcherThrows", "TaggedPitchType", "RelSpeed", "SpinRate",
                 "RelHeight", "InducedVertBreak", "HorzBreak", "Extension"]
NON_PITCH_TAGS      = ["Knuckleball", "Other", "Undefined"]          # pooled-analysis exclusions
K_RANGE             = range(1, 7)
GMM_KW              = dict(covariance_type="full", n_init=20, random_state=42)
MIN_PITCHES_POOLED  = 300                                            # notebook's per-pitcher floor
MIN_PITCHES_SINGLE  = 30                                             # sane floor for one pitcher


# ── Data prep (notebook cells 1 + 3) ──────────────────────────────────────────────────────────────
def load_pitches(level: str, year: str) -> pl.DataFrame:
    """Scan one (level, year)'s wbaserunners parquets -> the autotagger columns, with the
    notebook's pooled-analysis filters (drops Both/Undefined throwers and non-pitch tags)."""
    files = sorted((PIPELINE / level / year).glob("**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for level={level} year={year} under {PIPELINE}")
    return (pl.scan_parquet([str(f) for f in files])
              .select(LOAD_COLS)
              .drop_nulls()
              .filter(~pl.col("PitcherThrows").is_in(["Both", "Undefined"]))
              .filter(~pl.col("TaggedPitchType").is_in(NON_PITCH_TAGS))
              .collect())


def anchor_lefties(df: pl.DataFrame) -> pl.DataFrame:
    """Reflect HorzBreak for lefty pitchers so arm-side run points the same way for everyone."""
    return df.with_columns(HorzBreak=pl.when(pl.col("PitcherThrows") == "Left")
                                       .then(-pl.col("HorzBreak")).otherwise(pl.col("HorzBreak")))


def min_pitch_filter(df: pl.DataFrame, min_pitches: int = MIN_PITCHES_POOLED) -> pl.DataFrame:
    return df.filter(pl.len().over("Pitcher") >= min_pitches)


# ── Model (notebook cell 4) ───────────────────────────────────────────────────────────────────────
def fit_gmm(X: np.ndarray, k_range=K_RANGE) -> dict:
    """Scale X, sweep k over ``k_range``, keep the min-ICL model.

    Returns {model, scaler, labels, k, bic_table}; labels are the model's raw component ids."""
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    best_icl, best_model, best_k = np.inf, None, None
    bic_table = []
    for k in k_range:
        if k > len(Xs):
            break
        gmm = GaussianMixture(n_components=k, **GMM_KW)
        gmm.fit(Xs)
        bic = gmm.bic(Xs)
        resp = gmm.predict_proba(Xs)
        entropy = -np.sum(resp * np.log(resp + 1e-12))
        icl = bic + 2 * entropy                          # lower ICL takes precedent over BIC
        bic_table.append({"k": k, "BIC": float(bic), "ICL": float(icl)})
        if icl < best_icl:
            best_icl, best_model, best_k = icl, gmm, k
    return {"model": best_model, "scaler": scaler,
            "labels": best_model.predict(Xs), "k": int(best_k), "bic_table": bic_table}


def relabel_by_size(labels: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Renumber cluster ids by descending usage (0 = most thrown).

    Returns (new_labels, order) where ``order[new_id] = old component id``."""
    order = np.argsort(-np.bincount(labels, minlength=k))
    rank = np.empty(k, dtype=int)
    rank[order] = np.arange(k)
    return rank[labels], order


# ── High-level entrypoints ────────────────────────────────────────────────────────────────────────
def autotag_pitcher(df: pl.DataFrame, use_release: bool = False, features: list[str] | None = None,
                    min_pitches: int = MIN_PITCHES_SINGLE, relabel: bool = True) -> dict:
    """Cluster ONE pitcher's pitches. ``df`` is any frame holding PitcherThrows + the features —
    no tag filtering here (assigning Undefined/Other pitches is the point when retagging).

    Returns {labels, index, k, n, bic_table, features, model, scaler, means, counts} where
    ``index`` holds the df row positions that were clustered (rows missing a feature are skipped)
    and ``means``/``counts`` are per final cluster id, in original units."""
    feats = features or (FEATS_RELEASE if use_release else FEATS_DEFAULT)
    d = (df.with_row_index("_row")
           .pipe(anchor_lefties)
           .drop_nulls(subset=feats))
    n = d.height
    if n < min_pitches:
        raise ValueError(f"Need at least {min_pitches} pitches with complete features to cluster (have {n}).")

    res = fit_gmm(d.select(feats).to_numpy(), K_RANGE)
    labels, k = res["labels"], res["k"]
    # Per-pitch confidence = the model's max posterior (responsibility). It's a max over components,
    # so the size-relabel below (which only renumbers ids) leaves it unchanged — no reordering needed.
    conf = res["model"].predict_proba(res["scaler"].transform(d.select(feats).to_numpy())).max(axis=1)
    means = res["scaler"].inverse_transform(res["model"].means_)
    if relabel:
        labels, order = relabel_by_size(labels, k)
        means = means[order]
    return {"labels": labels, "index": d["_row"].to_numpy(), "k": k, "n": n, "conf": conf,
            "bic_table": res["bic_table"], "features": feats,
            "model": res["model"], "scaler": res["scaler"],
            "means": means, "counts": np.bincount(labels, minlength=k)}


def autotag_by_pitcher(df: pl.DataFrame, use_release: bool = False,
                       min_pitches: int = MIN_PITCHES_POOLED) -> dict[str, dict]:
    """Batch runner: fit every pitcher with >= ``min_pitches`` rows. {pitcher: autotag result}."""
    out = {}
    for (pitcher,), pdf in df.group_by("Pitcher"):
        if pdf.height >= min_pitches:
            out[pitcher] = autotag_pitcher(pdf, use_release=use_release, min_pitches=min_pitches)
    return out


def cluster_means(result: dict) -> pl.DataFrame:
    """Interpret a fit in original units (notebook's last cell): one row per final cluster id,
    the GMM component means inverse-transformed, plus pitch counts."""
    tbl = pl.DataFrame(result["means"], schema=result["features"])
    return (tbl.with_columns(pl.Series("Count", result["counts"]))
               .with_row_index("Cluster"))
