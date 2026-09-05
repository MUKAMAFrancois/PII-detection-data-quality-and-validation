"""
config.py
=========
Single source of truth for the pipeline: paths, schema, validation rules,
PII classification, masking rules and remediation strategy.

Declarative and side-effect free on import; only ensure_directories() writes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- #
# 1. Paths
# --------------------------------------------------------------------------- #

# Root-anchored, so entrypoints behave the same from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CLEANED_DIR = DATA_DIR / "cleaned"
MASKED_DIR = DATA_DIR / "masked"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"

RAW_PATH = RAW_DIR / "customers_raw.csv"
MANIFEST_PATH = RAW_DIR / "defect_manifest.csv"
CLEANED_PATH = CLEANED_DIR / "customers_cleaned.csv"
QUARANTINE_PATH = CLEANED_DIR / "customers_quarantined.csv"
MASKED_PATH = MASKED_DIR / "customers_masked.csv"

QUALITY_REPORT_PATH = REPORTS_DIR / "data_quality_report.txt"
PII_REPORT_PATH = REPORTS_DIR / "pii_detection_report.txt"
VALIDATION_REPORT_PATH = REPORTS_DIR / "validation_results.txt"
CLEANING_LOG_PATH = REPORTS_DIR / "cleaning_log.txt"
MASKED_SAMPLE_PATH = REPORTS_DIR / "masked_sample.txt"
PIPELINE_REPORT_PATH = REPORTS_DIR / "pipeline_execution_report.txt"

LOG_PATH = LOGS_DIR / "pipeline.log"

OUTPUT_DIRS = (CLEANED_DIR, MASKED_DIR, REPORTS_DIR, LOGS_DIR)


def ensure_directories() -> None:
    """Create every output directory."""
    for directory in OUTPUT_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def rel(path: Path | str) -> str:
    """Render a path relative to the project root, forward-slashed.

    Absolute paths in logs and reports leak the machine's directory layout
    and make output differ between checkouts. Falls back to the path as
    given when it sits outside the project (another drive, say).
    """
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return candidate.as_posix()


# --------------------------------------------------------------------------- #
# 2. Schema and column roles
# --------------------------------------------------------------------------- #

SCHEMA = [
    "customer_id", "first_name", "last_name", "email", "phone",
    "date_of_birth", "address", "income", "account_status", "created_date",
]

PRIMARY_KEY = "customer_id"

# integer | numeric | date | string | categorical
COLUMN_TYPES = {
    "customer_id": "integer",
    "first_name": "string",
    "last_name": "string",
    "email": "string",
    "phone": "string",
    "date_of_birth": "date",
    "address": "string",
    "income": "numeric",
    "account_status": "categorical",
    "created_date": "date",
}

DATE_COLUMNS = [c for c, t in COLUMN_TYPES.items() if t == "date"]
NUMERIC_COLUMNS = [c for c, t in COLUMN_TYPES.items()
                   if t in ("integer", "numeric")]

REQUIRED_COLUMNS = ["customer_id"]

# Hard constraint on the key; duplicate emails/phones are reported, not rejected.
UNIQUE_COLUMNS = ["customer_id"]
SOFT_UNIQUE_COLUMNS = ["email", "phone"]


# --------------------------------------------------------------------------- #
# 3. Null sentinels and invisible characters
# --------------------------------------------------------------------------- #

# All counted as missing; treating only "" as missing understates the gap.
# "[missing]" is our own placeholder (see section 12) and belongs here so a
# filled gap keeps reading as absent downstream.
NULL_SENTINELS = {
    "", "null", "none", "nan", "na", "n/a", "#n/a", "-",
    "--", "?", "tbd", "unknown", "not provided", "[missing]",
}

# Codepoints, not literals: an invisible character in source is unreviewable.
INVISIBLE_CHARS = {
    chr(0x00A0): " ",   # non-breaking space
    chr(0x200B): "",    # zero-width space
    chr(0xFEFF): "",    # BOM as a cell value
    chr(0x000D): "",    # carriage return
}

INPUT_ENCODING = "utf-8-sig"
OUTPUT_ENCODING = "utf-8"


# --------------------------------------------------------------------------- #
# 4. Normalization primitives
# --------------------------------------------------------------------------- #
# Kept beside NULL_SENTINELS: the set and the test that applies it are one
# decision, and splitting them across modules is how they drift apart.

def clean_ws(value: object) -> str:
    """Fold compatibility variants, drop invisible characters, strip edges."""
    text = unicodedata.normalize("NFKC", str(value))
    for char, replacement in INVISIBLE_CHARS.items():
        text = text.replace(char, replacement)
    return text.strip()


def is_null_sentinel(value: object) -> bool:
    """True if the cell means "no value", whatever spelling it used."""
    return clean_ws(value).lower() in NULL_SENTINELS


def is_present(value: object) -> bool:
    """True if the cell carries real content."""
    return not is_null_sentinel(value)


# --------------------------------------------------------------------------- #
# 5. Validation rules
# --------------------------------------------------------------------------- #
# Anchored: "is this whole value already correct?"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_CLEAN_RE = re.compile(r"^\d{3}-\d{3}-\d{4}$")
PHONE_DIGITS_RE = re.compile(r"^\d{10}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Unicode-aware: a plain [A-Za-z]+ rule rejects O'Brien and van der Berg.
NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '\-][^\W\d_]+)*$", re.UNICODE)

NAME_MIN_LEN, NAME_MAX_LEN = 2, 50
ADDRESS_MIN_LEN, ADDRESS_MAX_LEN = 10, 200

INCOME_MIN = 0.0
INCOME_MAX = 10_000_000.0

CUSTOMER_ID_MIN = 1

MAX_AGE_YEARS = 150
ADULT_AGE_YEARS = 18


# --------------------------------------------------------------------------- #
# 6. Date handling
# --------------------------------------------------------------------------- #

DOB_MIN = date(1900, 1, 1)
COMPANY_FOUNDED = date(2015, 1, 1)   # earlier created_date = backfill artifact


def dob_bounds() -> tuple[date, date]:
    """(earliest, latest) plausible birth date; latest is today, not import time."""
    return DOB_MIN, date.today()


# Tried in order. Month-first precedes day-first: US extract, so 03/04/1985
# is 4 March. Truly ambiguous values are flagged, not silently assigned.
DATE_INPUT_FORMATS = [
    "%Y-%m-%d",        # 1985-03-15 (canonical)
    "%m/%d/%Y",        # 03/15/1985
    "%Y/%m/%d",        # 1985/03/15
    "%d-%m-%Y",        # 15-03-1985
    "%m-%d-%Y",        # 03-15-1985
    "%B %d, %Y",       # March 15, 1985
    "%b %d, %Y",       # Mar 15, 1985
    "%d %b %Y",        # 15 Mar 1985
    "%d-%b-%Y",        # 15-Mar-1985
    "%m/%d/%y",        # 03/15/85
]

DATE_OUTPUT_FORMAT = "%Y-%m-%d"

EXCEL_EPOCH = date(1899, 12, 30)     # offset absorbs Excel's 1900 leap-year bug
UNIX_EPOCH = date(1970, 1, 1)
EXCEL_SERIAL_RE = re.compile(r"^\d{5}$")
EPOCH_SECONDS_RE = re.compile(r"^\d{9,10}$")
COMPACT_DATE_RE = re.compile(r"^\d{8}$")            # YYYYMMDD

# Parse cleanly but mean "unknown"; treated as missing, never as facts.
SENTINEL_DATES = {
    "1900-01-01", "1970-01-01", "9999-12-31", "1111-11-11", "0000-00-00",
}

DATE_TIME_SPLIT_RE = re.compile(r"[ T]")


# --------------------------------------------------------------------------- #
# 7. Phone handling
# --------------------------------------------------------------------------- #

PHONE_OUTPUT_FORMAT = "XXX-XXX-XXXX"
PHONE_EXPECTED_DIGITS = 10

# Extensions must be stripped before the digit count, or valid numbers
# read as "too long".
PHONE_EXTENSION_RE = re.compile(r"\s*(?:x|ext\.?|extension)\s*\d+\s*$", re.I)
PHONE_STRIP_RE = re.compile(r"[()\s.\-]")

PHONE_COUNTRY_PREFIXES = {"+1", "1"}

# Valid but does not fit XXX-XXX-XXXX; preserved and flagged, not reformatted.
PHONE_INTERNATIONAL_RE = re.compile(r"^\+(?!1\b)\d{1,3}[\s\-]")

# Well-formed but meaningless; treated as missing.
PHONE_PLACEHOLDERS = {
    "0000000000", "1111111111", "1234567890", "5555555555", "9999999999",
}

PHONE_VANITY_RE = re.compile(r"[A-Za-z]")   # 555-CAL-LME: remove, never decode


# --------------------------------------------------------------------------- #
# 8. Income handling
# --------------------------------------------------------------------------- #

CURRENCY_SYMBOLS = "$£€¥"
CURRENCY_SUFFIX_RE = re.compile(r"\s*(USD|GBP|EUR|RWF)\s*$", re.I)
CURRENCY_STRIP_RE = re.compile(rf"[{re.escape(CURRENCY_SYMBOLS)},\s]")

ACCOUNTING_NEGATIVE_RE = re.compile(r"^\((.+)\)$")      # (1234) -> -1234

MAGNITUDE_SUFFIXES = {"k": 1_000, "m": 1_000_000}       # 50k -> 50000
MAGNITUDE_RE = re.compile(r"^([\d.,]+)\s*([km])$", re.I)

EUROPEAN_NUMBER_RE = re.compile(r"^-?\d{1,3}(\.\d{3})+,\d{1,2}$")   # 1.234,56

NUMERIC_RANGE_RE = re.compile(r"^-?[\d.,]+\s*-\s*[\d.,]+$")  # flag, don't average

# Numeric but meaning "unset"; named so reports say why, not just "out of range".
INCOME_SENTINEL_VALUES = {-1.0, 999_999_999.0}

INCOME_DECIMAL_PLACES = 2


# --------------------------------------------------------------------------- #
# 9. account_status canonicalization
# --------------------------------------------------------------------------- #

VALID_STATUSES = {"active", "inactive", "suspended"}

# Keys are already lowercased and stripped. Unambiguous mappings only.
STATUS_CANONICAL_MAP = {
    "active": "active",
    "inactive": "inactive",
    "suspended": "suspended",
    "in-active": "inactive",
    "in active": "inactive",
    "in_active": "inactive",
    "actve": "active",
    "actiive": "active",
    "suspnded": "suspended",
    "suspend": "suspended",
    "a": "active",
    "i": "inactive",
    "s": "suspended",
    "dormant": "inactive",      # legacy alias, pre-migration CRM
}

# Recognised but deliberately not mapped: guessing would fabricate account
# state on financial records. Flagged for data-owner review instead.
STATUS_UNMAPPABLE = {
    "closed": "real lifecycle state absent from the allowed set",
    "pending": "real lifecycle state absent from the allowed set",
    "banned": "real lifecycle state absent from the allowed set",
    "true": "boolean encoding with no documented mapping",
    "false": "boolean encoding with no documented mapping",
    "0": "numeric code with no documented codebook",
    "1": "numeric code with no documented codebook",
    "2": "numeric code with no documented codebook",
}


# --------------------------------------------------------------------------- #
# 10. PII classification
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PIIField:
    """category: direct | quasi | sensitive. sensitivity: HIGH | MEDIUM | LOW."""
    category: str
    sensitivity: str
    rationale: str


PII_FIELDS = {
    "first_name": PIIField(
        "direct", "MEDIUM",
        "names a person; low uniqueness alone, high in combination"),
    "last_name": PIIField(
        "direct", "MEDIUM",
        "names a person; family linkage across records"),
    "email": PIIField(
        "direct", "HIGH",
        "unique contact identifier; phishing and credential-stuffing vector"),
    "phone": PIIField(
        "direct", "HIGH",
        "unique contact identifier; SIM-swap and vishing vector"),
    "date_of_birth": PIIField(
        "quasi", "HIGH",
        "immutable; identity-verification factor and re-identification key"),
    "address": PIIField(
        "quasi", "HIGH",
        "physical location; enables real-world harm and household linkage"),
    "income": PIIField(
        "sensitive", "HIGH",
        "financial data; discrimination and targeting risk"),
    "customer_id": PIIField(
        "quasi", "LOW",
        "internal pseudonymous key; identifying only with system access"),
}

NON_PII_COLUMNS = ["account_status", "created_date"]

# Any two of these usually narrow a population to a handful of people.
QUASI_IDENTIFIER_SET = ["date_of_birth", "address", "income"]

# Unanchored: "does this text contain PII anywhere?" Distinct from section 5,
# which asks whether a value is well-formed.
EMAIL_SCAN_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_SCAN_RE = re.compile(
    r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}\b")
SSN_SCAN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CREDIT_CARD_SCAN_RE = re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b")

FREE_TEXT_COLUMNS = ["address", "first_name", "last_name"]


# --------------------------------------------------------------------------- #
# 11. Masking rules
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MaskRule:
    """`strategy` names a function in masker.py; examples are asserted in tests."""
    strategy: str
    example_before: str
    example_after: str


MASKING_RULES = {
    "first_name": MaskRule("initial_only", "John", "J***"),
    "last_name": MaskRule("initial_only", "Doe", "D***"),
    "email": MaskRule("email_local", "john.doe@gmail.com", "j***@gmail.com"),
    "phone": MaskRule("phone_last4", "555-123-4567", "***-***-4567"),
    "address": MaskRule("redact", "123 Main Street, Springfield",
                        "[MASKED ADDRESS]"),
    "date_of_birth": MaskRule("year_only", "1985-03-15", "1985-**-**"),
}

MASK_CHAR = "*"
REDACTED_ADDRESS = "[MASKED ADDRESS]"

# Not masked: the extract exists for analytics and income is the column
# analysts need. Controlled by access policy; revisit if it leaves internal use.
UNMASKED_PII_COLUMNS = ["income"]


# --------------------------------------------------------------------------- #
# 12. Missing-value strategy
# --------------------------------------------------------------------------- #
# drop_row / placeholder / flag_only. Nothing is imputed: an obvious gap beats
# a plausible fiction on financial records.

MISSING_VALUE_STRATEGY = {
    "customer_id": "drop_row",
    "first_name": "placeholder",
    "last_name": "placeholder",
    "email": "flag_only",
    "phone": "flag_only",
    "date_of_birth": "flag_only",
    "address": "flag_only",
    "income": "flag_only",
    "account_status": "placeholder",
    "created_date": "flag_only",
}

# Distinct from the source's own "Unknown" so a filled gap is never mistaken
# for a value the upstream system actually supplied.
MISSING_PLACEHOLDER = "[MISSING]"
STATUS_PLACEHOLDER = "unknown"       # outside VALID_STATUSES, by design

QUARANTINE_THRESHOLD = 5             # rule failures before a row is quarantined
MAX_ACCEPTABLE_FAILURE_RATE = 0.30   # above this, the run fails


# --------------------------------------------------------------------------- #
# 13. Logging
# --------------------------------------------------------------------------- #

LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-16s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

REPORT_WIDTH = 74
REPORT_RULE = "-" * REPORT_WIDTH
REPORT_BORDER = "=" * REPORT_WIDTH
