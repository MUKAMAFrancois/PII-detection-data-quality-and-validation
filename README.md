# PII Detection & Data Quality Validation Pipeline

A Python pipeline that profiles a messy fintech customer extract, detects the
PII in it, validates it against a rule table, cleans it, masks it, and reports
on every step.

Input is `data/raw/customers_raw.csv` (3,003 rows, ~30% defective by
construction). One command runs everything:

```bash
python -m src.pipeline
```

**Results:** 2,998 rows read, 2,943 written (98.2% retention). Validation
failures 1,011 → 484; pass rate 71.8% → 85.8%. 17,330 PII cells masked with
zero leaks. Runs in about four seconds.

---

## Layout

| Module | Part | Produces |
| :--- | :--- | :--- |
| [`config.py`](src/config.py) | — | Single source of truth: paths, schema, rules, PII classes, masking rules |
| [`dataset_generator.py`](src/dataset_generator.py) | — | The synthetic messy source file |
| [`profiler.py`](src/profiler.py) | 1 | `data_quality_report.txt` |
| [`pii_detector.py`](src/pii_detector.py) | 2 | `pii_detection_report.txt` |
| [`validator.py`](src/validator.py) | 3 | `validation_results.txt` |
| [`cleaner.py`](src/cleaner.py) | 4 | `customers_cleaned.csv`, `cleaning_log.txt` |
| [`masker.py`](src/masker.py) | 5 | `customers_masked.csv`, `masked_sample.txt` |
| [`pipeline.py`](src/pipeline.py) | 6 | `pipeline_execution_report.txt`, `logs/pipeline.log` |
| [`reflection.md`](reflection.md) | 7 | Written analysis |

---

## Running it

Requires Python 3.11+. Install dependencies (`pandas`, `pydantic`) first:

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
```

The whole pipeline, writing every report and both CSVs:

```bash
python -m src.pipeline
```

```bash
python -m src.pipeline --input data/raw/other.csv --quiet
```

`--quiet` logs to `logs/pipeline.log` only. Exit code is 0 only when every
critical stage succeeded and the cleaned output cleared the quality gate, so
the run can be scheduled and alerted on directly.

Each stage also runs on its own, in this order:

```bash
python -m src.profiler        # Part 1  -> data_quality_report.txt
python -m src.pii_detector    # Part 2  -> pii_detection_report.txt
python -m src.validator       # Part 3  -> validation_results.txt
python -m src.cleaner         # Part 4  -> customers_cleaned.csv + cleaning_log.txt
python -m src.masker          # Part 5  -> customers_masked.csv + masked_sample.txt
```

`cleaner` reads the raw file; `masker` reads the cleaned one, so run them in
order after any change to the source.

Regenerate the synthetic source (3,000 rows, seed 42, ~30% defective):

```bash
python -m src.dataset_generator
python -m src.dataset_generator 5000 --seed 7 --manifest
```

Tests — 29 cases over the rule table, the three normalizations and the five
masking rules:

```bash
python -m pytest tests/ -q
python -m tests.test_validators     # same suite, no pytest needed
```