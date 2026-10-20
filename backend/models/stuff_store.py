"""Artifact store + CLI for Stuff+ v1 (``stuff_v1.py``), the same split as ``eye_store``/``eye_v1``.

One model covers all of D1 (trained on 2022-26 pooled, not per season), and it is retrained ONCE A
SEASON with a version bump (PR3). The Stuff+ scale is frozen inside it, so grades don't move between
retrains. What changes nightly is the score tables: new games get tagged with the frozen pitch-group
templates and graded.

  * Train offline:   python -m backend.models.stuff_store --train            (about 30 min)
    Artifacts land in ``pipeline_v2/artifacts/`` as ``stuff_D1_v1_{group}_{piece}.txt`` (9 LightGBM
    files) + ``stuff_D1_v1.json`` (scale, prices, context weights, templates, ...). Git-ignored;
    build them on each machine, the data is already there. Training also writes every season's
    score table.
  * Re-score from the existing model:   python -m backend.models.stuff_store --score-only --years 2026
  * Serve: ``load()`` -> StuffModel (or None), ``stuff_lookup(year)`` -> PitchUID -> grades.

P4 pitches are D1 pitches graded by the same model, so the P4 level reads the D1 tables (PR6).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

from pipeline_v2 import config

ARTIFACT_DIR = config.ARTIFACTS_DIR       # pipeline_v2/artifacts


def _name(smoke: bool = False) -> str:
    from backend.models import stuff_v1
    return f"stuff_{stuff_v1.LEVEL}_{stuff_v1.MODEL_VERSION}" + ("_smoke" if smoke else "")


def _base(smoke: bool = False) -> Path:
    return ARTIFACT_DIR / _name(smoke)


def exists(smoke: bool = False) -> bool:
    from backend.models import stuff_v1
    b = _base(smoke)
    return (b.with_name(b.name + ".json").exists()
            and all(b.with_name(f"{b.name}_{g}_{p}.txt").exists() for g in stuff_v1.GROUPS for p in stuff_v1.PIECES))


def load(smoke: bool = False):
    """The fitted StuffModel, or None if it hasn't been trained."""
    if not exists(smoke):
        return None
    from backend.models import stuff_v1
    return stuff_v1.StuffModel.load(_base(smoke))


# ── Score tables (one per season) ────────────────────────────────────────────────────────────────
# Keyed by PitchUID, so a night's new games simply miss until the next re-score. A RETRAIN changes
# every grade, so train() rewrites every season's table along with the model.
def _score_path(year: str, smoke: bool = False) -> Path:
    from backend.models import stuff_v1
    return ARTIFACT_DIR / f"stuffscore_{stuff_v1.LEVEL}_{year}{'_smoke' if smoke else ''}.parquet"


def score_exists(year: str) -> bool:
    return _score_path(year).exists()


def _write_scores(scores: pl.DataFrame, smoke: bool = False) -> dict[str, int]:
    from backend.models.cq_store import _atomic_write
    out = {}
    for (y,), d in sorted(scores.partition_by("Year", as_dict=True).items()):
        _atomic_write(d.drop("Year"), _score_path(str(y), smoke))
        out[str(y)] = d.height
    return out


def train(years: list[str] | None = None, fraction: float = 1.0):
    """Fit + save the model and every season's score table. ``fraction`` < 1 is a smoke test: it
    saves under ``*_smoke`` names, so it can never overwrite the real artifacts."""
    from backend.models import stuff_v1
    smoke = fraction < 1
    model, scores = stuff_v1.train(years, fraction)
    model.save(_base(smoke))
    return model, _write_scores(scores, smoke)


def build_score_cache(year: str) -> tuple[Path, int]:
    """Tag + grade every D1 pitch of ``year`` with the existing model."""
    from backend.models import stuff_v1
    from backend.models.cq_store import _atomic_write
    model = load()
    if model is None:
        raise FileNotFoundError("no stuff artifact; train it first: python -m backend.models.stuff_store --train")
    scores = stuff_v1.score(stuff_v1.load_pitches([year], with_outcomes=False), model)
    path = _score_path(year)
    _atomic_write(scores.drop("Year") if scores.height else scores, path)
    return path, scores.height


def stuff_lookup(year: str, level: str = "D1") -> pl.DataFrame | None:
    """PitchUID -> grades for one season; None when the table hasn't been built. P4 reads D1."""
    from backend.models import contact_quality as cq
    if level not in ("D1", cq.P4_LEVEL):
        return None
    path = _score_path(year)
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path).unique(subset=["PitchUID"])
    except Exception as e:            # a truncated/corrupt table must never break a report
        print(f"stuff table {path.name} unreadable, scoring live instead: {e}")
        return None


def main(argv=None):
    print(config.data_mode(), flush=True)
    p = argparse.ArgumentParser(description="Train / re-score Stuff+ v1 (D1).")
    p.add_argument("--train", action="store_true", help="train the model on --years (default: all) and score them")
    p.add_argument("--score-only", action="store_true", help="re-score --years from the existing model")
    p.add_argument("--years", nargs="*", default=None, help="seasons (default: every season on disk)")
    p.add_argument("--fraction", type=float, default=1.0, help="smoke test: train on this share of pitchers")
    args = p.parse_args(argv)
    from backend.models import stuff_v1
    years = args.years or stuff_v1.available_years()
    if args.train:
        model, n = train(years, args.fraction)
        print(f"saved {_name(args.fraction < 1)} ({model.meta['n_swings']:,} swings; trees {model.meta['trees']})")
        print("score tables: " + ", ".join(f"{y}: {k:,}" for y, k in n.items()))
    elif args.score_only:
        failed = []
        for y in years:               # one season failing doesn't stop the others
            try:
                path, n = build_score_cache(y)
                print(f"saved {path.name} ({n:,} pitches)", flush=True)
            except Exception as e:
                print(f"  FAILED {y}: {type(e).__name__}: {e}", flush=True)
                failed.append(y)
        if failed:
            raise SystemExit(f"stuff: re-score failed for {failed}")
    else:
        p.error("pass --train or --score-only")


if __name__ == "__main__":
    main()
