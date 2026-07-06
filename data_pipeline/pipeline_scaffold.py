"""CSV -> fixes -> type coercion -> validation -> Parquet  (Trackman).

Per-file flow:
  1. READ every column as a STRING (no type inference) so raw values survive intact.
  2. CAST to the canonical Polars dtypes (``trackman_schema.pl_csv_schema``). strict=False,
     so a value that cannot be cast becomes null instead of crashing -- those are logged
     per file so bad values are surfaced, not hidden.
  3. ADD MISSING COLUMNS for this file as typed nulls and reorder to the canonical
     201-column order (the key order of ``trackman_schema.pl_csv_schema``).
  4. FIX with ``fix_dictionary.apply_fixes``: typo remaps, junk -> null / "Undefined",
     and drop rows with impossible integer counts.
  5. VALIDATE with the Pandera POLARS schema (``trackman_pandera_schema.TRACKMAN_SCHEMA``)
     -- validated natively on the Polars frame, no pandas conversion.
  6. WRITE Parquet atomically.
  7. ARCHIVE the source CSV into ``data/cleaned/`` so it is not reprocessed on the next run.

Processed CSVs are moved to ``data/cleaned/`` (a subfolder of the input dir, skipped by the top-level
``*.csv`` glob); only un-processed CSVs sit directly in ``data/``, so each run picks up just the new
files. Files that error at any step (including validation) are copied to quarantine and left in
``data/`` (so they are retried next run). Files are processed in parallel -- one worker process per
CPU, one polars thread each.

Requires (one venv): polars, pandera>=0.32, numpy, pandas. numpy+pandas are needed only by
Pandera to build failure-case reports, even though validation runs on the Polars backend.

``trackman_schema.py``, ``trackman_pandera_schema.py`` and ``fix_dictionary.py`` must be
importable -- they sit next to this file.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

# Cap polars to one thread per process; the process pool below provides the parallelism.
# Must be set BEFORE `import polars`. setdefault lets you override via the environment.
os.environ.setdefault("POLARS_MAX_THREADS", "1")

import polars as pl
import pandera.polars as pa

# Make the sibling definition modules importable regardless of the current directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trackman_schema import pl_csv_schema            # {col: pl.DataType} in canonical order
from trackman_pandera_schema import TRACKMAN_SCHEMA  # pandera.polars DataFrameSchema (coerce=False)
from fix_dictionary import apply_fixes               # remap typos, null junk, drop corrupt-int rows


@dataclass(frozen=True)
class PipelinePaths:
    input_dir: Path
    output_dir: Path
    quarantine_dir: Path
    log_dir: Path
    cleaned_dir: Path          # processed source CSVs are moved here (e.g. data/cleaned)


def setup_logging(log_dir: Path, log_name: str = "pipeline.log") -> logging.Logger:
    """Create console + file logging."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("trackman_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_dir / log_name)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def ensure_directories(paths: PipelinePaths) -> None:
    """Create runtime directories if they do not exist."""
    for p in [paths.input_dir, paths.output_dir, paths.quarantine_dir, paths.log_dir, paths.cleaned_dir]:
        p.mkdir(parents=True, exist_ok=True)


def read_csv_as_strings(csv_path: Path) -> pl.DataFrame:
    """Read every column as Utf8 (no type inference).

    Only the empty field "" is treated as null, so any other unexpected token (e.g. "NA",
    ",", a typo) stays literal and is caught later by cast logging or Pandera validation.
    """
    return pl.read_csv(csv_path, infer_schema_length=0, null_values=[""])


