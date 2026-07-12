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


def run_gmm(df: pl.DataFrame, use_release: bool = False) -> dict:
    """Cluster one pitcher's pitches via the shared autotagger. Returns the app's JSON-safe shape:
    ``{assign: {PitchUID: cluster_int}, conf: {PitchUID: max_posterior}, k, n, n_unclustered,
    features, bic_table}``. ``conf`` is the GMM's confidence in each pitch's assignment (0–1) — the
    app flags the low ones for review. Raises ``ValueError`` when there is too little complete data."""
    at = _autotagger()
    d = df.filter(pl.col("PitchUID").is_not_null())
    res = at.autotag_pitcher(d, use_release=use_release, min_pitches=MIN_PITCHES)
    uids = d["PitchUID"].to_list()
    assign = {uids[i]: int(lab) for i, lab in zip(res["index"], res["labels"])}
    conf = {uids[i]: round(float(cf), 4) for i, cf in zip(res["index"], res["conf"])}
    return {"assign": assign, "conf": conf, "k": res["k"], "n": res["n"],
            "n_unclustered": df.height - res["n"], "features": res["features"],
            "bic_table": res["bic_table"]}
