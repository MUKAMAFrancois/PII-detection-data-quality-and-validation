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
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .config import (
    ADDRESS_MAX_LEN,
    ADDRESS_MIN_LEN,
    ADULT_AGE_YEARS,
    CUSTOMER_ID_MIN,
    DATE_INPUT_FORMATS,
    DATE_OUTPUT_FORMAT,
    EMAIL_RE,
    INCOME_MAX,
    INPUT_ENCODING,
    ISO_DATE_RE,
    MAX_AGE_YEARS,
    NAME_RE,
    PHONE_CLEAN_RE,
    PHONE_DIGITS_RE,
    QUALITY_REPORT_PATH,
    RAW_PATH,
    REPORT_BORDER,
    REPORT_RULE,
    SCHEMA,
    SENTINEL_DATES,
    STATUS_CANONICAL_MAP,
    STATUS_UNMAPPABLE,
    VALID_STATUSES,
    clean_ws,
    dob_bounds,
    is_null_sentinel,
    is_present,
    rel,
)

DOB_MIN, TODAY = dob_bounds()


# --------------------------------------------------------------------------- #
# Loading — defensive, because the file itself may be corrupt
# --------------------------------------------------------------------------- #

def load_raw(path: Path = RAW_PATH) -> tuple[pd.DataFrame, dict]:
    """Load raw CSV as strings, collecting anomalies instead of failing."""
    notes: dict[str, object] = {}

    raw_bytes = path.read_bytes()
    notes["has_bom"] = raw_bytes.startswith(b"\xef\xbb\xbf")
    notes["has_cr_lines"] = b"\r" in raw_bytes

    text = raw_bytes.decode(INPUT_ENCODING, errors="replace")
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
# clean_ws / is_null_sentinel / is_present come from config.

def present_values(df: pd.DataFrame, col: str) -> pd.Series:
    """Cleaned, non-sentinel values for one column."""
    vals = df[col].map(clean_ws)
    return vals[vals.map(is_present)]


# --------------------------------------------------------------------------- #
# Section 2 — Completeness
# --------------------------------------------------------------------------- #

def profile_completeness(df: pd.DataFrame) -> tuple[list[str], pd.DataFrame]:
    lines: list[str] = ["", REPORT_RULE, "2. COMPLETENESS", REPORT_RULE]
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
    lines = ["", REPORT_RULE, "3. TYPE INFERENCE (non-null values)", REPORT_RULE]

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
    for fmt in DATE_INPUT_FORMATS:
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
    lines = ["", REPORT_RULE, "4. FORMAT ISSUES", REPORT_RULE]

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
    lines = ["", REPORT_RULE, "5. UNIQUENESS", REPORT_RULE]
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
    lines = ["", REPORT_RULE, "6. INVALID VALUES", REPORT_RULE]

    # ---- customer_id ----
    ids = present_values(df, "customer_id")
    id_num = pd.to_numeric(ids, errors="coerce")
    lines.append("  customer_id:")
    lines.append(f"    {'non-numeric':<22}: {int(id_num.isna().sum())}")
    lines.append(f"    {f'< {CUSTOMER_ID_MIN}':<22}: "
                 f"{int((id_num < CUSTOMER_ID_MIN).sum())}")

    # ---- income ----
    inc = present_values(df, "income")
    inc_num = _numeric(inc)
    lines.append("  income:")
    lines.append(f"    {'unparseable as number':<22}: {int(inc_num.isna().sum())}")
    lines.append(f"    {'negative':<22}: {int((inc_num < 0).sum())}")
    lines.append(f"    {f'> ${INCOME_MAX:,.0f}':<22}: "
                 f"{int((inc_num > INCOME_MAX).sum())}")
    for value, count in inc[inc_num.isna()].value_counts().head(5).items():
        lines.append(f"      unparseable e.g. {value!r} x{count}")

    # ---- DOB semantics (ISO-parseable subset only) ----
    dob = present_values(df, "date_of_birth")
    parsed = pd.to_datetime(dob, format=DATE_OUTPUT_FORMAT, errors="coerce")
    valid = parsed.dropna()
    today = pd.Timestamp(TODAY)
    # "0000-00-00" is a sentinel no parser accepts, hence coerce + dropna.
    sentinels = pd.to_datetime(sorted(SENTINEL_DATES), format=DATE_OUTPUT_FORMAT,
                               errors="coerce").dropna()
    adult_cutoff = today - pd.DateOffset(years=ADULT_AGE_YEARS)
    lines.append("  date_of_birth (semantic, ISO-parseable subset only):")
    lines.append(f"    {'ISO-parseable rows':<25}: {len(valid)} of {len(dob)}")
    lines.append(f"    {f'before {DOB_MIN:%Y} (age > {MAX_AGE_YEARS})':<25}: "
                 f"{int((valid < pd.Timestamp(DOB_MIN)).sum())}")
    lines.append(f"    {'in the future':<25}: {int((valid > today).sum())}")
    lines.append(f"    {'sentinel dates':<25}: "
                 f"{int(valid.isin(sentinels).sum())}")
    lines.append(f"    {f'under {ADULT_AGE_YEARS} today':<25}: "
                 f"{int((valid > adult_cutoff).sum())}")

    # ---- address length ----
    addr = present_values(df, "address")
    lens = addr.map(len)
    lines.append("  address:")
    lines.append(f"    {f'< {ADDRESS_MIN_LEN} chars':<22}: "
                 f"{int((lens < ADDRESS_MIN_LEN).sum())}")
    lines.append(f"    {f'> {ADDRESS_MAX_LEN} chars':<22}: "
                 f"{int((lens > ADDRESS_MAX_LEN).sum())}")

    # ---- account_status blanks handled in section 7 ----
    return lines


