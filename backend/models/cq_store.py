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
import os
from pathlib import Path

import polars as pl

from pipeline_v2 import config

ARTIFACT_DIR = config.ARTIFACTS_DIR       # pipeline_v2/artifacts, or backend/models/artifacts with DELISPICE_DATA=old
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
    lf = pl.scan_parquet([str(f) for f in files])
    return (lf.select(cq.with_verified(lf, [*_EVENT_COLS, "Level"]))
              .filter(pl.col("Level") == level).drop("Level").collect())


def train(level: str, year: str, k: int = 800, alpha: float = 0.1):
    """Fit + save the (level, year) model. Skips the expensive training-set self-query that
    ``expected_run_values`` runs — the app only needs the fitted model, not labeled training rows."""
    from backend.models import contact_quality as cq
    rem = cq.load_re_matrix(level, year)
    if rem.height == 0:
        raise ValueError(f"re288_matrix has no rows for level={level} year={year}")
    # Whole games drop out, so the state after each ball is computed on complete half-innings first.
    df, skipped = cq.keep_verified(cq.filter_batted_balls(cq.compute_run_delta(_events(level, year), rem)))
    if df.height <= k:     # the k-NN needs more reference balls than neighbours, or it can't score anything
        raise cq.Refused(f"only {df.height:,} verified batted balls for {level} {year}; need more than k={k}")
    weights = cq.linear_weights(df)
    X = df.select(cq.FEATS).to_numpy().astype(float)
    y = df["PlayResult"].replace_strict(cq.LABEL_MAP, return_dtype=pl.Int64).to_numpy()
    model = cq.ContactQualityModel().fit(X, y, weights, k, alpha)
    model.meta = {"level": level, "year": year, "n_rows": df.height, "feats": cq.FEATS,
                  "verified_only": skipped is not None, "unverified_rows_skipped": skipped or 0}
    model.save(_base(level, year))
    # Every cached xRV came out of the OLD model, so it is stale the instant the model changes.
    _xrv_path(level, year).unlink(missing_ok=True)
    return model


# ── xRV output cache ─────────────────────────────────────────────────────────────────────────────
# The model is a LAZY learner: the artifact IS its ~270k-point reference set, so a "prediction" is a
# fresh 800-neighbour search — ~20s for a season, and it dominates every consumer's cost. Those
# answers are deterministic, so the OUTPUTS are worth storing as well as the model:
# ``xrv_{level}_{year}.parquet`` holding PitchUID -> xRV, beside the model it came from.
#
# Invalidated ONLY by a retrain (train() deletes it above). New games do NOT invalidate it — the
# table is keyed by PitchUID, so an unseen ball simply misses and is scored live by the caller.
def _xrv_path(level: str, year: str) -> Path:
    return ARTIFACT_DIR / f"xrv_{level.replace(' ', '')}_{year}.parquet"


def xrv_exists(level: str, year: str) -> bool:
    return _xrv_path(level, year).exists()


