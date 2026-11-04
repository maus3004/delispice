"""Artifact store + CLI for Stuff+ (``stuff_v1.py``, now v1.1), the same split as ``eye_store``/``eye_v1``.

One model covers all of D1 (trained on 2022-26 pooled, not per season), and it is retrained ONCE A
SEASON with a version bump (PR3). The Stuff+ scale is frozen inside it, so grades don't move between
retrains. What changes nightly is the score tables: new games get tagged with the frozen pitch-group
templates and graded.

  * Train offline:   python -m backend.models.stuff_store --train
    v1.1 grades every pitch against 16 hitter prototypes (both views at once), so grading is ~16x v1's
    (about 6 h on the server). Memory peaks at the targets + features step; grading is chunked and flat.
    Artifacts land in ``pipeline_v2/artifacts/`` as ``stuff_D1_{version}_{group}_{piece}.txt`` (9 LightGBM
    files) + ``stuff_D1_{version}.json`` (scale, prices, context weights, prototypes, P4 correction,
    templates, ...) + ``stuff_D1_{version}_pids.parquet`` (the training pitcher-ID merge). Git-ignored.
    Training also writes every season's score table, ``stuffscore_D1_{version}_{year}.parquet`` (v1's
    tables have no version in the name, so a v1.1 train never touches what a v1 app is serving).
  * Re-score from the existing model:   python -m backend.models.stuff_store --score-only --years 2026
  * Add the vs-RHB / vs-LHB columns to tables built before they existed, without changing any grade:
    python -m backend.models.stuff_store --add-splits --years 2026
  * Re-set the frozen scale (PR2) without retraining or re-grading, re-mapping every saved grade:
    python -m backend.models.stuff_store --rescale              (stuff_v1.SCALE_METHOD; or name one:
    --rescale per_pitch / per_pitch_type). Then restart the app.
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
    v = "" if stuff_v1.MODEL_VERSION == "v1" else f"{stuff_v1.MODEL_VERSION}_"      # v1's tables predate the version tag
    return ARTIFACT_DIR / f"stuffscore_{stuff_v1.LEVEL}_{v}{year}{'_smoke' if smoke else ''}.parquet"


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


class _Watch:
    """``--notify``: Discord messages (pipeline_v2.notify, whose webhook lives in pipeline_v2/.env) when training
    starts, after every step and grading block, and when it finishes or fails, plus a memory watchdog. Every
    ``every`` seconds it reads this process's memory plus its worker processes' (the tagging pool); each
    threshold in ``alert_gb`` @-mentions once, and so does the machine's free memory dropping under
    ``low_avail_gb``. A notification never stops training (``notify.send`` never raises). A process the
    kernel kills (OOM) can't report itself, so run it under a shell wrapper that posts the exit code."""

    def __init__(self, title: str, alert_gb=(12.0, 13.0), low_avail_gb: float = 1.5, every: int = 30):
        import threading
        self.title, self.alert, self.low, self.every = title, sorted(alert_gb), low_avail_gb, every
        self.fired, self.peak, self.t0 = set(), 0.0, None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @staticmethod
    def _now():
        import psutil
        me = psutil.Process()
        rss = me.memory_info().rss
        for c in me.children(recursive=True):
            try:
                rss += c.memory_info().rss
            except psutil.Error:
                pass
        return rss / 2**30, psutil.virtual_memory().available / 2**30

    def _fields(self, used=None, avail=None):
        import time
        if used is None:
            used, avail = self._now()
        return {"elapsed": f"{(time.time() - self.t0) / 60:.0f} min", "memory now": f"{used:.1f} GB",
                "peak so far": f"{max(self.peak, used):.1f} GB", "machine free": f"{avail:.1f} GB"}

    def _run(self):
        from pipeline_v2 import notify
        while not self.stop.wait(self.every):
            try:
                used, avail = self._now()
            except Exception:
                continue
            self.peak = max(self.peak, used)
            for gb in self.alert:
                if used >= gb and gb not in self.fired:
                    self.fired.add(gb)
                    notify.send(f"{self.title}: memory at {used:.1f} GB (alert at {gb:g})", "", "warn", self._fields(used, avail))
            if avail < self.low and "low" not in self.fired:
                self.fired.add("low")
                notify.send(f"{self.title}: machine down to {avail:.1f} GB free", "", "warn", self._fields(used, avail))

    def hook(self, msg: str, _elapsed: float):
        from pipeline_v2 import notify
        notify.send(f"{self.title}: {msg}"[:250], msg if len(msg) > 200 else "", "info", self._fields())

    def __enter__(self):
        import time
        from pipeline_v2 import notify
        self.t0 = time.time()
        self.thread.start()
        notify.send(f"{self.title}: started", "", "info", self._fields())
        return self

    def __exit__(self, kind, err, tb):
        import traceback
        from pipeline_v2 import notify
        self.stop.set()
        if kind is None:
            notify.send(f"{self.title}: done", "", "ok", self._fields())
        else:
            tail = "".join(traceback.format_exception(kind, err, tb))[-1500:]
            notify.send(f"{self.title}: FAILED ({kind.__name__})", f"```{tail}```", "error", self._fields())
        return False


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


