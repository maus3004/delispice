"""AutoCluster adapter — bridges ``autotagger.py`` (sibling module, the reusable GMM extracted from
``autotagger.ipynb``) to what delispice_app needs: PitchUID-keyed assignments, a JSON-safe result,
and the app's smaller per-pitcher floor.

Lives in backend/models next to the model it adapts; the app loads it by file path (backend/ is not
a package), and this file loads ``autotagger.py`` the same way, so both work no matter how they are
imported. sklearn is only imported when clustering actually runs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl

AUTOTAGGER_PATH = Path(__file__).resolve().parent / "autotagger.py"
MIN_PITCHES = 30

_MOD = [None]


def _autotagger():
    if _MOD[0] is None:
        spec = importlib.util.spec_from_file_location("autotagger", AUTOTAGGER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                     # imports sklearn lazily, on first use
        _MOD[0] = mod
    return _MOD[0]


def run_gmm(df: pl.DataFrame, use_release: bool = False) -> dict:
    """Cluster one pitcher's pitches via the shared autotagger. Returns the app's JSON-safe shape:
    ``{assign: {PitchUID: cluster_int}, k, n, n_unclustered, features, bic_table}``.
    Raises ``ValueError`` with a readable message when there is too little complete data."""
    at = _autotagger()
    d = df.filter(pl.col("PitchUID").is_not_null())
    res = at.autotag_pitcher(d, use_release=use_release, min_pitches=MIN_PITCHES)
    uids = d["PitchUID"].to_list()
    assign = {uids[i]: int(lab) for i, lab in zip(res["index"], res["labels"])}
    return {"assign": assign, "k": res["k"], "n": res["n"],
            "n_unclustered": df.height - res["n"], "features": res["features"],
            "bic_table": res["bic_table"]}