def cast_types(
    df: pl.DataFrame,
    type_dict: Mapping[str, pl.DataType],
    logger: logging.Logger,
    file_name: str,
) -> pl.DataFrame:
    """Cast string columns to their canonical Polars dtypes.

    strict=False keeps the run going if a value cannot be parsed (it becomes null), but we
    first count those uncastable-but-non-null values per column and log them, so bad values
    are surfaced rather than silently dropped.
    """
    numeric = [(c, dt) for c, dt in type_dict.items() if c in df.columns and dt != pl.Utf8]
    if numeric:
        fail_counts = df.select([
            (pl.col(c).is_not_null() & pl.col(c).cast(dt, strict=False).is_null()).sum().alias(c)
            for c, dt in numeric
        ]).to_dicts()[0]
        bad = {c: n for c, n in fail_counts.items() if n}
        if bad:
            logger.warning("%s: %d column(s) had uncastable values (set to null): %s",
                           file_name, len(bad), bad)

    return df.with_columns([
        pl.col(c).cast(dt, strict=False).alias(c)
        for c, dt in type_dict.items() if c in df.columns
    ])


def add_missing_columns(
    df: pl.DataFrame,
    type_dict: Mapping[str, pl.DataType],
    logger: logging.Logger,
    file_name: str,
) -> pl.DataFrame:
    """Add any schema columns absent from this file as typed nulls, then reorder.

    The output always has exactly the columns of ``type_dict`` in its key order (the
    canonical order from trackman_schema.py), so every Parquet shares one schema/layout.
    Unexpected extra columns are dropped (and logged).
    """
    extra = [c for c in df.columns if c not in type_dict]
    if extra:
        logger.warning("%s: dropping %d unexpected column(s): %s", file_name, len(extra), extra)

    missing = [c for c in type_dict if c not in df.columns]
    if missing:
        df = df.with_columns([pl.lit(None, dtype=type_dict[c]).alias(c) for c in missing])

    return df.select(list(type_dict.keys()))


def validate_polars(
    df: pl.DataFrame,
    schema: pa.DataFrameSchema,
    logger: logging.Logger,
    file_name: str,
) -> None:
    """Validate the cleaned frame with the Pandera POLARS API (no pandas conversion).

    lazy=True collects every failure. On failure we log a per-(column, check) summary and
    re-raise so the caller quarantines the file.
    """
    try:
        schema.validate(df, lazy=True)
    except pa.errors.SchemaErrors as exc:
        summary = (exc.failure_cases
                   .group_by("column", "check").len()
                   .sort("len", descending=True))
        logger.error("%s: validation FAILED (%d failure cases)", file_name, exc.failure_cases.height)
        for r in summary.head(15).iter_rows(named=True):
            logger.error("    %6d  %-22s %s", r["len"], r["column"], r["check"])
        raise