# --------------------------------------------------------------------------- #
# Section 7 — Categorical validity
# --------------------------------------------------------------------------- #

def profile_categorical(df: pd.DataFrame) -> list[str]:
    lines = ["", REPORT_RULE, "7. CATEGORICAL VALIDITY: account_status", REPORT_RULE]
    vals = present_values(df, "account_status")
    counts = vals.value_counts()
    keys = vals.str.lower()

    exact = int(vals.isin(VALID_STATUSES).sum())
    ci = int(keys.isin(VALID_STATUSES).sum())
    mappable = int(keys.isin(STATUS_CANONICAL_MAP).sum())
    unmappable = int(keys.isin(STATUS_UNMAPPABLE).sum())
    unknown = len(vals) - mappable - unmappable

    lines.append(f"  exact match (already clean)  : {exact}")
    lines.append(f"  valid case-insensitively     : {ci}")
    lines.append(f"  recoverable by lowercasing   : {ci - exact}")
    lines.append(f"  recoverable via canonical map: {mappable - ci}")
    lines.append(f"  known but unmappable         : {unmappable}")
    lines.append(f"  unrecognised entirely        : {unknown}")
    lines.append(f"  distinct raw values          : {len(counts)}")
    lines.append("  raw value counts:")
    for value, count in counts.items():
        key = value.lower()
        if key in VALID_STATUSES:
            flag = ""
        elif key in STATUS_CANONICAL_MAP:
            flag = f"   -> {STATUS_CANONICAL_MAP[key]}"
        elif key in STATUS_UNMAPPABLE:
            flag = f"   <-- UNMAPPABLE ({STATUS_UNMAPPABLE[key]})"
        else:
            flag = "   <-- UNRECOGNISED"
        lines.append(f"    {value!r:<20} {count:>5}{flag}")
    return lines


# --------------------------------------------------------------------------- #
# Section 8 — Cross-field anomalies
# --------------------------------------------------------------------------- #

def profile_cross_field(df: pd.DataFrame) -> list[str]:
    lines = ["", REPORT_RULE, "8. CROSS-FIELD ANOMALIES", REPORT_RULE]

    created = pd.to_datetime(df["created_date"].map(clean_ws).str.slice(0, 10),
                             format=DATE_OUTPUT_FORMAT, errors="coerce")
    dob = pd.to_datetime(df["date_of_birth"].map(clean_ws).str.slice(0, 10),
                         format=DATE_OUTPUT_FORMAT, errors="coerce")

    both = created.notna() & dob.notna()
    lines.append(f"  comparable row pairs (both dates ISO): {int(both.sum())}")
    lines.append(f"  created_date before date_of_birth : "
                 f"{int((both & (created < dob)).sum())}")
    minor_days = ADULT_AGE_YEARS * 365.25
    lines.append(f"  under {ADULT_AGE_YEARS} at account creation      : "
                 f"{int((both & ((created - dob).dt.days < minor_days)).sum())}")

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
        report_path: Path = QUALITY_REPORT_PATH) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)

    df, notes = load_raw(input_path)
    n = len(df)

    out: list[str] = []
    out.append(REPORT_BORDER)
    out.append("DATA QUALITY REPORT - customers_raw.csv")
    out.append(f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}")
    out.append(f"Rows loaded: {n}   Expected schema: {len(SCHEMA)} columns")
    out.append(REPORT_BORDER)

    # ---- Section 1 ----
    out += ["", REPORT_RULE, "1. FILE-LEVEL INTEGRITY", REPORT_RULE]
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
    out += ["", REPORT_RULE, "9. PRIORITY SUMMARY", REPORT_RULE]

    ids = present_values(df, "customer_id")
    id_dups = int((ids.value_counts() > 1).sum())
    status_vals = present_values(df, "account_status").str.lower()
    bad_status = int((~status_vals.isin(STATUS_CANONICAL_MAP)).sum())

    if id_dups:
        out.append(f"  [HIGH]   customer_id not unique - {id_dups} repeated "
                   f"values; primary key unusable until resolved")
    for _, row in completeness_df.nlargest(3, "missing_pct").iterrows():
        sev = "HIGH" if row["missing_pct"] >= 20 else "MEDIUM"
        out.append(f"  [{sev}] '{row['column']}' {row['missing_pct']:.1f}% "
                   f"missing ({int(row['missing'])} rows)")
    if bad_status:
        out.append(f"  [HIGH]   account_status has {bad_status} values no "
                   f"canonical mapping can recover; needs data-owner review")
    out += [
        "  [MEDIUM] non-standard date/phone formats require normalization",
        "  [MEDIUM] PII present in all rows (see pii_detection_report.txt)",
        "  [LOW]    file artifacts (BOM, line endings) - handled at load time",
        "",
        "END OF REPORT",
        REPORT_BORDER,
    ]

    report_path.write_text("\n".join(out), encoding="utf-8")
    print(f"Report written -> {rel(report_path)}")
    return report_path


if __name__ == "__main__":
    run()