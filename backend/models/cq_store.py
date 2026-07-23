"""Artifact store + training CLI for the contact-quality (xRV) model.

The k-NN model in ``contact_quality.py`` takes minutes to train (full-season scan + fit), so it is
NEVER trained at request time. Instead:

  * Train offline (here), once per (Level, Year):   python -m backend.models.cq_store
    Artifacts land in ``backend/models/artifacts/`` as ``cq_{level}_{year}.npz + .json``
    (~10 MB each; git-ignored — build them on each machine, the data is already there).
  * Serve from the artifact: ``load(level, year)`` -> fitted ContactQualityModel (or None if not
    trained). delispice_app caches loaded models in-process and scores batted balls per report.

Retrain when a season's data grows (new games): just re-run the CLI and restart the app.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / "backend" / "models" / "artifacts"
_EVENT_COLS = ["GameID", "Inning", "Top/Bottom", "PlayResult", "RunsScored",
               "Direction", "ExitSpeed", "Angle", "re288_state"]


def _base(level: str, year: str) -> Path:
    return ARTIFACT_DIR / f"cq_{level.replace(' ', '')}_{year}"


def exists(level: str, year: str) -> bool:
    b = _base(level, year)
    return b.with_name(b.name + ".npz").exists() and b.with_name(b.name + ".json").exists()


def load(level: str, year: str):
    """Fitted ContactQualityModel for (level, year), or None if no artifact has been trained."""
    if not exists(level, year):
        return None
    from backend.models import contact_quality as cq
    return cq.ContactQualityModel.load(_base(level, year))


def _events(level: str, year: str) -> pl.DataFrame:
    """Season events for any Level. D1 has its own partition dir; every other Level lives inside
    the Others/ partition, so those are scanned and filtered by the Level column. P4 is a League-
    based pseudo-level served from the D1 partition (handled inside cq.load_events)."""
    from backend.models import contact_quality as cq
    if level == cq.P4_LEVEL or (cq.PIPELINE / level).exists():
        return cq.load_events(level, year)
    files = sorted((cq.PIPELINE / "Others" / year).glob("**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for level={level} year={year} (checked Others/{year})")
    return (pl.scan_parquet([str(f) for f in files])
              .select([*_EVENT_COLS, "Level"])
              .filter(pl.col("Level") == level).drop("Level").collect())


def train(level: str, year: str, k: int = 800, alpha: float = 0.1):
    """Fit + save the (level, year) model. Skips the expensive training-set self-query that
    ``expected_run_values`` runs — the app only needs the fitted model, not labeled training rows."""
    from backend.models import contact_quality as cq
    rem = cq.load_re_matrix(level, year)
    if rem.height == 0:
        raise ValueError(f"re288_matrix has no rows for level={level} year={year}")
    df = cq.filter_batted_balls(cq.compute_run_delta(_events(level, year), rem))
    weights = cq.linear_weights(df)
    X = df.select(cq.FEATS).to_numpy().astype(float)
    y = df["PlayResult"].replace_strict(cq.LABEL_MAP, return_dtype=pl.Int64).to_numpy()
    model = cq.ContactQualityModel().fit(X, y, weights, k, alpha)
    model.meta = {"level": level, "year": year, "n_rows": df.height, "feats": cq.FEATS}
    model.save(_base(level, year))
    return model


def available(level: str) -> list[str]:
    """Years trainable for a level: present in the re-matrix AND having data on disk."""
    from backend.models import contact_quality as cq
    rem = pl.read_parquet(cq.REM_PATH).filter(pl.col("Level") == level)
    part = "D1" if level == cq.P4_LEVEL else (level if (cq.PIPELINE / level).exists() else "Others")
    return sorted(y for y in rem["year"].unique().to_list() if (cq.PIPELINE / part / y).is_dir())


def main(argv=None):
    p = argparse.ArgumentParser(description="Train contact-quality (xRV) artifacts.")
    p.add_argument("--level", default="D1", help="Level to train (default: D1)")
    p.add_argument("--years", nargs="*", default=None,
                   help="Years to train (default: every year with a re-matrix + data)")
    p.add_argument("--k", type=int, default=800)
    p.add_argument("--alpha", type=float, default=0.1)
    args = p.parse_args(argv)
    years = args.years or available(args.level)
    if not years:
        raise SystemExit(f"nothing trainable for level={args.level}")
    for yr in years:
        print(f"training cq {args.level} {yr} …", flush=True)
        m = train(args.level, yr, k=args.k, alpha=args.alpha)
        print(f"  saved {_base(args.level, yr).name} ({m.meta['n_rows']:,} batted balls)")


if __name__ == "__main__":
    main()
