"""AutoCluster adapter — bridges ``autotagger.py`` (sibling module, the reusable GMM extracted from
``notebooks/autotagger.ipynb``) to what delispice_app needs: PitchUID-keyed assignments, a JSON-safe
result, and the app's smaller per-pitcher floor.

Part of the ``backend.models`` package. The autotagger import stays inside a function so sklearn is
only imported when clustering actually runs.
"""
from __future__ import annotations

import polars as pl

MIN_PITCHES = 30


def _autotagger():
    from backend.models import autotagger          # deferred: pulls in sklearn on first cluster run
    return autotagger


def run_gmm(df: pl.DataFrame, use_release: bool = False, k: int | None = None) -> dict:
    """Cluster one pitcher's pitches via the shared autotagger. Returns the app's JSON-safe shape:
    ``{assign: {PitchUID: cluster_int}, conf: {PitchUID: max_posterior}, k, n, n_unclustered,
    features, bic_table}``. ``conf`` is the GMM's confidence in each pitch's assignment (0–1) — the
    app flags the low ones for review. ``k`` pins the cluster count instead of letting ICL choose.
    Raises ``ValueError`` when there is too little complete data."""
    at = _autotagger()
    d = df.filter(pl.col("PitchUID").is_not_null())
    res = at.autotag_pitcher(d, use_release=use_release, min_pitches=MIN_PITCHES, k=k)
    uids = d["PitchUID"].to_list()
    assign = {uids[i]: int(lab) for i, lab in zip(res["index"], res["labels"])}
    conf = {uids[i]: round(float(cf), 4) for i, cf in zip(res["index"], res["conf"])}
    return {"assign": assign, "conf": conf, "k": res["k"], "n": res["n"],
            "n_unclustered": df.height - res["n"], "features": res["features"],
            "bic_table": res["bic_table"]}


def split_gmm(df: pl.DataFrame, features: list[str], k: int = 2) -> dict:
    """Force a ``k``-way GMM within one cluster's pitches (``df`` = just that cluster's rows).
    Returns ``{assign: {PitchUID: 0..k-1}, conf: {PitchUID: max_posterior}, n}`` — sub-group 0 is the
    most-thrown. Raises ``ValueError`` when the cluster is too small to split."""
    at = _autotagger()
    d = df.filter(pl.col("PitchUID").is_not_null())
    res = at.split_pitches(d, features=features, k=k)
    uids = d["PitchUID"].to_list()
    return {"assign": {uids[i]: int(lab) for i, lab in zip(res["index"], res["labels"])},
            "conf": {uids[i]: round(float(cf), 4) for i, cf in zip(res["index"], res["conf"])},
            "n": res["n"], "separation": round(res["separation"], 2)}
