"""
validator.py
============
Part 3 — Data validation.

Applies the writeup's rule table with Pydantic and produces
reports/validation_results.txt.

Rules enforced (Section 2 of the brief):
    customer_id     integer, unique, positive
    first_name      non-empty, 2-50 chars, alphabetic
    last_name       non-empty, 2-50 chars, alphabetic
    email           valid email format
    phone           valid phone, XXX-XXX-XXXX
    date_of_birth   valid date, YYYY-MM-DD
    address         non-empty, 10-200 chars
    income          non-negative, <= $10M
    account_status  active | inactive | suspended
    created_date    valid date, YYYY-MM-DD

Row-level rules run through a Pydantic model, so each failure carries its
own column, rule id and message. Uniqueness is a dataset-level rule and is
checked separately across the frame.

validate_frame() is reused by the cleaner to re-validate after remediation.

Usage:
    python -m src.validator
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from .config import (
    ACCOUNTING_NEGATIVE_RE,
    ADDRESS_MAX_LEN,
    ADDRESS_MIN_LEN,
    COMPANY_FOUNDED,
    CURRENCY_STRIP_RE,
    CURRENCY_SUFFIX_RE,
    CUSTOMER_ID_MIN,
    DATE_INPUT_FORMATS,
    DATE_OUTPUT_FORMAT,
    EMAIL_RE,
    EUROPEAN_NUMBER_RE,
    INCOME_MAX,
    INCOME_MIN,
    MAGNITUDE_RE,
    MAX_ACCEPTABLE_FAILURE_RATE,
    NAME_MAX_LEN,
    NAME_MIN_LEN,
    NAME_RE,
    PHONE_CLEAN_RE,
    PHONE_EXTENSION_RE,
    PHONE_STRIP_RE,
    PRIMARY_KEY,
    RAW_PATH,
    REPORT_BORDER,
    REPORT_RULE,
    SCHEMA,
    SENTINEL_DATES,
    STATUS_CANONICAL_MAP,
    VALIDATION_REPORT_PATH,
    clean_ws,
    dob_bounds,
    is_present,
    rel,
)
from .pii_detector import redact
from .profiler import load_raw

DOB_MIN, TODAY = dob_bounds()

# The brief marks only these "Non-empty"; a missing value elsewhere is
# reported as a gap, not as a rule violation.
REQUIRED_COLUMNS = ["customer_id", "first_name", "last_name", "address"]

# Offending values are echoed so failures are actionable, except for direct
# identifiers and address, which are redacted because reports/ is committed.
REDACT_COLUMNS = ["first_name", "last_name", "email", "phone", "address"]

MAX_LISTED_FAILURES = 40


# --------------------------------------------------------------------------- #
# Failure records
# --------------------------------------------------------------------------- #

class Failure(NamedTuple):
    row: int
    customer_id: str
    column: str
    rule: str
    message: str
    value: str
    recoverable: bool


@dataclass
class ValidationResult:
    total_rows: int
    failures: list[Failure] = field(default_factory=list)
    missing: dict[str, int] = field(default_factory=dict)

    @property
    def failed_rows(self) -> set[int]:
        return {f.row for f in self.failures}

    @property
    def pass_rate(self) -> float:
        if not self.total_rows:
            return 0.0
        return 1.0 - len(self.failed_rows) / self.total_rows

    def by_column(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.failures:
            counts[f.column] = counts.get(f.column, 0) + 1
        return counts

    def by_rule(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for f in self.failures:
            key = (f.column, f.rule)
            counts[key] = counts.get(key, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# Parsing helpers shared by the validators
# --------------------------------------------------------------------------- #

def _as_date(value: str) -> date | None:
    """Strict ISO parse; None if the value is not YYYY-MM-DD."""
    try:
        return datetime.strptime(value, DATE_OUTPUT_FORMAT).date()
    except ValueError:
        return None


def _parses_any_format(value: str) -> bool:
    head = re.split(r"[ T]", value)[0]
    for fmt in DATE_INPUT_FORMATS:
        try:
            datetime.strptime(head, fmt)
            return True
        except ValueError:
            continue
    return False


def _as_number(value: str) -> float | None:
    text = CURRENCY_SUFFIX_RE.sub("", value)
    try:
        return float(CURRENCY_STRIP_RE.sub("", text))
    except ValueError:
        return None


def _cleanable_number(value: str) -> bool:
    if _as_number(value) is not None:
        return True
    return bool(ACCOUNTING_NEGATIVE_RE.match(value)
                or MAGNITUDE_RE.match(value)
                or EUROPEAN_NUMBER_RE.match(value))


def _phone_digits(value: str) -> str:
    return PHONE_STRIP_RE.sub("", PHONE_EXTENSION_RE.sub("", value))


# --------------------------------------------------------------------------- #
# Row model — one Pydantic validator per rule in the brief
# --------------------------------------------------------------------------- #

def _fail(rule: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(rule, message)


def _require(value: str, column: str) -> str:
    if not is_present(value):
        raise _fail("missing_required", f"{column} is empty or a null sentinel")
    return clean_ws(value)


class CustomerRecord(BaseModel):
    """The brief's rule table, expressed as a validated record."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    date_of_birth: str
    address: str
    income: str
    account_status: str
    created_date: str

    @field_validator("customer_id")
    @classmethod
    def check_customer_id(cls, v: str) -> str:
        v = _require(v, "customer_id")
        if not re.fullmatch(r"-?\d+", v):
            raise _fail("not_integer", f"{v!r} is not an integer")
        if int(v) < CUSTOMER_ID_MIN:
            raise _fail("not_positive", f"{v} is below {CUSTOMER_ID_MIN}")
        return v

    @field_validator("first_name", "last_name")
    @classmethod
    def check_name(cls, v: str, info) -> str:
        v = _require(v, info.field_name)
        if not NAME_MIN_LEN <= len(v) <= NAME_MAX_LEN:
            raise _fail("length_out_of_range",
                        f"length {len(v)} outside "
                        f"{NAME_MIN_LEN}-{NAME_MAX_LEN}")
        if not NAME_RE.fullmatch(v):
            raise _fail("not_alphabetic", f"{v!r} is not alphabetic")
        return v

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        if not is_present(v):
            return ""
        v = clean_ws(v)
        if not EMAIL_RE.fullmatch(v):
            raise _fail("invalid_email_format", f"{v!r} is not a valid address")
        return v

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v: str) -> str:
        if not is_present(v):
            return ""
        v = clean_ws(v)
        if not PHONE_CLEAN_RE.fullmatch(v):
            raise _fail("invalid_phone_format",
                        f"{v!r} is not XXX-XXX-XXXX")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def check_dob(cls, v: str) -> str:
        if not is_present(v):
            return ""
        v = clean_ws(v)
        if v in SENTINEL_DATES:
            raise _fail("sentinel_date", f"{v!r} is a placeholder, not a date")
        parsed = _as_date(v)
        if parsed is None:
            raise _fail("not_iso_date", f"{v!r} is not YYYY-MM-DD")
        if parsed < DOB_MIN:
            raise _fail("out_of_range", f"{v} precedes {DOB_MIN}")
        if parsed > TODAY:
            raise _fail("out_of_range", f"{v} is in the future")
        return v

    @field_validator("created_date")
    @classmethod
    def check_created(cls, v: str) -> str:
        if not is_present(v):
            return ""
        v = clean_ws(v)
        if v in SENTINEL_DATES:
            raise _fail("sentinel_date", f"{v!r} is a placeholder, not a date")
        parsed = _as_date(v)
        if parsed is None:
            raise _fail("not_iso_date", f"{v!r} is not YYYY-MM-DD")
        if parsed < COMPANY_FOUNDED:
            raise _fail("out_of_range", f"{v} precedes {COMPANY_FOUNDED}")
        if parsed > TODAY:
            raise _fail("out_of_range", f"{v} is in the future")
        return v

    @field_validator("address")
    @classmethod
    def check_address(cls, v: str) -> str:
        v = _require(v, "address")
        if not ADDRESS_MIN_LEN <= len(v) <= ADDRESS_MAX_LEN:
            raise _fail("length_out_of_range",
                        f"length {len(v)} outside "
                        f"{ADDRESS_MIN_LEN}-{ADDRESS_MAX_LEN}")
        return v

    @field_validator("income")
    @classmethod
    def check_income(cls, v: str) -> str:
        if not is_present(v):
            return ""
        v = clean_ws(v)
        amount = _as_number(v)
        if amount is None:
            raise _fail("not_numeric", f"{v!r} is not a number")
        if amount < INCOME_MIN:
            raise _fail("negative", f"{amount:,.2f} is negative")
        if amount > INCOME_MAX:
            raise _fail("exceeds_max",
                        f"{amount:,.2f} exceeds {INCOME_MAX:,.0f}")
        return v

    @field_validator("account_status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if not is_present(v):
            return ""
        v = clean_ws(v)
        if v.lower() not in ("active", "inactive", "suspended"):
            raise _fail("invalid_category",
                        f"{v!r} is not active/inactive/suspended")
        return v


# --------------------------------------------------------------------------- #
# Recoverability — does a cleaner have enough to fix this?
# --------------------------------------------------------------------------- #

def recoverable(column: str, rule: str, raw: str) -> bool:
    """True when Part 4 can repair the value without inventing data."""
    v = clean_ws(raw)
    # Range and emptiness failures need the real value from the source system;
    # no transformation of what is on disk can recover them.
    if rule in ("missing_required", "sentinel_date", "out_of_range",
                "length_out_of_range", "negative", "exceeds_max",
                "not_positive", "not_unique"):
        return False
    if column == "customer_id":
        # Recoverable only if it really is a whole number in disguise:
        # "1042.0" and "0001042" are, "1.042E+00" is not.
        try:
            return float(v).is_integer()
        except ValueError:
            return False
    if column in ("first_name", "last_name"):
        return NAME_RE.fullmatch(v) is not None
    if column == "email":
        return EMAIL_RE.fullmatch(v.strip().lower().rstrip(".")) is not None
    if column == "phone":
        return len(_phone_digits(v)) == 10
    if column in ("date_of_birth", "created_date"):
        return _parses_any_format(v)
    if column == "income":
        return _cleanable_number(v)
    if column == "account_status":
        return v.lower() in STATUS_CANONICAL_MAP
    return False


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_frame(df: pd.DataFrame) -> ValidationResult:
    """Row rules via Pydantic, plus the dataset-level uniqueness rule."""
    result = ValidationResult(total_rows=len(df))

    for col in SCHEMA:
        if col in df.columns:
            result.missing[col] = int((~df[col].map(is_present)).sum())

    records = df.to_dict(orient="records")
    for i, row in enumerate(records):
        payload = {col: str(row.get(col, "")) for col in SCHEMA}
        cid = clean_ws(payload["customer_id"]) or "?"
        try:
            CustomerRecord(**payload)
        except ValidationError as exc:
            for err in exc.errors():
                column = str(err["loc"][0])
                rule = err["type"]
                raw = payload.get(column, "")
                result.failures.append(Failure(
                    row=i,
                    customer_id=cid,
                    column=column,
                    rule=rule,
                    message=err["msg"],
                    value=raw,
                    recoverable=recoverable(column, rule, raw),
                ))

    # ---- dataset-level: customer_id uniqueness ----
    keys = df[PRIMARY_KEY].map(clean_ws)
    present = keys[keys.map(is_present)]
    dupes = present.value_counts()
    repeated = set(dupes[dupes > 1].index)
    for i, key in enumerate(keys):
        if key in repeated:
            result.failures.append(Failure(
                row=i,
                customer_id=key,
                column=PRIMARY_KEY,
                rule="not_unique",
                message=f"{key!r} appears {int(dupes[key])} times",
                value=key,
                recoverable=False,
            ))

    return result


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _show(column: str, value: str) -> str:
    # A null sentinel carries no PII, and which spelling it used is the
    # diagnostic, so it is shown verbatim even for redacted columns.
    if column in REDACT_COLUMNS and is_present(value):
        return redact(value)
    return repr(clean_ws(value))


def format_report(result: ValidationResult, source: Path, label: str,
                  enforce_gate: bool = False) -> list[str]:
    n = result.total_rows
    failed = len(result.failed_rows)

    out = [
        REPORT_BORDER,
        f"VALIDATION RESULTS - {label}",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Source: {source.name}   Rows validated: {n}",
        "Engine: Pydantic v2 (row rules) + pandas (uniqueness)",
        REPORT_BORDER,
    ]

    # ---- 1. rules ----
    out += ["", REPORT_RULE, "1. RULES ENFORCED", REPORT_RULE]
    rules = [
        ("customer_id", "integer, unique, positive"),
        ("first_name", f"non-empty, {NAME_MIN_LEN}-{NAME_MAX_LEN} chars, alphabetic"),
        ("last_name", f"non-empty, {NAME_MIN_LEN}-{NAME_MAX_LEN} chars, alphabetic"),
        ("email", "valid email format"),
        ("phone", "XXX-XXX-XXXX"),
        ("date_of_birth", f"YYYY-MM-DD, {DOB_MIN} to today"),
        ("address", f"non-empty, {ADDRESS_MIN_LEN}-{ADDRESS_MAX_LEN} chars"),
        ("income", f"numeric, {INCOME_MIN:,.0f} to {INCOME_MAX:,.0f}"),
        ("account_status", "active | inactive | suspended"),
        ("created_date", f"YYYY-MM-DD, {COMPANY_FOUNDED} to today"),
    ]
    for col, rule in rules:
        out.append(f"  {col:<16} {rule}")
    out.append(f"\n  Required (non-empty): {', '.join(REQUIRED_COLUMNS)}")
    out.append("  Elsewhere a missing value is reported as a gap, not a")
    out.append("  rule violation, matching the brief's wording.")
    out.append("  The brief sets no format rule for income, so currency")
    out.append("  symbols and thousands separators parse rather than fail;")
    out.append("  normalizing them is the cleaner's job.")

    # ---- 2. summary ----
    out += ["", REPORT_RULE, "2. SUMMARY", REPORT_RULE]
    out.append(f"  rows validated      : {n}")
    out.append(f"  rows passing all    : {n - failed} "
               f"({100.0 * result.pass_rate:.1f}%)")
    out.append(f"  rows with >=1 fail  : {failed} "
               f"({100.0 * failed / n if n else 0.0:.1f}%)")
    out.append(f"  total failures      : {len(result.failures)}")
    rec = sum(1 for f in result.failures if f.recoverable)
    out.append(f"  auto-fixable        : {rec}")
    out.append(f"  need source data    : {len(result.failures) - rec}")

    # ---- 3. per column ----
    out += ["", REPORT_RULE, "3. FAILURES BY COLUMN", REPORT_RULE]
    by_col = result.by_column()
    out.append(f"  {'column':<16} {'failures':>9} {'missing':>9}")
    for col in SCHEMA:
        out.append(f"  {col:<16} {by_col.get(col, 0):>9} "
                   f"{result.missing.get(col, 0):>9}")

    # ---- 4. per rule ----
    out += ["", REPORT_RULE, "4. FAILURES BY RULE", REPORT_RULE]
    out.append(f"  {'column':<16} {'rule':<22} {'count':>7} {'fixable':>8}")
    by_rule = result.by_rule()
    fixable_by_rule: dict[tuple[str, str], int] = {}
    for f in result.failures:
        if f.recoverable:
            key = (f.column, f.rule)
            fixable_by_rule[key] = fixable_by_rule.get(key, 0) + 1
    for (col, rule), count in sorted(by_rule.items(),
                                     key=lambda kv: -kv[1]):
        out.append(f"  {col:<16} {rule:<22} {count:>7} "
                   f"{fixable_by_rule.get((col, rule), 0):>8}")

    # ---- 5. row detail ----
    out += ["", REPORT_RULE, "5. ROW-LEVEL FAILURES", REPORT_RULE]
    out.append(f"  {'row':>6} {'customer_id':<14} {'column':<15} "
               f"{'rule':<22} value / reason")
    for f in result.failures[:MAX_LISTED_FAILURES]:
        flag = "" if f.recoverable else "  [needs source]"
        out.append(f"  {f.row:>6} {f.customer_id:<14} {f.column:<15} "
                   f"{f.rule:<22} {_show(f.column, f.value)}{flag}")
    if len(result.failures) > MAX_LISTED_FAILURES:
        out.append(f"  ... {len(result.failures) - MAX_LISTED_FAILURES} "
                   f"further failures not listed")
    out.append(f"\n  Values for {', '.join(REDACT_COLUMNS)} are redacted;")
    out.append("  locate the record by row number or customer_id.")

    # ---- 6. verdict ----
    out += ["", REPORT_RULE, "6. VERDICT", REPORT_RULE]
    fail_rate = failed / n if n else 1.0
    threshold = MAX_ACCEPTABLE_FAILURE_RATE
    out.append(f"  failure rate: {100.0 * fail_rate:.1f}%")
    if enforce_gate:
        verdict = "PASS" if fail_rate <= threshold else "FAIL"
        out.append(f"  gate: {100.0 * threshold:.0f}% -> {verdict}")
        if verdict == "FAIL":
            out.append("  Cleaned output is not fit for release.")
    else:
        # The gate measures remediation, so scoring raw input against it
        # would report a PASS for data that has not been cleaned yet.
        out.append(f"  The {100.0 * threshold:.0f}% gate applies to cleaned "
                   f"output and is not scored here.")
        out.append(f"  {rec} of {len(result.failures)} failures are "
                   f"auto-fixable; the rest need the source system.")
    out += ["", "END OF REPORT", REPORT_BORDER]
    return out


def run(input_path: Path = RAW_PATH,
        report_path: Path = VALIDATION_REPORT_PATH,
        label: str = "customers_raw.csv (pre-cleaning)",
        enforce_gate: bool = False) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)

    df, _ = load_raw(input_path)
    result = validate_frame(df)
    report = format_report(result, input_path, label, enforce_gate)
    report_path.write_text("\n".join(report), encoding="utf-8")
    print(f"Report written -> {rel(report_path)}")
    print(f"  {len(result.failed_rows)} of {result.total_rows} rows failed, "
          f"{len(result.failures)} total failures")
    return report_path


if __name__ == "__main__":
    run()
