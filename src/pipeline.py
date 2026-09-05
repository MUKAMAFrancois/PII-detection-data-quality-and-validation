"""
pipeline.py
===========
Part 6 — End-to-end orchestration.

Runs every stage in order, logs to logs/pipeline.log, and writes
reports/pipeline_execution_report.txt.

Stages:
    1. load          raw CSV -> DataFrame
    2. profile       -> data_quality_report.txt
    3. detect PII    -> pii_detection_report.txt
    4. validate      -> validation_results.txt        (pre-cleaning)
    5. clean         -> customers_cleaned.csv, cleaning_log.txt
    6. re-validate   quality gate on the cleaned frame
    7. mask          -> customers_masked.csv, masked_sample.txt
    8. report        -> pipeline_execution_report.txt

The brief lists "Load -> Clean -> Validate -> Detect PII -> Mask". Two
deviations, both forced by dependencies:
  - PII detection runs on the RAW frame, because that is the data whose
    breach exposure is being assessed.
  - Validation runs twice. Part 3's deliverable is the raw assessment;
    Part 4 requires re-validation to prove the fixes landed.

Error handling: a failing critical stage (load, clean, mask) aborts the run;
a failing report stage is recorded and the pipeline continues, because a
broken report should not block the data products. Exit code is 0 only when
every critical stage succeeded and the cleaned output cleared the gate.

Usage:
    python -m src.pipeline
    python -m src.pipeline --input data/raw/other.csv --quiet
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import cleaner, masker, pii_detector, profiler, validator
from .config import (
    CLEANED_PATH,
    CLEANING_LOG_PATH,
    INPUT_ENCODING,
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    LOG_LEVEL,
    LOG_PATH,
    LOGS_DIR,
    MASKED_SAMPLE_PATH,
    MAX_ACCEPTABLE_FAILURE_RATE,
    PII_REPORT_PATH,
    PIPELINE_REPORT_PATH,
    PROJECT_ROOT,
    QUALITY_REPORT_PATH,
    QUARANTINE_PATH,
    RAW_PATH,
    REPORT_BORDER,
    REPORT_RULE,
    VALIDATION_REPORT_PATH,
    ensure_directories,
)

LOGGER_NAME = "pipeline"

# The brief's deliverable checklist. reflection.md is written by hand.
DELIVERABLES = [
    QUALITY_REPORT_PATH,
    PII_REPORT_PATH,
    VALIDATION_REPORT_PATH,
    CLEANING_LOG_PATH,
    MASKED_SAMPLE_PATH,
    PIPELINE_REPORT_PATH,
    CLEANED_PATH,
    PROJECT_ROOT / "reflection.md",
]


@dataclass
class Stage:
    name: str
    status: str = "pending"          # ok | failed | skipped
    seconds: float = 0.0
    detail: str = ""
    error: str = ""
    outputs: list[Path] = field(default_factory=list)


@dataclass
class Run:
    started: datetime = field(default_factory=datetime.now)
    stages: list[Stage] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)
    aborted_at: str = ""

    @property
    def failed(self) -> list[Stage]:
        return [s for s in self.stages if s.status == "failed"]

    def stage(self, name: str) -> Stage | None:
        return next((s for s in self.stages if s.name == name), None)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(quiet: bool = False) -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(LOG_LEVEL)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if not quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)

    return logger


# --------------------------------------------------------------------------- #
# Stage runner
# --------------------------------------------------------------------------- #

def _relay(logger: logging.Logger, stage: str, text: str) -> None:
    """Forward a module's own stdout into the log, one line per record."""
    for line in text.splitlines():
        if line.strip():
            logger.info("  [%s] %s", stage, line.strip())


def run_stage(run: Run, logger: logging.Logger, name: str, func,
              critical: bool = False):
    """Execute one stage, recording timing and any failure.

    Returns the stage's value, or None if it failed. A failing critical
    stage raises, because everything downstream depends on its output.
    """
    stage = Stage(name=name)
    run.stages.append(stage)
    logger.info("stage start: %s", name)
    started = time.perf_counter()

    # Each module prints a summary when run standalone. Capture it so the
    # orchestrated run emits one structured stream instead of two.
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            value = func(stage)
    except Exception as exc:
        stage.seconds = time.perf_counter() - started
        stage.status = "failed"
        stage.error = f"{type(exc).__name__}: {exc}"
        _relay(logger, name, captured.getvalue())
        logger.error("stage FAILED: %s (%s)", name, stage.error)
        logger.debug("traceback:\n%s", traceback.format_exc())
        if critical:
            run.aborted_at = name
            raise
        return None

    stage.seconds = time.perf_counter() - started
    stage.status = "ok"
    _relay(logger, name, captured.getvalue())
    logger.info("stage ok: %s (%.2fs) %s", name, stage.seconds, stage.detail)
    return value


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #

