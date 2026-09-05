"""
cleaner.py
==========
Part 4 — Data cleaning.

Reads data/raw/customers_raw.csv and writes data/cleaned/customers_cleaned.csv
plus reports/cleaning_log.txt, then re-runs the Part 3 validator to confirm
the fixes.

Normalization (per the brief):
    phone   -> XXX-XXX-XXXX
    dates   -> YYYY-MM-DD
    names   -> Title Case

Cleaning principle: repair FORMAT, and remove only values config declares
meaningless (null sentinels, placeholder phones, sentinel dates) or that
cannot be parsed into the column's type at all. Semantic range violations
(negative income, a 2030 birth date) are left in place so they stay visible
as validation failures for the data owner. Nothing is imputed.

Usage:
    python -m src.cleaner
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from .config import (
    ACCOUNTING_NEGATIVE_RE,
    CLEANED_PATH,
    CLEANING_LOG_PATH,
    COMPACT_DATE_RE,
    CURRENCY_STRIP_RE,
    CURRENCY_SUFFIX_RE,
    DATE_INPUT_FORMATS,
    DATE_TIME_SPLIT_RE,
    EMAIL_RE,
    EPOCH_SECONDS_RE,
    EUROPEAN_NUMBER_RE,
    EXCEL_EPOCH,
    EXCEL_SERIAL_RE,
    INCOME_DECIMAL_PLACES,
    INCOME_SENTINEL_VALUES,
    MAGNITUDE_RE,
    MAGNITUDE_SUFFIXES,
    MISSING_PLACEHOLDER,
    MISSING_VALUE_STRATEGY,
    NAME_RE,
    NUMERIC_RANGE_RE,
    OUTPUT_ENCODING,
    PHONE_EXTENSION_RE,
    PHONE_PLACEHOLDERS,
    PHONE_VANITY_RE,
    QUARANTINE_PATH,
    QUARANTINE_THRESHOLD,
    RAW_PATH,
    REPORT_BORDER,
    REPORT_RULE,
    SCHEMA,
    SENTINEL_DATES,
    STATUS_CANONICAL_MAP,
    STATUS_PLACEHOLDER,
    UNIX_EPOCH,
    clean_ws,
    is_present,
)
from .profiler import load_raw
from .validator import REDACT_COLUMNS, validate_frame

# Action vocabulary used throughout the log.
OK = "ok"                       # already valid, untouched
NORMALIZED = "normalized"       # format repaired
NULLED = "nulled"               # meaningless value removed
UNFIXABLE = "unfixable"         # left as-is, still invalid
MISSING = "missing"             # absent on arrival

MAX_LOG_EXAMPLES = 4


class Change(NamedTuple):
    row: int
    column: str
    before: str
    after: str
    action: str
    reason: str


@dataclass
class CleaningReport:
    rows_in: int = 0
    rows_out: int = 0
    dropped: list[int] = field(default_factory=list)
    quarantined: list[int] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    placeholders: Counter = field(default_factory=Counter)

    def actions(self) -> Counter:
        return Counter((c.column, c.action) for c in self.changes)

    def reasons(self, column: str, action: str) -> Counter:
        return Counter(c.reason for c in self.changes
                       if c.column == column and c.action == action)


# --------------------------------------------------------------------------- #
# Column cleaners — each returns (value, action, reason)
# --------------------------------------------------------------------------- #

Result = tuple[str, str, str]


def clean_customer_id(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    v = clean_ws(raw)
    if re.fullmatch(r"-?\d+", v):
        out = str(int(v))
        return (out, OK, "") if out == v else (out, NORMALIZED, "leading zeros")
    try:
        number = float(v)
    except ValueError:
        return v, UNFIXABLE, "not numeric"
    if number.is_integer():
        return str(int(number)), NORMALIZED, "float round-trip"
    return v, UNFIXABLE, "scientific notation, value not recoverable"


# Nobility and patronymic particles stay lowercase inside a surname. Blind
# .title() rewrites "van der Berg" to "Van Der Berg", corrupting a correct
# name -- the same over-eager-rule defect NAME_RE is written to avoid.
NAME_PARTICLES = {
    "van", "von", "der", "den", "ter", "ten", "de", "del", "della", "di",
    "da", "dos", "du", "la", "le", "el", "bin", "binte", "ibn", "al",
}


def _title_name(value: str) -> str:
    """Title Case, preserving particles the source already had lowercase."""
    words = value.split(" ")
    return " ".join(
        word if word.islower() and word in NAME_PARTICLES else word.title()
        for word in words
    )


def clean_name(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    v = clean_ws(raw)
    if not NAME_RE.fullmatch(v):
        return v, UNFIXABLE, "not alphabetic"
    out = _title_name(v)
    if out == raw:
        return out, OK, ""
    reason = "title case" if out != v else "whitespace trimmed"
    return out, NORMALIZED, reason


def clean_email(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    original = clean_ws(raw)
    v = original.lower()
    if v.startswith("mailto:"):
        v = v[len("mailto:"):]
    v = v.rstrip(".")
    if ";" in v or "," in v:
        # Two addresses in one cell; picking one would discard a real value.
        return original, UNFIXABLE, "multiple addresses"
    if not EMAIL_RE.fullmatch(v):
        return original, UNFIXABLE, "malformed"
    return (v, OK, "") if v == raw else (v, NORMALIZED, "lowercase/trim")


def clean_phone(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    v = clean_ws(raw)
    core = PHONE_EXTENSION_RE.sub("", v)
    had_extension = core != v
    if PHONE_VANITY_RE.search(core):
        return v, UNFIXABLE, "letters in number"
    digits = re.sub(r"\D", "", core)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if digits in PHONE_PLACEHOLDERS:
        return "", NULLED, "placeholder number"
    if len(digits) != 10:
        reason = ("international" if v.startswith("+")
                  else f"{len(digits)} digits, expected 10")
        return v, UNFIXABLE, reason
    out = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if out == raw:
        return out, OK, ""
    return out, NORMALIZED, "extension dropped" if had_extension else "reformatted"


def _from_formats(text: str) -> date | None:
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def clean_date(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    v = clean_ws(raw)
    if v in SENTINEL_DATES:
        return "", NULLED, "sentinel date"

    head = DATE_TIME_SPLIT_RE.split(v)[0]
    parsed = _from_formats(head)
    if parsed is None and EXCEL_SERIAL_RE.fullmatch(v):
        parsed = EXCEL_EPOCH + timedelta(days=int(v))
    if parsed is None and EPOCH_SECONDS_RE.fullmatch(v):
        parsed = UNIX_EPOCH + timedelta(seconds=int(v))
    if parsed is None and COMPACT_DATE_RE.fullmatch(v):
        parsed = _from_formats(f"{v[:4]}-{v[4:6]}-{v[6:]}")
    if parsed is None:
        return "", NULLED, "unparseable as a date"

    out = parsed.isoformat()
    if out == raw:
        return out, OK, ""
    reason = "time component dropped" if head != v else "format converted"
    return out, NORMALIZED, reason


def clean_address(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    v = re.sub(r"\s+", " ", clean_ws(raw))
    return (v, OK, "") if v == raw else (v, NORMALIZED, "whitespace collapsed")


def clean_income(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    original = clean_ws(raw)
    v = original
    negative = False

    accounting = ACCOUNTING_NEGATIVE_RE.match(v)
    if accounting:
        v, negative = accounting.group(1), True
    if NUMERIC_RANGE_RE.fullmatch(v):
        return "", NULLED, "range, no single value"

    v = CURRENCY_SUFFIX_RE.sub("", v)
    if EUROPEAN_NUMBER_RE.fullmatch(v):
        v = v.replace(".", "").replace(",", ".")

    magnitude = MAGNITUDE_RE.fullmatch(v)
    if magnitude:
        base = magnitude.group(1).replace(",", "")
        try:
            amount = float(base) * MAGNITUDE_SUFFIXES[magnitude.group(2).lower()]
        except ValueError:
            return "", NULLED, "unparseable as a number"
    else:
        try:
            amount = float(CURRENCY_STRIP_RE.sub("", v))
        except ValueError:
            return "", NULLED, "unparseable as a number"

    if negative:
        amount = -amount
    if amount in INCOME_SENTINEL_VALUES:
        return "", NULLED, "sentinel amount"

    out = f"{amount:.{INCOME_DECIMAL_PLACES}f}"
    return (out, OK, "") if out == raw else (out, NORMALIZED, "numeric format")


def clean_status(raw: str) -> Result:
    if not is_present(raw):
        return "", MISSING, "absent"
    v = clean_ws(raw)
    canonical = STATUS_CANONICAL_MAP.get(v.lower())
    if canonical is None:
        return v, UNFIXABLE, "no canonical mapping"
    return (canonical, OK, "") if canonical == raw else (canonical, NORMALIZED,
                                                         "canonicalised")


CLEANERS = {
    "customer_id": clean_customer_id,
    "first_name": clean_name,
    "last_name": clean_name,
    "email": clean_email,
    "phone": clean_phone,
    "date_of_birth": clean_date,
    "address": clean_address,
    "income": clean_income,
    "account_status": clean_status,
    "created_date": clean_date,
}


# --------------------------------------------------------------------------- #
# Frame-level cleaning
# --------------------------------------------------------------------------- #

def clean_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame,
                                           CleaningReport]:
    report = CleaningReport(rows_in=len(df))
    rows: list[dict[str, str]] = []

    for i, source in enumerate(df.to_dict(orient="records")):
        row: dict[str, str] = {}
        for col in SCHEMA:
            raw = str(source.get(col, ""))
            value, action, reason = CLEANERS[col](raw)
            if action != OK:
                report.changes.append(
                    Change(i, col, raw, value, action, reason))
            row[col] = value
        rows.append(row)

    cleaned = pd.DataFrame(rows, columns=SCHEMA)

    # ---- missing-value strategy ----
    drop_mask = pd.Series(False, index=cleaned.index)
    for col in SCHEMA:
        gaps = ~cleaned[col].map(is_present)
        if not gaps.any():
            continue
        strategy = MISSING_VALUE_STRATEGY[col]
        if strategy == "drop_row":
            drop_mask |= gaps
        elif strategy == "placeholder":
            filler = (STATUS_PLACEHOLDER if col == "account_status"
                      else MISSING_PLACEHOLDER)
            cleaned.loc[gaps, col] = filler
            report.placeholders[col] += int(gaps.sum())

    report.dropped = cleaned.index[drop_mask].tolist()
    kept = cleaned.loc[~drop_mask].copy()

    # ---- quarantine rows that are still badly broken ----
    result = validate_frame(kept)
    per_row = Counter(f.row for f in result.failures)
    positions = [p for p, count in per_row.items()
                 if count >= QUARANTINE_THRESHOLD]
    quarantine_index = kept.index[positions] if positions else kept.index[:0]

    report.quarantined = quarantine_index.tolist()
    quarantined = kept.loc[quarantine_index].copy()
    final = kept.drop(index=quarantine_index)
    report.rows_out = len(final)

    return final.reset_index(drop=True), quarantined.reset_index(drop=True), report


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #

def _shape(value: str) -> str:
    """Digits/letters as placeholders, so a format change is visible but the
    value is not: '(555) 123-4567' renders as '(###) ###-####'."""
    return "".join(
        "#" if ch.isdigit()
        else ("A" if ch.isupper() else "a") if ch.isalpha()
        else ch
        for ch in value
    )


def _sample(column: str, value: str) -> str:
    if not value:
        return "<removed>"
    if column in REDACT_COLUMNS:
        return repr(_shape(value))
    return repr(value)


def format_log(report: CleaningReport, before, after) -> list[str]:
    out = [
        REPORT_BORDER,
        "CLEANING LOG - customers_raw.csv -> customers_cleaned.csv",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        REPORT_BORDER,
    ]

    # ---- 1. row accounting ----
    out += ["", REPORT_RULE, "1. ROW ACCOUNTING", REPORT_RULE]
    out.append(f"  rows read        : {report.rows_in}")
    out.append(f"  rows written     : {report.rows_out}")
    out.append(f"  dropped (no key) : {len(report.dropped)}")
    out.append(f"  quarantined      : {len(report.quarantined)} "
               f"(>= {QUARANTINE_THRESHOLD} failures)")
    out.append(f"  cells changed    : {len(report.changes)}")
    out.append("  Dropped and quarantined rows are written to "
               f"{QUARANTINE_PATH.name},")
    out.append("  never discarded.")

    # ---- 2. actions by column ----
    out += ["", REPORT_RULE, "2. ACTIONS BY COLUMN", REPORT_RULE]
    actions = report.actions()
    out.append(f"  {'column':<16} {'normalized':>11} {'nulled':>8} "
               f"{'unfixable':>10} {'missing':>8}")
    for col in SCHEMA:
        out.append(f"  {col:<16} {actions[(col, NORMALIZED)]:>11} "
                   f"{actions[(col, NULLED)]:>8} "
                   f"{actions[(col, UNFIXABLE)]:>10} "
                   f"{actions[(col, MISSING)]:>8}")

    # ---- 3. normalization detail ----
    out += ["", REPORT_RULE, "3. NORMALIZATION", REPORT_RULE]
    for col in SCHEMA:
        reasons = report.reasons(col, NORMALIZED)
        if not reasons:
            continue
        out.append(f"  {col}")
        for reason, count in reasons.most_common():
            out.append(f"    {reason:<34} {count:>6}")

    # ---- 4. removals ----
    out += ["", REPORT_RULE, "4. VALUES REMOVED", REPORT_RULE]
    out.append("  Removed because the value carried no information, not")
    out.append("  because it was inconvenient. Each becomes a visible gap.")
    any_nulled = False
    for col in SCHEMA:
        reasons = report.reasons(col, NULLED)
        if not reasons:
            continue
        any_nulled = True
        out.append(f"  {col}")
        for reason, count in reasons.most_common():
            out.append(f"    {reason:<34} {count:>6}")
    if not any_nulled:
        out.append("  none")

    # ---- 5. unfixable ----
    out += ["", REPORT_RULE, "5. LEFT FOR THE DATA OWNER", REPORT_RULE]
    out.append("  Kept as-is and still failing validation; repairing these")
    out.append("  would mean inventing data.")
    for col in SCHEMA:
        reasons = report.reasons(col, UNFIXABLE)
        if not reasons:
            continue
        out.append(f"  {col}")
        for reason, count in reasons.most_common():
            out.append(f"    {reason:<34} {count:>6}")

    # ---- 6. missing-value strategy ----
    out += ["", REPORT_RULE, "6. MISSING-VALUE STRATEGY", REPORT_RULE]
    out.append(f"  {'column':<16} {'strategy':<14} {'gaps filled':>12}")
    for col in SCHEMA:
        out.append(f"  {col:<16} {MISSING_VALUE_STRATEGY[col]:<14} "
                   f"{report.placeholders.get(col, 0):>12}")
    out.append(f"\n  Placeholder is {MISSING_PLACEHOLDER!r} "
               f"({STATUS_PLACEHOLDER!r} for account_status). Both are null")
    out.append("  sentinels, so a filled gap stays visible as a gap rather")
    out.append("  than passing validation as real data. Nothing is imputed.")

    # ---- 7. before / after ----
    out += ["", REPORT_RULE, "7. VALIDATION: BEFORE vs AFTER", REPORT_RULE]
    out.append(f"  {'metric':<26} {'before':>10} {'after':>10}")
    out.append(f"  {'rows':<26} {before.total_rows:>10} "
               f"{after.total_rows:>10}")
    out.append(f"  {'rows failing':<26} {len(before.failed_rows):>10} "
               f"{len(after.failed_rows):>10}")
    out.append(f"  {'total failures':<26} {len(before.failures):>10} "
               f"{len(after.failures):>10}")
    out.append(f"  {'pass rate':<26} {100 * before.pass_rate:>9.1f}% "
               f"{100 * after.pass_rate:>9.1f}%")

    out.append("\n  by column:")
    before_cols, after_cols = before.by_column(), after.by_column()
    out.append(f"  {'column':<16} {'before':>8} {'after':>8} {'fixed':>8}")
    for col in SCHEMA:
        b, a = before_cols.get(col, 0), after_cols.get(col, 0)
        out.append(f"  {col:<16} {b:>8} {a:>8} {b - a:>8}")

    # ---- 8. examples ----
    out += ["", REPORT_RULE, "8. SAMPLE CHANGES", REPORT_RULE]
    shown: set[tuple[str, str]] = set()
    for change in report.changes:
        if change.action not in (NORMALIZED, NULLED):
            continue
        key = (change.column, change.reason)
        if key in shown:
            continue
        shown.add(key)
        out.append(f"  {change.column:<15} "
                   f"{_sample(change.column, change.before)} -> "
                   f"{_sample(change.column, change.after)}")
        out.append(f"    {change.reason}")
        if len(shown) >= MAX_LOG_EXAMPLES * 4:
            break

    out += ["", "END OF LOG", REPORT_BORDER]
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(input_path: Path = RAW_PATH,
        output_path: Path = CLEANED_PATH,
        log_path: Path = CLEANING_LOG_PATH,
        quarantine_path: Path = QUARANTINE_PATH) -> Path:
    for path in (output_path, log_path, quarantine_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    raw, _ = load_raw(input_path)
    before = validate_frame(raw)

    cleaned, quarantined, report = clean_frame(raw)
    after = validate_frame(cleaned)

    cleaned.to_csv(output_path, index=False, encoding=OUTPUT_ENCODING,
                   lineterminator="\n")

    dropped = raw.loc[[i for i in report.dropped if i in raw.index]]
    rejects = pd.concat([dropped, quarantined], ignore_index=True)
    rejects.to_csv(quarantine_path, index=False, encoding=OUTPUT_ENCODING,
                   lineterminator="\n")

    log_path.write_text("\n".join(format_log(report, before, after)),
                        encoding="utf-8")

    print(f"Cleaned CSV   -> {output_path}  ({len(cleaned)} rows)")
    print(f"Quarantine    -> {quarantine_path}  ({len(rejects)} rows)")
    print(f"Cleaning log  -> {log_path}")
    print(f"  failures {len(before.failures)} -> {len(after.failures)}, "
          f"pass rate {100 * before.pass_rate:.1f}% -> "
          f"{100 * after.pass_rate:.1f}%")
    return output_path


if __name__ == "__main__":
    run()