def _atomic_write(df: pl.DataFrame, path: Path) -> None:
    """Temp file + os.replace, so a concurrent reader never sees a half-written parquet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)


def scorable_balls(level: str, year: str) -> pl.DataFrame:
    """Every ball delispice_app can score for (level, year): PitchUID + the three launch features.

    Deliberately selected the way the APP selects rows, which is wider than the TRAINING set twice
    over:
      * training drops Error/Sacrifice/FieldersChoice/... (``cq.DROP_RESULTS``), but the app scores
        any ball in play with complete launch data — 21,789 extra balls in D1 2025 alone;
      * BOTH physical partitions are scanned and filtered on Level, because a level's rows are not
        confined to its own partition (1,479 D1 balls sit under ``Others/`` in 2025). Globbing only
        ``{level}/{year}/``, as ``_events`` does for training, would leave those permanently uncached.

    De-duplicated on PitchUID: those cross-partition rows are the SAME pitch written into both
    partitions (verified identical launch values), and the result is joined on PitchUID downstream,
    where a duplicate key would fan out rows and double-count the ball.
    """
    from backend.models import contact_quality as cq
    cols = ["PitchUID", "ExitSpeed", "Angle", "Direction"]
    files = sorted(cq.PIPELINE.glob(f"*/{year}/**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for year={year} under {cq.PIPELINE}")
    scope_col = "League" if level == cq.P4_LEVEL else "Level"
    scope = (pl.col("League").is_in(cq.P4_LEAGUES) if level == cq.P4_LEVEL
             else pl.col("Level") == level)
    return (pl.scan_parquet([str(f) for f in files])
              .select([*cols, "PitchCall", scope_col])
              .filter((pl.col("PitchCall") == "InPlay") & pl.col("PitchUID").is_not_null()
                      & pl.col("ExitSpeed").is_not_null() & pl.col("Angle").is_not_null()
                      & pl.col("Direction").is_not_null() & scope)
              .select(cols).unique(subset=["PitchUID"]).collect())


def build_xrv_cache(level: str, year: str) -> tuple[Path, int]:
    """Score every scorable ball once and store PitchUID -> xRV. Needs a trained model."""
    model = load(level, year)
    if model is None:
        raise FileNotFoundError(f"no cq artifact for level={level} year={year} — train it first")
    balls = scorable_balls(level, year)
    path = _xrv_path(level, year)
    if balls.height == 0:
        _atomic_write(pl.DataFrame(schema={"PitchUID": pl.Utf8, "xrv": pl.Float64}), path)
        return path, 0
    X = balls.select(["ExitSpeed", "Angle", "Direction"]).to_numpy().astype(float)
    _atomic_write(balls.select("PitchUID").with_columns(pl.Series("xrv", model.predict_xrv(X))), path)
    return path, balls.height


def xrv_lookup(level: str, year: str) -> pl.DataFrame | None:
    """PitchUID -> xRV for (level, year); None when the cache has not been built."""
    path = _xrv_path(level, year)
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path)
    except Exception as e:            # a truncated/corrupt cache must never break scoring
        print(f"xrv cache {path.name} unreadable, scoring live instead: {e}")
        return None


def available(level: str) -> list[str]:
    """Years trainable for a level: present in the re-matrix AND having data on disk."""
    from backend.models import contact_quality as cq
    rem = pl.read_parquet(cq.REM_PATH).filter(pl.col("Level") == level)
    part = "D1" if level == cq.P4_LEVEL else (level if (cq.PIPELINE / level).exists() else "Others")
    return sorted(y for y in rem["year"].unique().to_list() if (cq.PIPELINE / part / y).is_dir())


def main(argv=None):
    print(config.data_mode(), flush=True)
    from backend.models.contact_quality import Refused
    p = argparse.ArgumentParser(description="Train contact-quality (xRV) artifacts.")
    p.add_argument("--level", default="D1", help="Level to train (default: D1)")
    p.add_argument("--years", nargs="*", default=None,
                   help="Years to train (default: every year with a re-matrix + data)")
    p.add_argument("--k", type=int, default=800)
    p.add_argument("--alpha", type=float, default=0.1)
    p.add_argument("--xrv", action="store_true",
                   help="also build the PitchUID->xRV output cache (~20s/season; makes every "
                        "later report and leaderboard build skip the k-NN search entirely)")
    p.add_argument("--xrv-only", action="store_true",
                   help="skip training; only (re)build the xRV cache from the existing models")
    args = p.parse_args(argv)
    years = args.years or available(args.level)
    if not years:
        raise SystemExit(f"nothing trainable for level={args.level}")
    failed = []
    for yr in years:                  # one year failing doesn't stop the others
        try:
            if not args.xrv_only:
                print(f"training cq {args.level} {yr} …", flush=True)
                m = train(args.level, yr, k=args.k, alpha=args.alpha)
                print(f"  saved {_base(args.level, yr).name} ({m.meta['n_rows']:,} batted balls)")
            if args.xrv or args.xrv_only:
                print(f"caching xRV {args.level} {yr} …", flush=True)
                path, n = build_xrv_cache(args.level, yr)
                print(f"  saved {path.name} ({n:,} balls scored)")
        except Refused as e:          # by design (thin level / incomplete RE288): not a failure
            print(f"  SKIPPED {args.level} {yr}: {e}", flush=True)
        except Exception as e:
            print(f"  FAILED {args.level} {yr}: {type(e).__name__}: {e}", flush=True)
            failed.append(yr)
    if failed:
        raise SystemExit(f"cq {args.level}: failed for {failed}")


if __name__ == "__main__":
    main()
