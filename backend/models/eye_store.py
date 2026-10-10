"""Artifact store + training CLI for the batter EYE metric (``eye_v1.py``).

Same split as ``cq_store``/``contact_quality``: the model lives in ``eye_v1.py``, persistence and
the CLI live here. Three LightGBM fits over a full season take minutes, and the score of ONE pitch
depends on the league-wide policy at that location — which delispice_app, scanning a single player's
rows, cannot see. So nothing is ever trained at request time:

  * Train offline, once per (Level, Year):   python -m backend.models.eye_store --eye
    Artifacts land in ``backend/models/artifacts/`` as ``eye_{level}_{year}_{ump,swing,ev}.txt``
    + ``.json`` (small; git-ignored — build them on each machine, the data is already there).
  * Serve from the artifact: ``load(level, year)`` -> fitted EyeModel (or None if not trained),
    plus ``eye_lookup(level, year)`` -> the precomputed PitchUID -> eye table.

**Run environment is BAKED IN (v1).** Unlike RV — where the state transition is a fact and the RE
matrix is applied at read time — an eye score depends on the matrix twice over (the count run values
AND the run values the EV model was trained on), so an artifact holds one finished number per pitch
in its own (Level, Year) environment. The app's "Change run environment" picker therefore does not
re-value this column. Splitting it would mean storing p_strike/p_swing/ev_swing/swung per pitch and
recombining at request time; deferred to v2.

Retrain when a season's data grows (new games): re-run the CLI and restart the app. Between runs,
new pitches simply miss the cache and are scored live from the model (see ``data._eye_for``), the
same way xRV handles a night's new games.

Build:  python -m backend.models.eye_store --eye                    # every level with a re-matrix
        python -m backend.models.eye_store --level D1 --years 2026 --eye
        python -m backend.models.eye_store --level D1 --eye-only    # rescore from existing models
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

from pipeline_v2 import config


ARTIFACT_DIR = config.ARTIFACTS_DIR       # pipeline_v2/artifacts, or backend/models/artifacts with DELISPICE_DATA=old


def _base(level: str, year: str) -> Path:
    return ARTIFACT_DIR / f"eye_{level.replace(' ', '')}_{year}"


def exists(level: str, year: str) -> bool:
    b = _base(level, year)
    return (b.with_name(b.name + ".json").exists()
            and all(b.with_name(f"{b.name}_{k}.txt").exists() for k in ("ump", "swing", "ev")))


def load(level: str, year: str):
    """Fitted EyeModel for (level, year), or None if no artifact has been trained."""
    if not exists(level, year):
        return None
    from backend.models import eye_v1
    return eye_v1.EyeModel.load(_base(level, year))


def train(level: str, year: str):
    """Fit + save the (level, year) model."""
    from backend.models import eye_v1
    model = eye_v1.train(level, year)
    model.save(_base(level, year))
    # Every cached score came out of the OLD model, so it is stale the instant the model changes.
    _eye_path(level, year).unlink(missing_ok=True)
    return model


# ── eye output cache ─────────────────────────────────────────────────────────────────────────────
# Unlike xRV's k-NN, these are trees — scoring is fast, so the cache is a convenience rather than a
# necessity. It still earns its place: the leaderboard joins ~2M scores inside DuckDB, which wants a
# parquet on disk, not a Python model. Invalidated ONLY by a retrain (train() deletes it above); new
# games do NOT invalidate it, since the table is keyed by PitchUID and an unseen pitch simply misses.
def _eye_path(level: str, year: str) -> Path:
    return ARTIFACT_DIR / f"eyescore_{level.replace(' ', '')}_{year}.parquet"


def eye_exists(level: str, year: str) -> bool:
    return _eye_path(level, year).exists()


def scorable_pitches(level: str, year: str) -> pl.DataFrame:
    """Every pitch delispice_app can score for (level, year), prepared for the model.

    Identical population to training — for eye these genuinely are the same set, because the model
    trains on every competitive decision rather than a filtered subset the way contact quality
    drops Error/Sacrifice/etc."""
    from backend.models import eye_v1
    return eye_v1.prepare(eye_v1.load_pitches(level, year))


def build_eye_cache(level: str, year: str) -> tuple[Path, int]:
    """Score every scorable pitch once and store PitchUID -> eye. Needs a trained model."""
    from backend.models.cq_store import _atomic_write
    model = load(level, year)
    if model is None:
        raise FileNotFoundError(f"no eye artifact for level={level} year={year} — train it first")
    d = scorable_pitches(level, year)
    path = _eye_path(level, year)
    if d.height == 0:
        _atomic_write(pl.DataFrame(schema={"PitchUID": pl.Utf8, "eye": pl.Float32}), path)
        return path, 0
    _atomic_write(d.select("PitchUID").with_columns(model.predict_eye(d).cast(pl.Float32)), path)
    return path, d.height


def eye_lookup(level: str, year: str) -> pl.DataFrame | None:
    """PitchUID -> eye for (level, year); None when the cache has not been built.

    Uniqued on the join key defensively: a duplicate PitchUID would fan out the join downstream and
    double-count the pitch."""
    path = _eye_path(level, year)
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path).unique(subset=["PitchUID"])
    except Exception as e:            # a truncated/corrupt cache must never break scoring
        print(f"eye cache {path.name} unreadable, scoring live instead: {e}")
        return None


def available(level: str) -> list[str]:
    """Years trainable for a level: present in the re-matrix AND having data on disk."""
    from backend.models import contact_quality as cq
    from backend.models import eye_v1
    rem = pl.read_parquet(eye_v1.REM_PATH).filter(pl.col("Level") == level)
    part = "D1" if level == cq.P4_LEVEL else (level if (eye_v1.PIPELINE / level).exists() else "Others")
    return sorted(y for y in rem["year"].unique().to_list() if (eye_v1.PIPELINE / part / y).is_dir())


def main(argv=None):
    print(config.data_mode(), flush=True)
    from backend.models.contact_quality import Refused
    p = argparse.ArgumentParser(description="Train batter eye (swing-decision) artifacts.")
    p.add_argument("--level", default="D1", help="Level to train (default: D1)")
    p.add_argument("--years", nargs="*", default=None,
                   help="Years to train (default: every year with a re-matrix + data)")
    p.add_argument("--eye", action="store_true",
                   help="also build the PitchUID->eye score cache (the app and leaderboard read it)")
    p.add_argument("--eye-only", action="store_true",
                   help="skip training; only (re)build the score cache from the existing models")
    args = p.parse_args(argv)
    years = args.years or available(args.level)
    if not years:
        raise SystemExit(f"nothing trainable for level={args.level}")
    failed = []
    for yr in years:                  # one year failing doesn't stop the others
        try:
            if not args.eye_only:
                print(f"training eye {args.level} {yr} …", flush=True)
                m = train(args.level, yr)
                print(f"  saved {_base(args.level, yr).name} "
                      f"({m.meta['n_rows']:,} decisions, {m.meta['n_swings']:,} swings)")
            if args.eye or args.eye_only:
                print(f"caching eye {args.level} {yr} …", flush=True)
                path, n = build_eye_cache(args.level, yr)
                print(f"  saved {path.name} ({n:,} pitches scored)")
        except Refused as e:          # by design (thin level / incomplete RE288): not a failure
            print(f"  SKIPPED {args.level} {yr}: {e}", flush=True)
        except Exception as e:
            print(f"  FAILED {args.level} {yr}: {type(e).__name__}: {e}", flush=True)
            failed.append(yr)
    if failed:
        raise SystemExit(f"eye {args.level}: failed for {failed}")


if __name__ == "__main__":
    main()