def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False,
                       encoding=INPUT_ENCODING)


def execute(run: Run, logger: logging.Logger, input_path: Path) -> None:
    # ---- 1. load ----
    def _load(stage: Stage):
        frame, notes = profiler.load_raw(input_path)
        stage.detail = (f"{len(frame)} rows, "
                        f"{notes['ragged_count']} ragged rows excluded")
        run.metrics["rows_raw"] = len(frame)
        run.metrics["ragged"] = notes["ragged_count"]
        return frame

    raw = run_stage(run, logger, "load", _load, critical=True)

    # ---- 2. profile ----
    def _profile(stage: Stage):
        path = profiler.run(input_path)
        stage.outputs = [path]
        stage.detail = f"-> {path.name}"
        return path

    run_stage(run, logger, "profile", _profile)

    # ---- 3. detect PII ----
    def _detect(stage: Stage):
        path = pii_detector.run(input_path)
        stage.outputs = [path]
        stage.detail = f"-> {path.name}"
        return path

    run_stage(run, logger, "detect_pii", _detect)

    # ---- 4. validate (pre-cleaning) ----
    def _validate_pre(stage: Stage):
        result = validator.validate_frame(raw)
        validator.run(input_path)
        stage.outputs = [VALIDATION_REPORT_PATH]
        stage.detail = (f"{len(result.failed_rows)} of {result.total_rows} "
                        f"rows failing")
        run.metrics["before"] = result
        return result

    before = run_stage(run, logger, "validate_pre", _validate_pre)

    # ---- 5. clean ----
    def _clean(stage: Stage):
        path = cleaner.run(input_path)
        stage.outputs = [path, CLEANING_LOG_PATH, QUARANTINE_PATH]
        frame = _read_csv(path)
        stage.detail = f"{len(frame)} rows written"
        run.metrics["rows_cleaned"] = len(frame)
        return frame

    cleaned = run_stage(run, logger, "clean", _clean, critical=True)

    # ---- 6. re-validate ----
    def _validate_post(stage: Stage):
        result = validator.validate_frame(cleaned)
        stage.detail = (f"{len(result.failed_rows)} of {result.total_rows} "
                        f"rows failing")
        run.metrics["after"] = result
        return result

    after = run_stage(run, logger, "validate_post", _validate_post)

    # ---- 7. mask ----
    def _mask(stage: Stage):
        path = masker.run(CLEANED_PATH)
        stage.outputs = [path, MASKED_SAMPLE_PATH]
        frame = _read_csv(path)
        stage.detail = f"{len(frame)} rows masked"
        run.metrics["rows_masked"] = len(frame)
        return frame

    run_stage(run, logger, "mask", _mask, critical=True)

    # ---- quality gate ----
    if after is not None:
        rate = 1.0 - after.pass_rate
        run.metrics["failure_rate"] = rate
        run.metrics["gate_passed"] = rate <= MAX_ACCEPTABLE_FAILURE_RATE
        logger.info("quality gate: %.1f%% failing against %.0f%% threshold -> %s",
                    100 * rate, 100 * MAX_ACCEPTABLE_FAILURE_RATE,
                    "PASS" if run.metrics["gate_passed"] else "FAIL")
    else:
        run.metrics["gate_passed"] = False

    if before is not None and after is not None:
        logger.info("failures %d -> %d", len(before.failures),
                    len(after.failures))


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def format_report(run: Run, input_path: Path, succeeded: bool) -> list[str]:
    finished = datetime.now()
    elapsed = (finished - run.started).total_seconds()

    out = [
        REPORT_BORDER,
        "PIPELINE EXECUTION REPORT",
        f"Started : {run.started:%Y-%m-%d %H:%M:%S}",
        f"Finished: {finished:%Y-%m-%d %H:%M:%S}   ({elapsed:.1f}s)",
        f"Input   : {input_path}",
        f"Status  : {'SUCCESS' if succeeded else 'FAILED'}",
        REPORT_BORDER,
    ]

    # ---- 1. stages ----
    out += ["", REPORT_RULE, "1. STAGES", REPORT_RULE]
    out.append(f"  {'stage':<16} {'status':<8} {'seconds':>8}  detail")
    for stage in run.stages:
        out.append(f"  {stage.name:<16} {stage.status:<8} "
                   f"{stage.seconds:>8.2f}  {stage.detail}")
        if stage.error:
            out.append(f"    error: {stage.error}")
    if run.aborted_at:
        out.append(f"\n  Run aborted at critical stage '{run.aborted_at}'; "
                   f"later stages did not execute.")

    # ---- 2. data flow ----
    out += ["", REPORT_RULE, "2. DATA FLOW", REPORT_RULE]
    rows_raw = run.metrics.get("rows_raw")
    rows_cleaned = run.metrics.get("rows_cleaned")
    rows_masked = run.metrics.get("rows_masked")

    def _line(label: str, value) -> str:
        # "not reached" is not the same as zero: a stage that never ran must
        # not be reported as having produced nothing.
        return f"  {label:<26} : {value if value is not None else 'not reached'}"

    out.append(_line("rows read from source", rows_raw))
    out.append(_line("ragged rows excluded", run.metrics.get("ragged")))
    out.append(_line("rows after cleaning", rows_cleaned))
    if rows_raw is not None and rows_cleaned is not None:
        out.append(_line("rows dropped / quarantined", rows_raw - rows_cleaned))
    out.append(_line("rows in masked extract", rows_masked))
    if rows_raw and rows_cleaned is not None:
        out.append(f"  {'retention':<26} : "
                   f"{100.0 * rows_cleaned / rows_raw:.1f}%")

    # ---- 3. quality ----
    out += ["", REPORT_RULE, "3. QUALITY GATE", REPORT_RULE]
    before = run.metrics.get("before")
    after = run.metrics.get("after")
    if before is not None and after is not None:
        out.append(f"  {'metric':<22} {'before':>10} {'after':>10}")
        out.append(f"  {'rows failing':<22} {len(before.failed_rows):>10} "
                   f"{len(after.failed_rows):>10}")
        out.append(f"  {'total failures':<22} {len(before.failures):>10} "
                   f"{len(after.failures):>10}")
        out.append(f"  {'pass rate':<22} {100 * before.pass_rate:>9.1f}% "
                   f"{100 * after.pass_rate:>9.1f}%")
        rate = run.metrics.get("failure_rate", 1.0)
        verdict = "PASS" if run.metrics.get("gate_passed") else "FAIL"
        out.append(f"\n  gate: {100 * rate:.1f}% failing against a "
                   f"{100 * MAX_ACCEPTABLE_FAILURE_RATE:.0f}% "
                   f"threshold -> {verdict}")
    else:
        out.append("  validation did not complete; no gate evaluated.")

    # ---- 4. outputs ----
    out += ["", REPORT_RULE, "4. OUTPUTS", REPORT_RULE]
    for stage in run.stages:
        for path in stage.outputs:
            size = path.stat().st_size if path.exists() else 0
            state = "ok" if path.exists() else "MISSING"
            out.append(f"  {state:<8} {size:>9,}b  "
                       f"{path.relative_to(PROJECT_ROOT)}")

    # ---- 5. deliverables ----
    out += ["", REPORT_RULE, "5. DELIVERABLES", REPORT_RULE]
    for path in DELIVERABLES:
        if path == PIPELINE_REPORT_PATH:
            # Being written by this very call, so disk state is last run's.
            out.append(f"  [x] {path.relative_to(PROJECT_ROOT)}   "
                       f"(this file)")
            continue
        exists = path.exists() and path.stat().st_size > 0
        mark = "[x]" if exists else "[ ]"
        note = "" if exists else "   <-- not produced"
        out.append(f"  {mark} {path.relative_to(PROJECT_ROOT)}{note}")

    # ---- 6. problems ----
    out += ["", REPORT_RULE, "6. PROBLEMS", REPORT_RULE]
    if run.failed:
        for stage in run.failed:
            out.append(f"  {stage.name}: {stage.error}")
    else:
        out.append("  none")
    out.append(f"\n  Full log: {LOG_PATH.relative_to(PROJECT_ROOT)}")

    out += ["", "END OF REPORT", REPORT_BORDER]
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(input_path: Path = RAW_PATH, quiet: bool = False) -> int:
    ensure_directories()
    logger = setup_logging(quiet)
    run_state = Run()

    logger.info("=" * 60)
    logger.info("pipeline start: input=%s", input_path)

    succeeded = True
    try:
        if not input_path.exists():
            raise FileNotFoundError(f"input not found: {input_path}")
        execute(run_state, logger, input_path)
        succeeded = not run_state.failed and bool(
            run_state.metrics.get("gate_passed"))
    except Exception as exc:
        succeeded = False
        logger.error("pipeline aborted: %s: %s", type(exc).__name__, exc)

    try:
        PIPELINE_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        PIPELINE_REPORT_PATH.write_text(
            "\n".join(format_report(run_state, input_path, succeeded)),
            encoding="utf-8")
        logger.info("execution report -> %s", PIPELINE_REPORT_PATH)
    except Exception as exc:
        logger.error("could not write execution report: %s", exc)

    logger.info("pipeline %s", "SUCCESS" if succeeded else "FAILED")
    return 0 if succeeded else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PII detection and data quality pipeline")
    parser.add_argument("--input", type=Path, default=RAW_PATH,
                        help="source CSV (default: data/raw/customers_raw.csv)")
    parser.add_argument("--quiet", action="store_true",
                        help="log to file only")
    args = parser.parse_args(argv)
    return run(args.input, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
