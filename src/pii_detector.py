"""
pii_detector.py
===============
Part 2 — PII detection.

Scans data/raw/customers_raw.csv and produces reports/pii_detection_report.txt.

Report sections:
    1. PII inventory (category, sensitivity, rationale)
    2. Regex detection (email, phone)
    3. Free-text leakage (PII in fields that should not carry it)
    4. Occurrence counts and uniqueness
    5. Re-identification risk
    6. Breach impact assessment
    7. Summary

Every example in the report is redacted: a PII report that prints PII is a
breach of its own.

Usage:
    python -m src.pii_detector
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import (
    CREDIT_CARD_SCAN_RE,
    EMAIL_RE,
    EMAIL_SCAN_RE,
    FREE_TEXT_COLUMNS,
    NON_PII_COLUMNS,
    PHONE_CLEAN_RE,
    PHONE_SCAN_RE,
    PII_FIELDS,
    PII_REPORT_PATH,
    QUASI_IDENTIFIER_SET,
    RAW_PATH,
    REPORT_BORDER,
    REPORT_RULE,
    SSN_SCAN_RE,
    clean_ws,
    is_present,
)
from .profiler import load_raw, present_values

SENSITIVITY_WEIGHT = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

DIRECT_IDENTIFIERS = [c for c, f in PII_FIELDS.items() if f.category == "direct"]
CONTACT_FIELDS = ["email", "phone"]


# --------------------------------------------------------------------------- #
# Redaction for report examples
# --------------------------------------------------------------------------- #

STARS = 8   # fixed width, so a redaction never reveals the original length


def redact(value: str) -> str:
    """Shape-preserving placeholder so no example carries real PII."""
    v = clean_ws(value)
    if not v:
        return "''"
    if "@" in v:
        local, _, domain = v.partition("@")
        return f"{local[:1]}{'*' * STARS}@{domain}"
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 7:
        return f"***-***-{digits[-4:]}"
    return f"{v[:1]}{'*' * min(len(v) - 1, STARS)}"


def _examples(series: pd.Series, mask: pd.Series, limit: int = 5,
              pattern: re.Pattern | None = None) -> list[str]:
    """Redacted sample values; with `pattern`, the matched substring only."""
    hits = series[mask]
    if pattern is not None:
        hits = hits.map(lambda v: m.group(0) if (m := pattern.search(v)) else v)
    return [f"      e.g. {redact(v)} x{n}"
            for v, n in hits.value_counts().head(limit).items()]


# --------------------------------------------------------------------------- #
# Section 1 — PII inventory
# --------------------------------------------------------------------------- #

def detect_inventory(df: pd.DataFrame) -> list[str]:
    lines = ["", REPORT_RULE, "1. PII INVENTORY", REPORT_RULE]
    n = len(df)

    for category in ("direct", "quasi", "sensitive"):
        cols = [c for c, f in PII_FIELDS.items() if f.category == category]
        plural = "column" if len(cols) == 1 else "columns"
        lines.append(f"  {category.upper()} IDENTIFIERS ({len(cols)} {plural})")
        for col in cols:
            field = PII_FIELDS[col]
            populated = int(df[col].map(is_present).sum()) if col in df else 0
            lines.append(f"    {col:<15} {field.sensitivity:<7} "
                         f"{populated:>5}/{n} populated")
            lines.append(f"      {field.rationale}")

    lines.append(f"  NON-PII: {', '.join(NON_PII_COLUMNS)}")
    lines.append(f"\n  {len(PII_FIELDS)} of {len(df.columns)} columns carry PII.")
    return lines


# --------------------------------------------------------------------------- #
# Section 2 — Regex detection
# --------------------------------------------------------------------------- #

def detect_regex(df: pd.DataFrame) -> tuple[list[str], dict[str, int]]:
    lines = ["", REPORT_RULE, "2. REGEX DETECTION", REPORT_RULE]
    found: dict[str, int] = {}

    # ---- email ----
    emails = present_values(df, "email")
    well_formed = emails.map(lambda v: bool(EMAIL_RE.fullmatch(v)))
    contains = emails.map(lambda v: bool(EMAIL_SCAN_RE.search(v)))
    found["email"] = int(contains.sum())
    lines.append("  email  (pattern: something@domain.tld)")
    lines.append(f"    present values        : {len(emails)}")
    lines.append(f"    well-formed addresses : {int(well_formed.sum())} "
                 f"({100.0 * well_formed.mean():.1f}%)")
    lines.append(f"    contain an address    : {int(contains.sum())}")
    lines.append(f"    malformed but present : "
                 f"{int((contains & ~well_formed).sum())}")
    lines.append(f"    no address detected   : {int((~contains).sum())}")
    lines += _examples(emails, contains & ~well_formed)

    # ---- phone ----
    phones = present_values(df, "phone")
    standard = phones.map(lambda v: bool(PHONE_CLEAN_RE.fullmatch(v)))
    detected = phones.map(lambda v: bool(PHONE_SCAN_RE.search(v)))
    found["phone"] = int(detected.sum())
    lines.append("  phone  (pattern: optional +CC, then 3-3-4 digits)")
    lines.append(f"    present values        : {len(phones)}")
    lines.append(f"    standard XXX-XXX-XXXX : {int(standard.sum())} "
                 f"({100.0 * standard.mean():.1f}%)")
    lines.append(f"    detected as a phone   : {int(detected.sum())}")
    lines.append(f"    non-standard but real : "
                 f"{int((detected & ~standard).sum())}")
    lines.append(f"    no phone detected     : {int((~detected).sum())}")
    lines += _examples(phones, detected & ~standard)

    lines.append(f"\n  Regex recall: {found['email']} emails and "
                 f"{found['phone']} phones recovered from raw text.")
    return lines, found


# --------------------------------------------------------------------------- #
# Section 3 — Free-text leakage
# --------------------------------------------------------------------------- #

def detect_leakage(df: pd.DataFrame) -> list[str]:
    lines = ["", REPORT_RULE, "3. FREE-TEXT LEAKAGE", REPORT_RULE]
    lines.append("  Contact and financial identifiers found in columns that")
    lines.append("  are not declared as holding them.")

    patterns = {
        "email": EMAIL_SCAN_RE,
        "phone": PHONE_SCAN_RE,
        "SSN": SSN_SCAN_RE,
        "credit card": CREDIT_CARD_SCAN_RE,
    }

    total = 0
    for col in FREE_TEXT_COLUMNS:
        vals = present_values(df, col)
        hits = {label: vals.map(lambda v, p=pat: bool(p.search(v)))
                for label, pat in patterns.items()}
        counts = {label: int(m.sum()) for label, m in hits.items()}
        total += sum(counts.values())
        summary = ", ".join(f"{label}={n}" for label, n in counts.items())
        lines.append(f"  {col:<12} {summary}")
        for label, mask in hits.items():
            if counts[label]:
                lines.append(f"    {label} matches:")
                lines += _examples(vals, mask, limit=3,
                                   pattern=patterns[label])

    lines.append(f"\n  Total leaked identifiers: {total}")
    if not total:
        lines.append("  No contact or financial data leaked into free text.")
    return lines


# --------------------------------------------------------------------------- #
# Section 4 — Occurrence counts
# --------------------------------------------------------------------------- #

def count_occurrences(df: pd.DataFrame) -> tuple[list[str], dict[str, int]]:
    lines = ["", REPORT_RULE, "4. OCCURRENCE COUNTS", REPORT_RULE]
    n = len(df)
    populated: dict[str, int] = {}

    lines.append(f"  {'column':<15} {'populated':>9} {'distinct':>9} "
                 f"{'unique':>7} {'shared':>7}")
    for col in PII_FIELDS:
        if col not in df.columns:
            continue
        vals = present_values(df, col)
        counts = vals.value_counts()
        unique = int((counts == 1).sum())
        shared = int((counts > 1).sum())
        populated[col] = len(vals)
        lines.append(f"  {col:<15} {len(vals):>9} {len(counts):>9} "
                     f"{unique:>7} {shared:>7}")

    cells = sum(populated.values())
    lines.append(f"\n  PII cells populated: {cells} across {n} records")
    lines.append(f"  Average PII fields per record: {cells / n:.1f} of "
                 f"{len(PII_FIELDS)}")
    lines.append("  'unique' values identify exactly one record and carry the")
    lines.append("  highest re-identification risk.")
    return lines, populated


# --------------------------------------------------------------------------- #
# Section 5 — Re-identification risk
# --------------------------------------------------------------------------- #

def _combo_key(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    """Row key over cols, dropping rows where any part is missing."""
    present = df[cols[0]].map(is_present)
    for col in cols[1:]:
        present &= df[col].map(is_present)
    subset = df.loc[present, cols].apply(lambda s: s.map(clean_ws))
    return subset.agg("|".join, axis=1)


def assess_reidentification(df: pd.DataFrame) -> list[str]:
    lines = ["", REPORT_RULE, "5. RE-IDENTIFICATION RISK", REPORT_RULE]
    n = len(df)

    combos = {
        "full name": ["first_name", "last_name"],
        "full name + DOB": ["first_name", "last_name", "date_of_birth"],
        "DOB + address": ["date_of_birth", "address"],
        "quasi-identifier set": QUASI_IDENTIFIER_SET,
    }

    for label, cols in combos.items():
        keys = _combo_key(df, cols)
        if keys.empty:
            continue
        sizes = keys.value_counts()
        singletons = int((sizes == 1).sum())
        lines.append(f"  {label}")
        lines.append(f"    comparable records : {len(keys)}")
        lines.append(f"    distinct combos    : {len(sizes)}")
        lines.append(f"    k=1 (unique)       : {singletons} "
                     f"({100.0 * singletons / len(keys):.1f}% of comparable)")
        lines.append(f"    largest group      : k={int(sizes.max())}")

    direct = df[DIRECT_IDENTIFIERS].apply(
        lambda row: any(is_present(v) for v in row), axis=1)
    contactable = df[CONTACT_FIELDS].apply(
        lambda row: any(is_present(v) for v in row), axis=1)
    lines.append(f"\n  Records with >=1 direct identifier : {int(direct.sum())}"
                 f" of {n}")
    lines.append(f"  Records directly contactable       : "
                 f"{int(contactable.sum())} of {n}")
    lines.append("  k=1 means the combination is unique in this file, so")
    lines.append("  removing names alone would not anonymise those records.")
    return lines


# --------------------------------------------------------------------------- #
# Section 6 — Breach impact
# --------------------------------------------------------------------------- #

def assess_breach_impact(df: pd.DataFrame,
                         populated: dict[str, int]) -> list[str]:
    lines = ["", REPORT_RULE, "6. BREACH IMPACT ASSESSMENT", REPORT_RULE]
    n = len(df)

    exposure = 0
    lines.append(f"  {'column':<15} {'sensitivity':<12} {'cells':>7} "
                 f"{'weight':>7} {'score':>8}")
    for col, count in populated.items():
        field = PII_FIELDS[col]
        weight = SENSITIVITY_WEIGHT[field.sensitivity]
        score = count * weight
        exposure += score
        lines.append(f"  {col:<15} {field.sensitivity:<12} {count:>7} "
                     f"{weight:>7} {score:>8}")

    max_score = n * sum(SENSITIVITY_WEIGHT[f.sensitivity]
                        for f in PII_FIELDS.values())
    lines.append(f"  {'TOTAL':<15} {'':<12} {sum(populated.values()):>7} "
                 f"{'':>7} {exposure:>8}")
    lines.append(f"\n  Exposure score: {exposure} of a possible {max_score} "
                 f"({100.0 * exposure / max_score:.1f}%)")

    high = [c for c, f in PII_FIELDS.items() if f.sensitivity == "HIGH"]
    lines.append(f"  HIGH-sensitivity columns: {', '.join(high)}")

    lines += [
        "",
        "  If this file leaked:",
        f"    - {populated.get('email', 0)} email addresses and "
        f"{populated.get('phone', 0)} phone numbers become a direct",
        "      phishing, smishing and credential-stuffing target list.",
        f"    - {populated.get('date_of_birth', 0)} birth dates combine with "
        "names to defeat knowledge-based",
        "      identity verification; unlike a password, a DOB cannot be reset.",
        f"    - {populated.get('address', 0)} home addresses expose customers "
        "to physical risk and",
        "      allow household-level linkage.",
        f"    - {populated.get('income', 0)} income figures are financial data, "
        "carrying discrimination",
        "      and targeting risk for a fintech's customer base.",
        "",
        "  Aggravating factor: this is a financial services dataset, so a",
        "  breach is reportable and the records support both identity theft",
        "  and account takeover without further enrichment.",
    ]
    return lines


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #

def run(input_path: Path = RAW_PATH,
        report_path: Path = PII_REPORT_PATH) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)

    df, notes = load_raw(input_path)
    n = len(df)

    out = [
        REPORT_BORDER,
        "PII DETECTION REPORT - customers_raw.csv",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Rows scanned: {n}   Ragged rows excluded: {notes['ragged_count']}",
        REPORT_BORDER,
    ]

    out += detect_inventory(df)
    regex_lines, found = detect_regex(df)
    out += regex_lines
    out += detect_leakage(df)
    count_lines, populated = count_occurrences(df)
    out += count_lines
    out += assess_reidentification(df)
    out += assess_breach_impact(df, populated)

    # ---- Section 7: summary ----
    out += ["", REPORT_RULE, "7. SUMMARY", REPORT_RULE]
    high = sum(1 for f in PII_FIELDS.values() if f.sensitivity == "HIGH")
    out += [
        f"  {len(PII_FIELDS)} PII columns, {high} rated HIGH sensitivity.",
        f"  {sum(populated.values())} populated PII cells across {n} records.",
        f"  Regex recovered {found['email']} emails and {found['phone']} phones.",
        "",
        "  Required before this data is shared:",
        "    [HIGH]   mask direct identifiers (see masked_sample.txt)",
        "    [HIGH]   mask DOB and address; names alone are not the risk",
        "    [MEDIUM] restrict income by access policy rather than masking",
        "    [MEDIUM] keep the raw file access-controlled and never in a repo",
        "",
        "END OF REPORT",
        REPORT_BORDER,
    ]

    report_path.write_text("\n".join(out), encoding="utf-8")
    print(f"Report written -> {report_path}")
    return report_path


if __name__ == "__main__":
    run()