def atomic_write_parquet(df: pl.DataFrame, output_path: Path) -> None:
    """Write to a temp file first, then move into place."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    df.write_parquet(tmp_path)
    tmp_path.replace(output_path)


def archive_source(csv_path: Path, cleaned_dir: Path) -> None:
    """Move a successfully-processed source CSV into cleaned_dir (atomic; overwrites any prior copy).

    This is the processed-files ledger: anything in cleaned_dir is done, anything still in the input
    dir is unprocessed. cleaned_dir is a subfolder of the input dir, so the top-level glob skips it."""
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    os.replace(csv_path, cleaned_dir / csv_path.name)


def quarantine_file(source_csv: Path, quarantine_dir: Path, reason: str) -> Path:
    """Copy a bad input file to quarantine with a timestamped name + reason note."""
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = quarantine_dir / f"{source_csv.stem}__{stamp}{source_csv.suffix}"
    shutil.copy2(source_csv, target)

    note = target.with_suffix(target.suffix + ".txt")
    note.write_text(reason + "\n", encoding="utf-8")
    return target


def process_one_csv(
    csv_path: Path,
    output_dir: Path,
    quarantine_dir: Path,
    cleaned_dir: Path,
    type_dict: Mapping[str, pl.DataType],
    schema: pa.DataFrameSchema,
    logger: logging.Logger,
) -> bool:
    """Run the full pipeline for one CSV. Returns True on success, False if quarantined.

    On success the source CSV is moved into ``cleaned_dir`` so it is not reprocessed next run."""
    logger.info("Processing %s", csv_path.name)
    try:
        df = read_csv_as_strings(csv_path)                                  # 1 read as strings
        df = cast_types(df, type_dict, logger, csv_path.name)               # 2 cast (+ log bad)
        df = add_missing_columns(df, type_dict, logger, csv_path.name)      # 3 add missing + order
        df = apply_fixes(df)                                                # 4 fixes + quarantine rows
        validate_polars(df, schema, logger, csv_path.name)                 # 5 validate (polars)

        output_path = output_dir / f"{csv_path.stem}.parquet"
        atomic_write_parquet(df, output_path)                              # 6 write
        archive_source(csv_path, cleaned_dir)                              # 7 mark processed
        logger.info("Wrote %s (%d rows, %d cols); archived source -> cleaned/",
                    output_path.name, df.height, df.width)
        return True

    except Exception as exc:  # noqa: BLE001
        reason = f"{csv_path.name}: {type(exc).__name__}: {exc}"
        quarantine_file(csv_path, quarantine_dir, reason)
        logger.exception("Quarantined %s", csv_path.name)
        return False


# --- parallel workers -------------------------------------------------------
_WORKER_LOGGER: logging.Logger | None = None


def _init_worker(log_dir: str) -> None:
    """Runs once per worker process: give each process its own log file."""
    global _WORKER_LOGGER
    _WORKER_LOGGER = setup_logging(Path(log_dir), log_name=f"worker_{os.getpid()}.log")


def _process_one(csv_path: str, output_dir: str, quarantine_dir: str, cleaned_dir: str) -> bool:
    """Top-level (picklable) worker. Reuses process_one_csv and pulls type_dict / schema
    from module globals so the schema isn't re-pickled for every task."""
    return process_one_csv(
        Path(csv_path), Path(output_dir), Path(quarantine_dir), Path(cleaned_dir),
        pl_csv_schema, TRACKMAN_SCHEMA, _WORKER_LOGGER,
    )


def process_all_csvs(
    paths: PipelinePaths,
    type_dict: Mapping[str, pl.DataType] | None = None,   # kept for API compat; workers use globals
    schema: pa.DataFrameSchema | None = None,             # kept for API compat; workers use globals
    max_workers: int | None = None,
) -> None:
    """Process every CSV in the input directory, one worker process per CPU."""
    ensure_directories(paths)
    logger = setup_logging(paths.log_dir, log_name="pipeline.log")  # parent / summary log

    csv_files = sorted(paths.input_dir.glob("*.csv"))
    if not csv_files:
        logger.warning("No CSV files found in %s", paths.input_dir)
        return

    max_workers = max_workers or os.cpu_count()
    logger.info("Processing %d files across %d workers", len(csv_files), max_workers)

    ok = bad = 0
    with ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=mp.get_context("spawn"),   # fresh interpreter per worker; avoids fork+polars hazards on Linux
        initializer=_init_worker,
        initargs=(str(paths.log_dir),),
    ) as executor:
        futures = {
            executor.submit(_process_one, str(p), str(paths.output_dir), str(paths.quarantine_dir),
                            str(paths.cleaned_dir)): p
            for p in csv_files
        }
        for fut in as_completed(futures):
            try:
                if fut.result():
                    ok += 1
                else:
                    bad += 1
            except Exception:  # a worker crashed outright (not a normal quarantine)
                bad += 1
                logger.exception("Worker crashed on %s", futures[fut].name)

    logger.info("Done. success=%d quarantined=%d", ok, bad)


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    paths = PipelinePaths(
        input_dir=base_dir / "data",
        output_dir=base_dir / "clean",
        quarantine_dir=base_dir / "quarantine",
        log_dir=base_dir / "logs",
        cleaned_dir=base_dir / "data" / "cleaned",   # processed CSVs moved here (skipped by *.csv glob)
    )
    process_all_csvs(paths, type_dict=pl_csv_schema, schema=TRACKMAN_SCHEMA)