def add_side_splits(year: str) -> tuple[Path, int, int]:
    """Add the vs-RHB / vs-LHB grades to an existing score table, keeping its grades as they are.
    The season is re-tagged and re-graded with the frozen model, and a pitch takes the new splits only
    where that reproduces its stored Stuff+ (same group, within 0.01). A one-season re-score merges a
    few pitcher IDs and retags a few pitches differently from training (~0.1%); those get null splits
    rather than splits that don't match their grade. Returns (path, pitches, pitches with splits)."""
    from backend.models import stuff_v1
    from backend.models.cq_store import _atomic_write
    model = load()
    if model is None:
        raise FileNotFoundError("no stuff artifact; train it first: python -m backend.models.stuff_store --train")
    path = _score_path(year)
    old = pl.read_parquet(path)
    sides = stuff_v1.StuffModel.SIDE_COLS
    new = (stuff_v1.score(stuff_v1.load_pitches([year], with_outcomes=False), model).unique(subset=["PitchUID"])
           .select("PitchUID", pl.col("group").alias("_g"), pl.col("stuff").alias("_s"), *sides))
    keep = (pl.col("group") == pl.col("_g")) & ((pl.col("stuff") - pl.col("_s")).abs() < 0.01)
    out = (old.drop(sides, strict=False).join(new, on="PitchUID", how="left")
              .with_columns(*[pl.when(keep).then(pl.col(c)).alias(c) for c in sides])
              .drop("_g", "_s"))
    _atomic_write(out, path)
    return path, out.height, out.height - out["stuff_rhb"].null_count()


def rescale(method: str | None = None) -> dict:
    """Re-set the frozen Stuff+ scale (PR2) without retraining or re-grading. Each grade is linear in the
    model's raw output, so the raw values are recovered from the saved grades, the new mean/SD are set
    on them (by pitch type: pitcher x season x pitch, the training pitcher-ID merge), the model's JSON is
    rewritten (the boosters aren't touched) and every season's table is re-mapped. Running it with the
    previous method puts the old numbers back."""
    import copy
    import time
    from backend.models import stuff_v1
    from backend.models.cq_store import _atomic_write
    method = method or stuff_v1.SCALE_METHOD
    model = load()
    if model is None:
        raise FileNotFoundError("no stuff artifact; train it first: python -m backend.models.stuff_store --train")
    years = [y for y in stuff_v1.available_years() if _score_path(y).exists()]
    tables = {y: pl.read_parquet(_score_path(y)) for y in years}
    G = pl.concat([t.select("PitchUID", "group", "retag", *[n for _, n, _ in model.PLUS])
                    .with_columns(Year=pl.lit(int(y), pl.Int32)) for y, t in tables.items()])
    ids, _ = stuff_v1.merge_pitcher_ids(stuff_v1.load_pitches(years, with_outcomes=False, columns=[
        "PitcherId", "Pitcher", "PitcherThrows", "PitcherTeam", "RelHeight", "RelSide"]))   # the merge's inputs
    P = G.select("PitchUID", "Year", "group", "retag").join(ids.select("PitchUID", "pid").unique("PitchUID"),
                                                            on="PitchUID", how="left")
    old, old_method = copy.deepcopy(model.scale), model.meta.get("scale_method", "per_pitch")
    model.set_scale(P, model.unscale(G), method)
    model.meta["rescaled"] = time.strftime("%Y-%m-%d %H:%M")
    model.meta["previous_scale"] = {"method": old_method, "scale": old}
    model.save(_base(), boosters=False)
    for y, t in tables.items():
        _atomic_write(model.remap(t, old), _score_path(y))
    return {"method": method, "years": years, "pitches": G.height, "no_pid": int(P["pid"].null_count()),
            "scale": model.scale}


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
    p = argparse.ArgumentParser(description="Train / re-score Stuff+ (D1).")
    p.add_argument("--train", action="store_true", help="train the model on --years (default: all) and score them")
    p.add_argument("--score-only", action="store_true", help="re-score --years from the existing model")
    p.add_argument("--add-splits", action="store_true",
                   help="add the vs-RHB / vs-LHB columns to --years' existing tables (grades unchanged)")
    p.add_argument("--rescale", nargs="?", const="", choices=["", "per_pitch", "per_pitch_type"], default=None,
                   help="re-set the Stuff+ scale and re-map every saved grade, no retraining "
                        "(default: stuff_v1.SCALE_METHOD)")
    p.add_argument("--years", nargs="*", default=None, help="seasons (default: every season on disk)")
    p.add_argument("--fraction", type=float, default=1.0, help="smoke test: train on this share of pitchers")
    p.add_argument("--notify", action="store_true",
                   help="--train only: Discord progress + memory alerts (webhook from pipeline_v2/.env)")
    p.add_argument("--mem-alert", type=float, nargs="+", default=[12.0, 13.0],
                   help="--notify: GB of memory (this process + its workers) that each trigger one alert")
    args = p.parse_args(argv)
    from backend.models import stuff_v1
    years = args.years or stuff_v1.available_years()
    if args.train:
        import contextlib
        title = f"Stuff+ {stuff_v1.MODEL_VERSION} train" + (" (smoke)" if args.fraction < 1 else "")
        watch = _Watch(title, args.mem_alert) if args.notify else contextlib.nullcontext()
        with watch:
            if args.notify:
                stuff_v1.LOG_HOOK = watch.hook
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
    elif args.rescale is not None:
        out = rescale(args.rescale or None)
        print(f"rescaled to {out['method']}: {out['pitches']:,} pitches in {', '.join(out['years'])} "
              f"({out['no_pid']:,} without a pitcher ID, left out of the scale)")
        for g, s in out["scale"].items():
            print(f"  {g}: " + ", ".join(f"{c} mean {m:+.4f} SD {sd:.4f}" for c, (m, sd) in s.items()))
        print("restart the app (or HUP it) so it reloads the model and tables", flush=True)
    elif args.add_splits:
        for y in years:
            path, n, k = add_side_splits(y)
            print(f"saved {path.name}: splits on {k:,} of {n:,} pitches ({n - k:,} left blank)", flush=True)
    else:
        p.error("pass --train, --score-only, --add-splits or --rescale")


if __name__ == "__main__":
    main()
