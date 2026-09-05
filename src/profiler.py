"""
profiler.py
===========
Part 1 — Exploratory Data Quality Analysis.

Profiles data/raw/customers_raw.csv and produces reports/data_quality_report.txt.

Profiling is performed on RAW STRINGS (dtype=str) so that what we report is what
is actually in the file, before any pandas type inference silently "fixes" or
mangles values (e.g. "52,000.50" -> NaN, "0001042" -> 1042).

Report sections:
    1. File-level integrity (BOM, line endings, ragged rows, mojibake)
    2. Completeness (true missing vs null sentinels)
    3. Type inference per column
    4. Format issues (phone, dates, email, names)
    5. Uniqueness (customer_id, email, phone)
    6. Invalid values (income, DOB semantics, address length)
    7. Categorical validity (account_status)
    8. Cross-field anomalies
    9. Priority summary

Usage:
    python -m src.profiler
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

RAW_PATH = Path("data/raw/customers_raw.csv")
REPORT_PATH = Path("reports/data_quality_report.txt")

SCHEMA = [
    "customer_id", "first_name", "last_name", "email", "phone",
    "date_of_birth", "address", "income", "account_status", "created_date",
]

NULL_SENTINELS = {
    "", "null", "none", "nan", "na", "n/a", "#n/a", "-",
    "--", "?", "tbd", "unknown", "not provided",
}

VALID_STATUSES = {"active", "inactive", "suspended"}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_CLEAN_RE = re.compile(r"^\d{3}-\d{3}-\d{4}$")
PHONE_DIGITS_RE = re.compile(r"^\d{10}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '\-][^\W\d_]+)*$", re.UNICODE)

DOB_MIN, DOB_MAX = date(1900, 1, 1), date.today()
INCOME_MAX = 10_000_000

SENTINEL_DATES = ["1900-01-01", "1970-01-01", "9999-12-31", "1111-11-11"]


# --------------------------------------------------------------------------- #
# Loading — defensive, because the file itself may be corrupt
# --------------------------------------------------------------------------- #

def load_raw(path: Path = RAW_PATH) -> tuple[pd.DataFrame, dict]:
    """Load raw CSV as strings, collecting anomalies instead of failing."""
    notes: dict[str, object] = {}

    raw_bytes = path.read_bytes()
    notes["has_bom"] = raw_bytes.startswith(b"\xef\xbb\xbf")
    notes["has_cr_lines"] = b"\r" in raw_bytes

    text = raw_bytes.decode("utf-8-sig", errors="replace")
    notes["has_mojibake"] = ("Ã©" in text or "Ã¼" in text or "â€" in text)
    notes["replacement_chars"] = text.count("\ufffd")

    # Line endings: count \r\n vs lone \n (mixed endings defect).
    notes["crlf_lines"] = text.count("\r\n")
    notes["lf_lines"] = text.count("\n") - text.count("\r\n")

    # Ragged rows: detect with csv.reader so quoted commas are respected.
    # pandas' on_bad_lines callback only fires for too-MANY fields and needs
    # engine="python"; doing it here catches short rows too.
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    header, data_rows = rows[0], rows[1:]

    good_rows, bad_rows = [], []
    for i, row in enumerate(data_rows, start=2):     # line 1 is the header
        if len(row) == len(header):
            good_rows.append(row)
        else:
            bad_rows.append((i, len(row), ",".join(row)))

    notes["ragged_rows"] = bad_rows
    notes["ragged_count"] = len(bad_rows)

    df = pd.DataFrame(good_rows, columns=header, dtype=str)
    notes["column_mismatch"] = list(df.columns) != SCHEMA
    notes["loaded_columns"] = list(df.columns)

    return df, notes


# --------------------------------------------------------------------------- #
# Cell-level helpers
# --------------------------------------------------------------------------- #

def is_null_sentinel(value: str) -> bool:
    """Whitespace + unicode-variant stripping before sentinel comparison."""
    cleaned = (str(value)
               .replace("\u00a0", " ")   # non-breaking space
               .replace("\u200b", "")    # zero-width space
               .replace("\r", "")
               .strip()
               .lower())
    return cleaned in NULL_SENTINELS


def is_present(value: str) -> bool:
    return not is_null_sentinel(value)


def clean_ws(value: str) -> str:
    """Unicode-normalize and strip hidden characters for inspection."""
    return (unicodedata.normalize("NFKC", str(value))
            .replace("\u200b", "")
            .replace("\r", "")
            .strip())


def present_values(df: pd.DataFrame, col: str) -> pd.Series:
    """Cleaned, non-sentinel values for one column."""
    vals = df[col].map(clean_ws)
    return vals[vals.map(is_present)]


# --------------------------------------------------------------------------- #
# Section 2 — Completeness
# --------------------------------------------------------------------------- #

def profile_completeness(df: pd.DataFrame) -> tuple[list[str], pd.DataFrame]:
    lines: list[str] = ["", "-" * 74, "2. COMPLETENESS", "-" * 74]
    n = len(df)
    stats = []
    for col in df.columns:
        sentinel_mask = df[col].map(is_null_sentinel)
        true_missing = int(sentinel_mask.sum())
        stats.append({
            "column": col,
            "missing": true_missing,
            "missing_pct": 100.0 * true_missing / n,
            "present": n - true_missing,
        })
        lines.append(
            f"  {col:<16} {true_missing:>5} missing "
            f"({100.0 * true_missing / n:5.1f}%)"
        )
    worst = max(stats, key=lambda s: s["missing_pct"])
    lines.append(f"\n  Worst column: '{worst['column']}' at "
                 f"{worst['missing_pct']:.1f}% missing")
    lines.append("  NOTE: counts include sentinels (NULL, N/A, nan, '-', "
                 "'unknown'), not just empty cells.")
    return lines, pd.DataFrame(stats)


# --------------------------------------------------------------------------- #
# Section 3 — Type inference
# --------------------------------------------------------------------------- #

def _parseable_date(v: str) -> bool:
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def profile_types(df: pd.DataFrame) -> list[str]:
    lines = ["", "-" * 74, "3. TYPE INFERENCE (non-null values)", "-" * 74]

    def rate(vals: list[str], test) -> float:
        return 100.0 * sum(bool(test(v)) for v in vals) / len(vals)

    def infer(vals: list[str]) -> tuple[str, dict[str, float]]:
        """Return the strictest type ALL values satisfy, plus parse rates.

        Rates matter more than the verdict: one bad cell drops a column to
        'string', so 99%-clean and 0%-clean would otherwise look identical.
        """
        rates = {
            "integer": rate(vals, lambda v: re.fullmatch(r"-?\d+", v)),
            "numeric": rate(vals, lambda v: re.fullmatch(r"-?\d+(\.\d+)?", v)),
            "date": rate(vals, lambda v: ISO_DATE_RE.fullmatch(v)
                         and _parseable_date(v)),
        }
        for kind in ("integer", "numeric", "date"):
            if rates[kind] == 100.0:
                return kind, rates
        return "string", rates

    expected = {
        "customer_id": "integer", "income": "numeric",
        "date_of_birth": "date", "created_date": "date",
    }
    for col in df.columns:
        vals = [clean_ws(v) for v in df[col] if is_present(v)]
        if not vals:
            lines.append(f"  {col:<16} -> all null")
            continue
        got, rates = infer(vals)
        want = expected.get(col, "string")
        if want == "string":
            lines.append(f"  {col:<16} -> {got}")
        else:
            flag = "" if got == want else f"   <-- expected {want}"
            lines.append(f"  {col:<16} -> {got:<8} "
                         f"({rates[want]:5.1f}% parse as {want}){flag}")

    lines.append(
        "\n  NOTE: all columns load as object/str due to dtype=str.\n"
        "  The verdict is the strictest type EVERY value satisfies; the\n"
        "  percentage is the share that parses cleanly. A column reading\n"
        "  'string (97.6% parse as integer)' needs coercion, not redesign."
    )
    return lines


# --------------------------------------------------------------------------- #
# Section 4 — Format issues (phone, dates, email, names)
# --------------------------------------------------------------------------- #

def _try_any_date(v: str) -> bool:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y",
                "%B %d, %Y", "%b %d, %Y", "%d %b %Y", "%d-%b-%Y", "%m/%d/%y"):
        try:
            datetime.strptime(v.split(" x")[0].split("T")[0].strip(), fmt)
            return True
        except ValueError:
            continue
    if re.fullmatch(r"\d{8}", v):        # YYYYMMDD compact
        return _parseable_date(f"{v[:4]}-{v[4:6]}-{v[6:]}")
    if re.fullmatch(r"\d{5}", v):        # Excel serial
        return True
    return False


def profile_formats(df: pd.DataFrame) -> list[str]:
    lines = ["", "-" * 74, "4. FORMAT ISSUES", "-" * 74]

    # ---- phone ----
    phones = present_values(df, "phone")
    dash_std = phones.map(lambda v: bool(PHONE_CLEAN_RE.fullmatch(v)))
    bare = phones.map(lambda v: bool(PHONE_DIGITS_RE.fullmatch(v)))
    other = ~(dash_std | bare)
    lines.append("  phone:")
    lines.append(f"    standard XXX-XXX-XXXX : {int(dash_std.sum())}")
    lines.append(f"    bare 10 digits        : {int(bare.sum())}")
    lines.append(f"    non-standard / broken : {int(other.sum())}")
    for value, count in phones[other].value_counts().head(5).items():
        lines.append(f"      e.g. {value!r} x{count}")

    # ---- dates ----
    for col in ("date_of_birth", "created_date"):
        vals = present_values(df, col)
        iso_shape = vals.map(lambda v: bool(ISO_DATE_RE.fullmatch(v)))
        real_iso = vals.map(lambda v: bool(ISO_DATE_RE.fullmatch(v))
                            and _parseable_date(v))
        impossible = iso_shape & ~real_iso
        other_fmt = ~iso_shape & vals.map(_try_any_date)
        unparseable = ~iso_shape & ~vals.map(_try_any_date)
        lines.append(f"  {col}:")
        lines.append(f"    ISO YYYY-MM-DD        : {int(real_iso.sum())}")
        lines.append(f"    ISO-shaped, impossible: {int(impossible.sum())}")
        lines.append(f"    other parseable format: {int(other_fmt.sum())}")
        lines.append(f"    unparseable garbage   : {int(unparseable.sum())}")
        for value, count in vals[other_fmt].value_counts().head(3).items():
            lines.append(f"      alt format e.g. {value!r} x{count}")
        for value, count in vals[unparseable].value_counts().head(5).items():
            lines.append(f"      garbage e.g. {value!r} x{count}")

    # ---- email ----
    emails = present_values(df, "email")
    bad_email = emails.map(lambda v: not bool(EMAIL_RE.fullmatch(v)))
    upper_email = emails.map(lambda v: v != v.lower())
    lines.append(f"  email: {int(bad_email.sum())} fail basic regex "
                 f"({100.0 * bad_email.mean():.1f}% of present values)")
    lines.append(f"    non-lowercase (cosmetic): {int(upper_email.sum())}")
    for value, count in emails[bad_email].value_counts().head(5).items():
        lines.append(f"      e.g. {value!r} x{count}")

    # ---- names ----
    for col in ("first_name", "last_name"):
        raw = df[col]
        vals = present_values(df, col)
        non_alpha = vals.map(lambda v: not bool(NAME_RE.fullmatch(v)))
        padded = int((raw != raw.map(clean_ws)).sum())
        mixed_case = vals.map(lambda v: v != v.title() and v.replace(" ", "")
                              .replace("-", "").replace("'", "").isalpha())
        lines.append(f"  {col}: {int(non_alpha.sum())} fail alphabetic pattern, "
                     f"{padded} have stray whitespace, "
                     f"{int(mixed_case.sum())} not Title Case")

    return lines


# --------------------------------------------------------------------------- #
# Section 5 — Uniqueness
# --------------------------------------------------------------------------- #

def profile_uniqueness(df: pd.DataFrame) -> list[str]:
    lines = ["", "-" * 74, "5. UNIQUENESS", "-" * 74]
    n = len(df)
    for col in ("customer_id", "email", "phone"):
        vals = present_values(df, col)
        counts = vals.value_counts()
        dups = counts[counts > 1]
        extra = int(dups.sum() - len(dups))
        lines.append(f"  {col}: {n} rows, {len(vals)} present, "
                     f"{extra} duplicate occurrences "
                     f"across {len(dups)} repeated values")
        for value, count in dups.head(5).items():
            lines.append(f"      {value!r} appears {count}x")
        if col == "customer_id" and not dups.empty:
            lines.append("    -> customer_id is NOT unique "
                         "(primary key constraint violated)")
    return lines


# --------------------------------------------------------------------------- #
# Section 6 — Invalid values
# --------------------------------------------------------------------------- #

def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.str.replace(r"[$,\s]", "", regex=True),
                         errors="coerce")


def profile_invalid(df: pd.DataFrame) -> list[str]:
    lines = ["", "-" * 74, "6. INVALID VALUES", "-" * 74]

    # ---- customer_id ----
    ids = present_values(df, "customer_id")
    id_num = pd.to_numeric(ids, errors="coerce")
    lines.append("  customer_id:")
    lines.append(f"    non-numeric           : {int(id_num.isna().sum())}")
    lines.append(f"    <= 0                  : {int((id_num <= 0).sum())}")

    # ---- income ----
    inc = present_values(df, "income")
    inc_num = _numeric(inc)
    lines.append("  income:")
    lines.append(f"    unparseable as number : {int(inc_num.isna().sum())}")
    lines.append(f"    negative              : {int((inc_num < 0).sum())}")
    lines.append(f"    > ${INCOME_MAX:,}          : "
                 f"{int((inc_num > INCOME_MAX).sum())}")
    for value, count in inc[inc_num.isna()].value_counts().head(5).items():
        lines.append(f"      unparseable e.g. {value!r} x{count}")

    # ---- DOB semantics (ISO-parseable subset only) ----
    dob = present_values(df, "date_of_birth")
    parsed = pd.to_datetime(dob, format="%Y-%m-%d", errors="coerce")
    valid = parsed.dropna()
    today = pd.Timestamp(date.today())
    lines.append("  date_of_birth (semantic, ISO-parseable subset only):")
    lines.append(f"    ISO-parseable rows       : {len(valid)} of {len(dob)}")
    lines.append(f"    before 1900 (age > ~150) : "
                 f"{int((valid < pd.Timestamp(DOB_MIN)).sum())}")
    lines.append(f"    in the future            : {int((valid > today).sum())}")
    lines.append(f"    sentinel dates           : "
                 f"{int(valid.isin(pd.to_datetime(SENTINEL_DATES)).sum())}")
    lines.append(f"    born after 2006 (minor)  : "
                 f"{int((valid > pd.Timestamp('2006-12-31')).sum())}")

    # ---- address length ----
    addr = present_values(df, "address")
    lens = addr.map(len)
    lines.append("  address:")
    lines.append(f"    < 10 chars            : {int((lens < 10).sum())}")
    lines.append(f"    > 200 chars           : {int((lens > 200).sum())}")

    # ---- account_status blanks handled in section 7 ----
    return lines


# --------------------------------------------------------------------------- #
# Section 7 — Categorical validity
# --------------------------------------------------------------------------- #

def profile_categorical(df: pd.DataFrame) -> list[str]:
    lines = ["", "-" * 74, "7. CATEGORICAL VALIDITY: account_status", "-" * 74]
    vals = present_values(df, "account_status")
    counts = vals.value_counts()
    exact = int(vals.isin(VALID_STATUSES).sum())
    ci = int(vals.str.lower().isin(VALID_STATUSES).sum())
    lines.append(f"  exact match (already clean) : {exact}")
    lines.append(f"  valid case-insensitively    : {ci}")
    lines.append(f"  recoverable by lowercasing  : {ci - exact}")
    lines.append(f"  invalid / unmappable        : {len(vals) - ci}")
    lines.append(f"  distinct raw values         : {len(counts)}")
    lines.append("  raw value counts:")
    for value, count in counts.items():
        flag = "" if value.lower() in VALID_STATUSES else "   <-- INVALID"
        lines.append(f"    {value!r:<20} {count:>5}{flag}")
    return lines


# --------------------------------------------------------------------------- #
# Section 8 — Cross-field anomalies
# --------------------------------------------------------------------------- #

def profile_cross_field(df: pd.DataFrame) -> list[str]:
    lines = ["", "-" * 74, "8. CROSS-FIELD ANOMALIES", "-" * 74]

    created = pd.to_datetime(df["created_date"].map(clean_ws).str.slice(0, 10),
                             format="%Y-%m-%d", errors="coerce")
    dob = pd.to_datetime(df["date_of_birth"].map(clean_ws).str.slice(0, 10),
                         format="%Y-%m-%d", errors="coerce")

    both = created.notna() & dob.notna()
    lines.append(f"  comparable row pairs (both dates ISO): {int(both.sum())}")
    lines.append(f"  created_date before date_of_birth : "
                 f"{int((both & (created < dob)).sum())}")
    lines.append(f"  under 18 at account creation      : "
                 f"{int((both & ((created - dob).dt.days < 18 * 365.25)).sum())}")

    income_num = _numeric(df["income"].map(clean_ws))
    status = df["account_status"].map(clean_ws).str.lower()
    lines.append(f"  income = 0 while active           : "
                 f"{int(((income_num == 0) & (status == 'active')).sum())}")

    email_present = df["email"].map(lambda v: is_present(clean_ws(v)))
    dup_emails = int(df.loc[email_present, "email"].duplicated(keep=False).sum())
    lines.append(f"  duplicate emails across rows      : {dup_emails}")

    addr_present = df["address"].map(lambda v: is_present(clean_ws(v)))
    addr_counts = df.loc[addr_present, "address"].value_counts()
    shared = addr_counts[addr_counts >= 10]
    lines.append(f"  addresses shared by >=10 rows     : {len(shared)} distinct "
                 f"({int(shared.sum())} rows involved)")

    identical = int(df.duplicated(keep=False).sum())
    lines.append(f"  fully duplicated rows             : {identical}")
    return lines


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #

def run(input_path: Path = RAW_PATH,
        report_path: Path = REPORT_PATH) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)

    df, notes = load_raw(input_path)
    n = len(df)

    out: list[str] = []
    out.append("=" * 74)
    out.append("DATA QUALITY REPORT - customers_raw.csv")
    out.append(f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}")
    out.append(f"Rows loaded: {n}   Expected schema: {len(SCHEMA)} columns")
    out.append("=" * 74)

    # ---- Section 1 ----
    out += ["", "-" * 74, "1. FILE-LEVEL INTEGRITY", "-" * 74]
    out.append(f"  BOM on header              : "
               f"{'YES' if notes['has_bom'] else 'no'}")
    out.append(f"  line endings               : "
               f"{notes['crlf_lines']} CRLF vs {notes['lf_lines']} LF")
    out.append(f"  mojibake detected          : "
               f"{'YES' if notes['has_mojibake'] else 'no'}")
    out.append(f"  U+FFFD replacement chars   : {notes['replacement_chars']}")
    out.append(f"  ragged rows (excluded)     : {notes['ragged_count']}")
    for i, (lineno, width, content) in enumerate(notes["ragged_rows"][:5], 1):
        out.append(f"    {i}. line {lineno}: {width} fields - {content[:60]}...")
    if notes["column_mismatch"]:
        out.append("  WARNING: loaded columns differ from expected schema")
        out.append(f"    got     : {notes['loaded_columns']}")
        out.append(f"    expected: {SCHEMA}")
    else:
        out.append("  header matches expected schema : yes")

    # ---- Sections 2-8 ----
    completeness_lines, completeness_df = profile_completeness(df)
    out += completeness_lines
    out += profile_types(df)
    out += profile_formats(df)
    out += profile_uniqueness(df)
    out += profile_invalid(df)
    out += profile_categorical(df)
    out += profile_cross_field(df)

    # ---- Section 9: priority summary ----
    out += ["", "-" * 74, "9. PRIORITY SUMMARY", "-" * 74]

    ids = present_values(df, "customer_id")
    id_dups = int((ids.value_counts() > 1).sum())
    status_vals = present_values(df, "account_status")
    bad_status = int((~status_vals.str.lower().isin(VALID_STATUSES)).sum())

    if id_dups:
        out.append(f"  [HIGH]   customer_id not unique - {id_dups} repeated "
                   f"values; primary key unusable until resolved")
    for _, row in completeness_df.nlargest(3, "missing_pct").iterrows():
        sev = "HIGH" if row["missing_pct"] >= 20 else "MEDIUM"
        out.append(f"  [{sev}] '{row['column']}' {row['missing_pct']:.1f}% "
                   f"missing ({int(row['missing'])} rows)")
    if bad_status:
        out.append(f"  [HIGH]   account_status has {bad_status} values outside "
                   f"the allowed set")
    out += [
        "  [MEDIUM] non-standard date/phone formats require normalization",
        "  [MEDIUM] PII present in all rows (see pii_detection_report.txt)",
        "  [LOW]    file artifacts (BOM, line endings) - handled at load time",
        "",
        "END OF REPORT",
        "=" * 74,
    ]

    report_path.write_text("\n".join(out), encoding="utf-8")
    print(f"Report written -> {report_path}")
    return report_path


if __name__ == "__main__":
    run()