"""
masker.py
=========
Part 5 — PII masking.

Reads data/cleaned/customers_cleaned.csv, writes
data/masked/customers_masked.csv and reports/masked_sample.txt.

Masking (per the brief, declared in config.MASKING_RULES):
    names   John Doe            -> J***
    email   john.doe@gmail.com  -> j***@gmail.com
    phone   555-123-4567        -> ***-***-4567
    address any                 -> [MASKED ADDRESS]
    DOB     1985-03-15          -> 1985-**-**

Each strategy is checked against the before/after example config records for
it, so a change in masking behaviour fails immediately rather than quietly
shipping under-masked data.

Gaps stay gaps: a missing value or a "[MISSING]" placeholder is passed
through untouched rather than masked into something that looks like data.

Usage:
    python -m src.masker
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import (
    CLEANED_PATH,
    EMAIL_SCAN_RE,
    INPUT_ENCODING,
    ISO_DATE_RE,
    MASK_CHAR,
    MASKED_PATH,
    MASKED_SAMPLE_PATH,
    MASKING_RULES,
    OUTPUT_ENCODING,
    PII_FIELDS,
    QUASI_IDENTIFIER_SET,
    REDACTED_ADDRESS,
    REPORT_BORDER,
    REPORT_RULE,
    SCHEMA,
    UNMASKED_PII_COLUMNS,
    clean_ws,
    is_present,
)

STARS3 = MASK_CHAR * 3
SAMPLE_RECORDS = 6


# --------------------------------------------------------------------------- #
# Strategies — names match MaskRule.strategy in config
# --------------------------------------------------------------------------- #

def mask_initial_only(value: str) -> str:
    """John -> J***  (fixed width, so length is not leaked)."""
    v = clean_ws(value)
    return f"{v[0]}{STARS3}" if v else ""


ADDRESS_SPLIT_RE = re.compile(r"\s*[;,]\s*")
DOMAIN_RE = re.compile(r"[A-Za-z0-9.\-]+")


def _mask_one_address(v: str) -> str:
    if not v:
        return ""
    if "@" not in v:
        # Malformed: no domain worth preserving, mask the lot.
        return f"{v[0]}{STARS3}"
    local, _, domain = v.partition("@")
    head = local[0] if local else MASK_CHAR
    # A real domain has no spaces and no second '@'. If it has either, the
    # cell holds more than one address' worth of text and keeping it would
    # publish the remainder verbatim.
    if not DOMAIN_RE.fullmatch(domain):
        return f"{head}{STARS3}"
    return f"{head}{STARS3}@{domain}"


def mask_email_local(value: str) -> str:
    """john.doe@gmail.com -> j***@gmail.com

    Splits on ; and , first: a cell holding two addresses must have both
    masked, not just the one before the first '@'.
    """
    v = clean_ws(value)
    if not v:
        return ""
    parts = [p for p in ADDRESS_SPLIT_RE.split(v) if p]
    return "; ".join(_mask_one_address(p) for p in parts)


def mask_phone_last4(value: str) -> str:
    """555-123-4567 -> ***-***-4567"""
    v = clean_ws(value)
    if not v:
        return ""
    digits = re.sub(r"\D", "", v)
    tail = digits[-4:] if len(digits) >= 4 else MASK_CHAR * 4
    return f"{STARS3}-{STARS3}-{tail}"


def mask_redact(value: str) -> str:
    """Any address -> [MASKED ADDRESS]"""
    return REDACTED_ADDRESS if clean_ws(value) else ""


def mask_year_only(value: str) -> str:
    """1985-03-15 -> 1985-**-**"""
    v = clean_ws(value)
    if not v:
        return ""
    year = v[:4] if ISO_DATE_RE.fullmatch(v) else MASK_CHAR * 4
    return f"{year}-{MASK_CHAR * 2}-{MASK_CHAR * 2}"


STRATEGIES = {
    "initial_only": mask_initial_only,
    "email_local": mask_email_local,
    "phone_last4": mask_phone_last4,
    "redact": mask_redact,
    "year_only": mask_year_only,
}


def verify_rules() -> list[tuple[str, str, str, str, bool]]:
    """Run every rule against the example config documents for it."""
    checks = []
    for column, rule in MASKING_RULES.items():
        produced = STRATEGIES[rule.strategy](rule.example_before)
        checks.append((column, rule.strategy, rule.example_before,
                       produced, produced == rule.example_after))
    return checks


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #

def mask_value(column: str, value: str) -> str:
    if column not in MASKING_RULES:
        return value
    if not is_present(value):
        return value            # a gap must stay a visible gap
    return STRATEGIES[MASKING_RULES[column].strategy](value)


def mask_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    masked = df.copy()
    counts: dict[str, int] = {}
    for column in MASKING_RULES:
        if column not in masked.columns:
            continue
        original = masked[column]
        masked[column] = original.map(lambda v, c=column: mask_value(c, str(v)))
        counts[column] = int((original.astype(str)
                              != masked[column].astype(str)).sum())
    return masked, counts


def find_leaks(before: pd.DataFrame, after: pd.DataFrame) -> list[str]:
    """PII that survived masking, by whole value or as a readable fragment."""
    leaks = []
    for column in MASKING_RULES:
        if column not in after.columns:
            continue
        survived = sum(
            1 for original, result in zip(before[column].astype(str),
                                          after[column].astype(str))
            if is_present(original) and clean_ws(original) == clean_ws(result))
        if survived:
            leaks.append(f"{column}: {survived} values unchanged by masking")

    # A complete address must never remain readable in a masked email cell.
    if "email" in after.columns:
        intact = sum(1 for cell in after["email"].astype(str)
                     if EMAIL_SCAN_RE.search(cell))
        if intact:
            leaks.append(f"email: {intact} cells still contain a full address")
    return leaks


# --------------------------------------------------------------------------- #
# Re-identification, measured before and after
# --------------------------------------------------------------------------- #

def _k1_rate(df: pd.DataFrame, columns: list[str]) -> tuple[int, int]:
    """(records with a unique combination, comparable records)."""
    usable = [c for c in columns if c in df.columns]
    if not usable:
        return 0, 0
    present = df[usable[0]].map(is_present)
    for column in usable[1:]:
        present &= df[column].map(is_present)
    subset = df.loc[present, usable].apply(lambda s: s.map(clean_ws))
    if subset.empty:
        return 0, 0
    keys = subset.agg("|".join, axis=1)
    sizes = keys.value_counts()
    return int((sizes == 1).sum()), len(keys)


REID_COMBOS = {
    "full name": ["first_name", "last_name"],
    "full name + DOB": ["first_name", "last_name", "date_of_birth"],
    "DOB + address": ["date_of_birth", "address"],
    "DOB + income": ["date_of_birth", "income"],
    "quasi-identifier set": QUASI_IDENTIFIER_SET,
}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def format_sample(before: pd.DataFrame, after: pd.DataFrame,
                  counts: dict[str, int], leaks: list[str],
                  show_raw: bool) -> list[str]:
    out = [
        REPORT_BORDER,
        "MASKED SAMPLE - before / after comparison",
        f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Records: {len(after)}",
        REPORT_BORDER,
    ]
    if show_raw:
        out += [
            "",
            "  NOTE: the BEFORE column below contains unmasked PII and so",
            "  inherits the source data's classification. Safe here because",
            "  the dataset is synthetic; run with show_raw=False for real",
            "  data so this file can be shared.",
        ]

    # ---- 1. rules ----
    out += ["", REPORT_RULE, "1. RULES APPLIED", REPORT_RULE]
    out.append(f"  {'column':<15} {'strategy':<14} {'example':<30} result")
    for column, strategy, example, produced, ok in verify_rules():
        flag = "OK" if ok else "MISMATCH"
        out.append(f"  {column:<15} {strategy:<14} {example:<30} "
                   f"{produced}  [{flag}]")
    out.append("\n  Each row is the strategy run against the example config")
    out.append("  records for it; MISMATCH means masking drifted from spec.")

    unmasked = [c for c in PII_FIELDS if c not in MASKING_RULES]
    out.append(f"\n  PII left unmasked: {', '.join(unmasked)}")
    out.append(f"  Deliberately unmasked: {', '.join(UNMASKED_PII_COLUMNS)} "
               f"(analytics utility)")

    # ---- 2. coverage ----
    out += ["", REPORT_RULE, "2. COVERAGE", REPORT_RULE]
    out.append(f"  {'column':<15} {'masked':>8} {'gaps left':>11}")
    for column in MASKING_RULES:
        gaps = int((~after[column].map(is_present)).sum())
        out.append(f"  {column:<15} {counts.get(column, 0):>8} {gaps:>11}")
    out.append("\n  Gaps are passed through unmasked: masking an empty cell")
    out.append("  would fabricate the appearance of data.")

    # ---- 3. before / after ----
    out += ["", REPORT_RULE, "3. BEFORE / AFTER", REPORT_RULE]
    for position in range(min(SAMPLE_RECORDS, len(after))):
        raw_row = before.iloc[position]
        masked_row = after.iloc[position]
        out.append(f"  record {position + 1} "
                   f"(customer_id {masked_row.get('customer_id', '?')})")
        for column in SCHEMA:
            if column not in after.columns:
                continue
            was, now = str(raw_row[column]), str(masked_row[column])
            if column in MASKING_RULES:
                shown = was if show_raw else "<withheld>"
                out.append(f"    {column:<15} {shown:<34} -> {now}")
            else:
                out.append(f"    {column:<15} {now:<34}    (not masked)")
        out.append("")

    # ---- 4. re-identification ----
    out += [REPORT_RULE, "4. RE-IDENTIFICATION: BEFORE vs AFTER", REPORT_RULE]
    out.append(f"  {'combination':<24} {'k=1 before':>12} {'k=1 after':>11}")
    for label, columns in REID_COMBOS.items():
        b_unique, b_total = _k1_rate(before, columns)
        a_unique, a_total = _k1_rate(after, columns)
        b_pct = 100.0 * b_unique / b_total if b_total else 0.0
        a_pct = 100.0 * a_unique / a_total if a_total else 0.0
        out.append(f"  {label:<24} {b_pct:>11.1f}% {a_pct:>10.1f}%")
    out.append("\n  k=1 is the share of records whose combination is unique in")
    out.append("  the file, and so re-identifiable without any direct name.")

    # ---- 5. residual risk ----
    out += ["", REPORT_RULE, "5. RESIDUAL RISK", REPORT_RULE]
    income_unique, income_total = _k1_rate(after, ["income"])
    income_pct = 100.0 * income_unique / income_total if income_total else 0.0
    out.append(f"  income is unmasked and {income_pct:.1f}% of its values are")
    out.append("  unique, so it stays a quasi-identifier: birth year plus")
    out.append("  income still singles out most records. Masking the other")
    out.append("  fields does not make this extract anonymous, only")
    out.append("  pseudonymous, and it still needs access control.")
    out.append("")
    out.append(f"  customer_id is preserved as the join key, so the masked")
    out.append("  extract can be re-linked to the source by anyone holding")
    out.append("  both files.")

    # ---- 6. verification ----
    out += ["", REPORT_RULE, "6. VERIFICATION", REPORT_RULE]
    failed = [c for c in verify_rules() if not c[4]]
    out.append(f"  rule checks : {len(verify_rules()) - len(failed)} passed, "
               f"{len(failed)} failed")
    out.append(f"  leak checks : {'none' if not leaks else len(leaks)}")
    for leak in leaks:
        out.append(f"    {leak}")
    out += ["", "END OF SAMPLE", REPORT_BORDER]
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(input_path: Path = CLEANED_PATH,
        output_path: Path = MASKED_PATH,
        sample_path: Path = MASKED_SAMPLE_PATH,
        show_raw: bool = True) -> Path:
    for path in (output_path, sample_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    failed = [c for c in verify_rules() if not c[4]]
    if failed:
        raise ValueError(
            "masking strategies no longer match config examples: "
            + ", ".join(f"{c[0]} produced {c[3]!r}" for c in failed))

    before = pd.read_csv(input_path, dtype=str, keep_default_na=False,
                         encoding=INPUT_ENCODING)
    after, counts = mask_frame(before)
    leaks = find_leaks(before, after)

    after.to_csv(output_path, index=False, encoding=OUTPUT_ENCODING,
                 lineterminator="\n")
    sample_path.write_text(
        "\n".join(format_sample(before, after, counts, leaks, show_raw)),
        encoding="utf-8")

    print(f"Masked CSV    -> {output_path}  ({len(after)} rows)")
    print(f"Masked sample -> {sample_path}")
    print(f"  {sum(counts.values())} cells masked across "
          f"{len(MASKING_RULES)} columns, leaks: {len(leaks)}")
    return output_path


if __name__ == "__main__":
    run()
