"""
dataset_generator.py
====================
Simulates the messy `customers_raw.csv` for the PII Detection & Data Quality
Validation Pipeline lab.


Defect coverage:
  - Section 1:  File & encoding level      -> _post_process_csv_text()
  - Section 2:  Null sentinels             -> _sentinel() / _pick()
  - Section 3:  customer_id                -> _gen_customer_id()
  - Section 4:  first_name / last_name     -> _gen_name()
  - Section 5:  email                      -> _gen_email()
  - Section 6:  phone                      -> _gen_phone()
  - Section 7:  date_of_birth              -> _gen_dob()
  - Section 8:  address                    -> _gen_address()
  - Section 9:  income                     -> _gen_income()
  - Section 10: account_status             -> _gen_status()
  - Section 11: created_date               -> _gen_created_date()
  - Section 12: Cross-field rules          -> _inject_cross_field_defects()

Usage:
    python -m src.dataset_generator [num_rows] [--seed 42]
    python -m src.dataset_generator 3000 --defect-rate 0.05

Output:
    data/raw/customers_raw.csv
    data/raw/defect_manifest.csv   (only with --manifest)
"""

from __future__ import annotations

import argparse
import csv
import io
import random
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SCHEMA = [
    "customer_id", "first_name", "last_name", "email", "phone",
    "date_of_birth", "address", "income", "account_status", "created_date",
]

RAW_PATH = Path("data/raw/customers_raw.csv")
MANIFEST_PATH = Path("data/raw/defect_manifest.csv")

SEED = 42

# Per-column probabilities. Row-level clean rate is (1 - NULL - DEFECT) ** 10.
DEFECT_RATE = 0.030
NULL_RATE = 0.015

SENTINELS = ["NULL", "null", "None", "NaN", "NA", "N/A", "n/a",
             "#N/A", "-", "--", "?", "TBD", "Unknown", "Not Provided", "   "]

# A defect is (tag, relative_weight, zero-arg factory). Factories are lazy so
# only the chosen variant is ever evaluated.
Defect = tuple[str, int, Callable[[], str]]

# --------------------------------------------------------------------------- #
# Instrumentation
# --------------------------------------------------------------------------- #

defect_log: dict[str, int] = {}
manifest: list[dict[str, str]] = []
_row_index: int = -1
_next_id: int = 1001


def _reset_state() -> None:
    """Make generate() safe to call more than once per process."""
    global _row_index, _next_id
    defect_log.clear()
    manifest.clear()
    _row_index = -1
    _next_id = 1001


def _log(column: str, tag: str, row: int | None = None) -> None:
    defect_log[tag] = defect_log.get(tag, 0) + 1
    manifest.append({
        "row_index": str(_row_index if row is None else row),
        "column": column,
        "defect": tag,
    })


def _weighted(pairs: Sequence[tuple[Any, int]]) -> Any:
    values, weights = zip(*pairs)
    return random.choices(values, weights=weights, k=1)[0]


def _sentinel() -> str:
    return random.choice(SENTINELS)


def _pick(column: str,
          clean: str | Callable[[], str],
          defects: Sequence[Defect],
          defect_rate: float | None = None,
          null_rate: float | None = None) -> str:
    """Return a clean value, a null sentinel, or exactly one defect variant."""
    dr = DEFECT_RATE if defect_rate is None else defect_rate
    nr = NULL_RATE if null_rate is None else null_rate

    roll = random.random()
    if roll < nr:
        _log(column, "null_sentinel")
        return _sentinel()
    if roll < nr + dr and defects:
        tag, factory = _weighted([((t, f), w) for t, w, f in defects])
        _log(column, tag)
        return factory()
    return clean() if callable(clean) else clean


# --------------------------------------------------------------------------- #
# Section 3 — customer_id
# --------------------------------------------------------------------------- #


def _gen_customer_id() -> str:
    """Defect variants keep the underlying id recoverable where realistic."""
    global _next_id
    clean = str(_next_id)
    _next_id += 1
    return _pick("customer_id", clean, [
        ("id_negative",        2, lambda: f"-{random.randint(1, 999)}"),
        ("id_zero",            2, lambda: "0"),
        ("id_non_numeric",     3, lambda: f"AB-{random.randint(1000, 9999)}"),
        ("id_float_roundtrip", 3, lambda: f"{clean}.0"),
        ("id_leading_zeros",   2, lambda: f"{int(clean):07d}"),
        ("id_scientific",      2, lambda: f"{int(clean) / 1000:.3E}"),
    ])


# --------------------------------------------------------------------------- #
# Section 4 — first_name / last_name
# --------------------------------------------------------------------------- #

# Valid-but-awkward names live in the CLEAN pool on purpose: a naive
# "alphabetic only" rule will flag them, and that false positive is the lesson.
CLEAN_FIRST_NAMES = [
    "John", "Jane", "Michael", "Sarah", "David", "Emma", "Daniel", "Grace",
    "Peter", "Aline", "Chloe", "Samuel", "Nadia", "Thomas", "Leila",
    "José", "Müller", "Ngũgĩ", "Mary-Jane", "O'Brien", "D'Arcy",
]
CLEAN_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Davis", "Miller", "Wilson",
    "Moore", "Taylor", "Anderson", "Thomas", "Jackson", "Uwase", "Habimana",
    "D'Angelo", "Müller", "Ngũgĩ", "O'Brien", "van der Berg", "de la Cruz",
]

DIRTY_FIRST_NAMES = ["J0hn", "John!!", "J", "T" * 55, "Test", "asdf", "XXX",
                     "J.", "Jr.", "III", "John 🙂", "123", "N/A Name"]
DIRTY_LAST_NAMES = ["Sm1th", "Smith!!", "S", "T" * 55, "Test", "asdf", "XXX",
                    "Jr.", "III", "Smith 🙂", "456", "-"]

_CASE_DEFECTS: list[tuple[str, int, Callable[[str], str]]] = [
    ("name_lowercase",   18, str.lower),
    ("name_uppercase",   18, str.upper),
    ("name_mixed_case",  12, lambda n: "".join(
        c.upper() if i % 2 else c.lower() for i, c in enumerate(n))),
    ("name_padded",      14, lambda n: f"  {n} "),
    ("name_nbsp",         6, lambda n: f"{n}\u00a0"),
    ("name_zero_width",   6, lambda n: f"{n}\u200b"),
]


def _gen_name(column: str, clean_pool: list[str],
              dirty_pool: list[str]) -> str:
    defects: list[Defect] = [
        ("name_junk_token", 30, lambda: random.choice(dirty_pool)),
    ]
    defects += [
        (tag, weight, lambda f=fn: f(random.choice(clean_pool)))
        for tag, weight, fn in _CASE_DEFECTS
    ]
    return _pick(column, lambda: random.choice(clean_pool), defects)


def _gen_first_name() -> str:
    return _gen_name("first_name", CLEAN_FIRST_NAMES, DIRTY_FIRST_NAMES)


def _gen_last_name() -> str:
    return _gen_name("last_name", CLEAN_LAST_NAMES, DIRTY_LAST_NAMES)


# --------------------------------------------------------------------------- #
# Section 5 — email
# --------------------------------------------------------------------------- #

_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "company.co.uk"]

_TRANSLIT = str.maketrans({
    "é": "e", "è": "e", "ü": "u", "ö": "o", "ã": "a", "ĩ": "i", "ũ": "u",
    "-": "", "'": "", " ": ".",
})


def _email_base(first: str, last: str) -> str:
    """Local part built from names, tolerant of blank/sentinel inputs."""
    head = first.strip().lower().translate(_TRANSLIT) or "user"
    tail = last.strip().lower().translate(_TRANSLIT) or "x"
    return f"{head}.{tail}"


def _gen_email(first: str, last: str) -> str:
    base = _email_base(first, last)

    def clean() -> str:
        # Plus-addressing and multi-part TLDs are valid and need no cleaning,
        # so they belong here, not in the defect list.
        return _weighted([
            (f"{base}@{random.choice(_DOMAINS)}", 80),
            (f"{base}+newsletter@gmail.com", 10),
            (f"{base}@mail.company.co.uk", 10),
        ])

    return _pick("email", clean, [
        ("email_missing_at",      8, lambda: f"{base}_{random.choice(_DOMAINS)}"),
        ("email_missing_domain",  7, lambda: f"{base}@"),
        ("email_double_at",       6, lambda: f"{base}@@{random.choice(_DOMAINS)}"),
        ("email_no_tld",          7, lambda: f"{base}@gmail"),
        ("email_inner_space",     6, lambda: f"{base} @gmail.com"),
        ("email_wrong_order",     5, lambda: f"@gmail.com.{base}"),
        ("email_consecutive_dot", 6, lambda: f"{base}..doe@x.com"),
        ("email_leading_dot",     5, lambda: f".{base}@x.com"),
        ("email_mailto_prefix",   5, lambda: f"mailto:{base}@x.com"),
        ("email_two_addresses",   5, lambda: f"{base}@x.com; {base}2@y.com"),
        ("email_mixed_case",     10, lambda: f"{base.title()}@GMAIL.COM"),
        ("email_padded",          6, lambda: f" {base}@gmail.com "),
        ("email_trailing_dot",    4, lambda: f"{base}@gmail.com."),
    ])


# --------------------------------------------------------------------------- #
# Section 6 — phone
# --------------------------------------------------------------------------- #


def _gen_phone() -> str:
    digits = (f"{random.randint(200, 999)}"
              f"{random.randint(100, 999):03d}"
              f"{random.randint(0, 9999):04d}")
    d1, d2, d3 = digits[:3], digits[3:6], digits[6:]

    return _pick("phone", f"{d1}-{d2}-{d3}", [
        ("phone_parens",        10, lambda: f"({d1}) {d2}-{d3}"),
        ("phone_dots",           9, lambda: f"{d1}.{d2}.{d3}"),
        ("phone_bare_digits",    9, lambda: digits),
        ("phone_spaces",         6, lambda: f"{d1} {d2} {d3}"),
        ("phone_mixed_seps",     5, lambda: f"{d1}-{d2}.{d3}"),
        ("phone_country_us",     7, lambda: f"+1 {d1}-{d2}-{d3}"),
        ("phone_country_uk",     4, lambda: "+44 20 7946 0958"),
        ("phone_country_rw",     4, lambda: "+250 788 123 456"),
        ("phone_extension",      5, lambda: f"{d1}-{d2}-{d3} x89"),
        ("phone_too_short",      6, lambda: f"{d1}-{random.randint(1000, 9999)}"),
        ("phone_too_long",       4, lambda: f"{digits}9"),
        ("phone_vanity",         4, lambda: "555-CAL-LME"),
        ("phone_scientific",     3, lambda: "5.55123E+09"),
        ("phone_dropped_zero",   4, lambda: str(random.randint(100000000, 999999999))),
        ("phone_placeholder",    6, lambda: random.choice(
            ["000-000-0000", "111-111-1111", "123-456-7890"])),
    ])


# --------------------------------------------------------------------------- #
# Section 7 — date_of_birth
# --------------------------------------------------------------------------- #


def _random_dob() -> date:
    """Realistic adult DOB between 1940-01-01 and 2006-12-31."""
    return date.fromordinal(random.randint(
        date(1940, 1, 1).toordinal(), date(2006, 12, 31).toordinal()))


def _gen_dob() -> str:
    d = _random_dob()

    return _pick("date_of_birth", d.isoformat(), [
        # Parseable but wrongly formatted -> needs conversion.
        ("dob_us_format",      12, lambda: d.strftime("%m/%d/%Y")),
        ("dob_slash_iso",       6, lambda: d.strftime("%Y/%m/%d")),
        ("dob_day_first",       5, lambda: d.strftime("%d-%m-%Y")),
        ("dob_long_form",       5, lambda: d.strftime("%B %d, %Y")),
        ("dob_abbrev_month",    4, lambda: d.strftime("%d-%b-%Y")),
        ("dob_with_time",       5, lambda: f"{d.isoformat()} 00:00:00"),
        ("dob_tz_aware",        3, lambda: f"{d.isoformat()}T00:00:00+02:00"),
        ("dob_two_digit_year",  4, lambda: d.strftime("%m/%d/%y")),
        ("dob_excel_serial",    4, lambda: str((d - date(1899, 12, 30)).days)),
        ("dob_epoch_seconds",   3, lambda: str((d - date(1970, 1, 1)).days * 86400)),
        ("dob_ambiguous_dm",    3, lambda: "03/04/1985"),
        # Unparseable or semantically wrong.
        ("dob_invalid_literal", 5, lambda: "invalid_date"),
        ("dob_impossible_day",  4, lambda: "1985-02-30"),
        ("dob_mysql_zero",      4, lambda: "0000-00-00"),
        ("dob_epoch_sentinel",  3, lambda: "1900-01-01"),
        ("dob_max_placeholder", 3, lambda: random.choice(["9999-12-31", "1111-11-11"])),
        ("dob_age_over_150",    4, lambda: "1820-05-01"),
        ("dob_future",          4, lambda: "2030-01-01"),
        ("dob_underage",        4, lambda: "2010-06-01"),
    ])


# --------------------------------------------------------------------------- #
# Section 8 — address
# --------------------------------------------------------------------------- #

_STREETS = ["Main Street", "Oak Avenue", "Maple Drive", "Elm Boulevard",
            "Cedar Lane", "Pine Road", "Washington Blvd", "Lake Shore Drive"]
_CITIES = ["Springfield", "Riverside", "Franklin", "Greenville", "Bristol",
           "Clinton", "Fairview", "Salem"]


def _gen_address() -> str:
    num = random.randint(1, 9999)
    street = random.choice(_STREETS)
    city = random.choice(_CITIES)

    def clean() -> str:
        return _weighted([
            (f"{num} {street}, {city}", 82),
            (f"{num} {street}, Apt {random.randint(1, 50)}, {city}", 18),
        ])

    def exactly_200() -> str:
        head = f"{num} {street}, {city} "
        return (head + "x" * (200 - len(head)))[:200]

    return _pick("address", clean, [
        # < 10 chars, genuinely violating the floor.
        ("address_too_short",    10, lambda: f"{num} St"),
        ("address_boundary_10",   6, lambda: f"{num} Main St"[:10]),
        ("address_exactly_200",   5, exactly_200),
        ("address_over_200",      8, lambda: f"{num} {street}, "
                                             + "Suite " * 40 + city),
        ("address_numeric_only",  8, lambda: str(random.randint(10000, 99999))),
        ("address_all_caps",     12, lambda: f"{num} {street.upper()}, {city.upper()}"),
        ("address_padded",        9, lambda: f"  {num} {street},  {city}  "),
        ("address_junk",          8, lambda: random.choice(
            ["same as above", "see notes", "n/a - remote"])),
        ("address_no_city",       8, lambda: f"{num} {street}"),
    ])


# --------------------------------------------------------------------------- #
# Section 9 — income
# --------------------------------------------------------------------------- #


def _european(value: float) -> str:
    """1.234,56 — dot thousands, comma decimal."""
    return f"{value:,.2f}".translate(str.maketrans({",": ".", ".": ","}))


def _gen_income() -> str:
    income = round(random.uniform(15_000, 250_000), 2)

    return _pick("income", f"{income:.2f}", [
        ("income_negative",       6, lambda: f"-{random.randint(1000, 20000)}"),
        ("income_over_10m",       5, lambda: str(random.randint(11_000_000, 99_000_000))),
        ("income_boundary_10m",   3, lambda: "10000000.00"),
        ("income_zero",           5, lambda: "0"),
        ("income_currency_sym",   9, lambda: f"${income:,.0f}"),
        ("income_currency_sfx",   6, lambda: f"{income:.0f} USD"),
        ("income_comma_thousands", 9, lambda: f"{income:,.2f}"),
        ("income_european",       6, lambda: _european(income)),
        ("income_accounting_neg", 5, lambda: f"({random.randint(1000, 20000)})"),
        ("income_shorthand_k",    5, lambda: f"{random.randint(20, 99)}k"),
        ("income_range",          4, lambda: f"{income:.0f}-{income + 10000:.0f}"),
        ("income_spelled_out",    3, lambda: "fifty thousand"),
        ("income_neg_sentinel",   4, lambda: "-1"),
        ("income_max_sentinel",   4, lambda: "999999999"),
        ("income_excess_precision", 5, lambda: f"{income:.6f}"),
        ("income_padded",         4, lambda: f" {income:.2f} "),
    ])


# --------------------------------------------------------------------------- #
# Section 10 — account_status
# --------------------------------------------------------------------------- #

_VALID_STATUSES = ["active", "inactive", "suspended"]


def _gen_status() -> str:
    s = random.choice(_VALID_STATUSES)

    return _pick("account_status", s, [
        ("status_capitalised",   12, lambda: s.capitalize()),
        ("status_uppercase",      9, lambda: s.upper()),
        ("status_padded",        10, lambda: f" {s} "),
        ("status_hyphen_variant", 5, lambda: "in-active"),
        ("status_space_variant",  4, lambda: "in active"),
        ("status_typo",           7, lambda: random.choice(["actve", "actiive", "suspnded"])),
        ("status_unknown_value",  9, lambda: random.choice(["closed", "pending", "banned"])),
        ("status_legacy_value",   6, lambda: "dormant"),
        ("status_letter_code",    6, lambda: random.choice("AIS")),
        ("status_numeric_code",   6, lambda: str(random.randint(0, 2))),
        ("status_boolean",        5, lambda: random.choice(["TRUE", "FALSE"])),
    ])


# --------------------------------------------------------------------------- #
# Section 11 — created_date
# --------------------------------------------------------------------------- #


def _gen_created_date() -> str:
    d = date.fromordinal(random.randint(
        date(2015, 1, 1).toordinal(), date(2024, 12, 31).toordinal()))

    return _pick("created_date", d.isoformat(), [
        ("created_us_format",     12, lambda: d.strftime("%m/%d/%Y")),
        ("created_with_time",     12, lambda: f"{d.isoformat()} "
                                              f"{random.randint(0, 23):02d}:"
                                              f"{random.randint(0, 59):02d}:10"),
        ("created_tz_offset",      5, lambda: f"{d.isoformat()}T14:32:10+02:00"),
        ("created_two_digit_year", 6, lambda: d.strftime("%m/%d/%y")),
        ("created_excel_serial",   5, lambda: str((d - date(1899, 12, 30)).days)),
        ("created_impossible",     6, lambda: random.choice(["2023-13-45", "2023-02-30"])),
        ("created_future",         6, lambda: "2027-01-01"),
        ("created_pre_founding",   5, lambda: "1990-01-01"),
    ])


# --------------------------------------------------------------------------- #
# Row assembly
# --------------------------------------------------------------------------- #


def _generate_row(index: int) -> dict[str, str]:
    global _row_index
    _row_index = index

    first = _gen_first_name()
    last = _gen_last_name()

    return {
        "customer_id": _gen_customer_id(),
        "first_name": first,
        "last_name": last,
        "email": _gen_email(first, last),
        "phone": _gen_phone(),
        "date_of_birth": _gen_dob(),
        "address": _gen_address(),
        "income": _gen_income(),
        "account_status": _gen_status(),
        "created_date": _gen_created_date(),
    }


# --------------------------------------------------------------------------- #
# Section 12 — Cross-field defect injection
# --------------------------------------------------------------------------- #


def _sample_idx(rows: list[dict[str, str]], k: int) -> list[int]:
    """Sample row indices, clamped so small datasets never raise."""
    return random.sample(range(len(rows)), min(k, len(rows)))


def _disjoint_pairs(rows: list[dict[str, str]],
                    k: int) -> list[tuple[int, int]]:
    """k non-overlapping (source, target) index pairs."""
    idx = _sample_idx(rows, 2 * k)
    return list(zip(idx[::2], idx[1::2]))


def _inject_cross_field_defects(rows: list[dict[str, str]]) -> None:
    """Mutates `rows` in place. Deliberate, targeted, always-fires injections."""
    n = len(rows)

    # (a) Bulk-import block: identical creation timestamp
    for i in _sample_idx(rows, min(200, max(2, n // 15))):
        rows[i]["created_date"] = "2021-03-15 09:00:00"
        _log("created_date", "bulk_import_block", i)

    # (b) Shared address (fraud signal)
    for i in _sample_idx(rows, min(20, max(2, n // 150))):
        rows[i]["address"] = "666 Fraud Lane, Springfield"
        _log("address", "shared_address", i)

    # (c) Duplicate emails across different customer_ids
    for src, dst in _disjoint_pairs(rows, min(10, n // 4)):
        rows[dst]["email"] = rows[src]["email"]
        _log("email", "duplicate_email", dst)

    # (d) Duplicate phones across different customer_ids
    for src, dst in _disjoint_pairs(rows, min(8, n // 4)):
        rows[dst]["phone"] = rows[src]["phone"]
        _log("phone", "duplicate_phone", dst)

    # (e) Duplicate customer_id — Part 1 asks students to test uniqueness,
    #     so this must exist rather than depend on accidental collision.
    for src, dst in _disjoint_pairs(rows, min(12, n // 4)):
        rows[dst]["customer_id"] = rows[src]["customer_id"]
        _log("customer_id", "duplicate_customer_id", dst)

    # (f) created_date strictly before date_of_birth — literals, not a swap,
    #     so the defect fires regardless of what was generated.
    for i in _sample_idx(rows, min(5, max(1, n // 400))):
        rows[i]["date_of_birth"] = "1998-07-14"
        rows[i]["created_date"] = "1994-02-08"
        _log("created_date", "created_before_dob", i)

    # (g) Age < 18 at account creation (KYC violation)
    for i in _sample_idx(rows, min(5, max(1, n // 400))):
        rows[i]["date_of_birth"] = "2015-06-01"
        rows[i]["created_date"] = "2023-01-01"
        _log("date_of_birth", "underage_kyc", i)

    # (h) income = 0 while active (contradiction)
    for i in _sample_idx(rows, min(5, max(1, n // 400))):
        rows[i]["income"] = "0"
        rows[i]["account_status"] = "active"
        _log("income", "zero_income_active", i)

    # (i) Full name crammed into first_name, last_name blank
    for i in _sample_idx(rows, min(8, max(1, n // 300))):
        rows[i]["first_name"] = (f"{random.choice(CLEAN_FIRST_NAMES)} "
                                 f"{random.choice(CLEAN_LAST_NAMES)}")
        rows[i]["last_name"] = ""
        _log("first_name", "full_name_in_first_name", i)

    # (j) first/last swapped. Email is left untouched so the disagreement
    #     between name order and email local part makes this detectable.
    for i in _sample_idx(rows, min(15, max(1, n // 200))):
        rows[i]["first_name"], rows[i]["last_name"] = \
            rows[i]["last_name"], rows[i]["first_name"]
        _log("first_name", "name_swapped", i)

    # (k) Fully duplicated rows. MUST run last: any injection after this one
    #     could mutate a single member of a pair and silently un-duplicate it.
    for src, dst in _disjoint_pairs(rows, min(6, n // 4)):
        rows[dst] = dict(rows[src])
        _log("__row__", "duplicate_row", dst)


# --------------------------------------------------------------------------- #
# Section 1 — File & encoding level corruption (post-processing)
# --------------------------------------------------------------------------- #


def _find_line(lines: list[str], needle: str, start: int = 1) -> int | None:
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i
    return None


def _write_ragged_rows(buf: str) -> str:
    """Append raw rows with 11 and 9 fields (extra / missing field)."""
    extra = "9001,Extra,Field,row,with,eleven,fields,like,this,one,here"
    missing = "9002,Missing,Some,Fields,row,with,only,nine,fields"
    if len(extra.split(",")) != 11 or len(missing.split(",")) != 9:
        raise ValueError("ragged row field counts drifted from 11 / 9")
    _log("__file__", "ragged_row_11_fields", -1)
    _log("__file__", "ragged_row_9_fields", -1)
    return buf + extra + "\n" + missing + "\n"


def _post_process_csv_text(buf: str) -> str:
    """Applies Section 1 file-level corruptions. Every _log call is guarded."""

    buf = "\ufeff" + buf
    _log("__file__", "bom_header", -1)

    lines = buf.split("\n")

    # Unquote an address containing a comma -> genuinely malformed record.
    target = _find_line(lines, '"', start=200)
    if target is not None:
        lines[target] = lines[target].replace('"', "", 2)
        _log("__file__", "unquoted_comma", -1)

    # Raw newline inside an UNQUOTED field. The writer quotes any address
    # containing a comma, and a newline inside a quoted field is legal
    # RFC 4180 that parses cleanly -- so the quotes must come off too.
    i = _find_line(lines, " Street,", start=300)
    if i is not None and '"' in lines[i]:
        lines[i] = (lines[i].replace('"', "", 2)
                            .replace(" Street,", " Street\nApt 4,", 1))
        _log("__file__", "embedded_newline", -1)

    # Stray quote inside an unquoted name field. Both csv engines read this
    # literally, so it is a CONTENT defect (name contains a quote character),
    # not a parse failure. Name-validation rules should catch it.
    i = _find_line(lines, ",Smith,", start=40)
    if i is not None:
        lines[i] = lines[i].replace(",Smith,", ',Smith "Johnny",', 1)
        _log("__file__", "quote_in_name_field", -1)

    buf = "\n".join(lines)

    # Mojibake: double-encoded UTF-8.
    if "José" in buf:
        buf = buf.replace("José", "JosÃ©", 1)
        _log("__file__", "mojibake", -1)

    # Mixed line endings on ~5% of physical lines.
    buf = "\n".join(
        line + "\r" if random.random() < 0.05 else line
        for line in buf.split("\n")
    )
    _log("__file__", "mixed_line_endings", -1)

    return buf


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def generate(num_rows: int = 3000,
             seed: int = SEED,
             output: Path = RAW_PATH,
             manifest_path: Path | None = None,
             file_corruption: bool = True) -> Path:
    _reset_state()
    random.seed(seed)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Generating {num_rows} customer rows "
          f"(defect={DEFECT_RATE:.1%}, null={NULL_RATE:.1%})...")
    rows = [_generate_row(i) for i in range(num_rows)]

    print("Injecting cross-field defects (Section 12)...")
    _inject_cross_field_defects(rows)

    dirty_rows = {m["row_index"] for m in manifest if m["row_index"] != "-1"}
    clean_rows = num_rows - len(dirty_rows)

    sio = io.StringIO()
    writer = csv.DictWriter(sio, fieldnames=SCHEMA, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    buf = sio.getvalue()

    if file_corruption:
        print("Applying file-level corruption (Section 1)...")
        buf = _write_ragged_rows(buf)
        buf = _post_process_csv_text(buf)

    # newline="" disables the platform newline translation that would otherwise
    # rewrite every "\n" as "\r\n" on Windows, erasing the deliberate mix above.
    output.write_text(buf, encoding="utf-8", newline="")

    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", newline="", encoding="utf-8") as fh:
            mw = csv.DictWriter(fh, fieldnames=["row_index", "column", "defect"])
            mw.writeheader()
            mw.writerows(manifest)

    print(f"\nSaved -> {output}")
    if manifest_path is not None:
        print(f"Manifest -> {manifest_path}")
    pct = f"{clean_rows / num_rows:.1%}" if num_rows else "n/a"
    print(f"Clean rows: {clean_rows}/{num_rows} ({pct})")
    print(f"\nDefect injection summary ({sum(defect_log.values())} total):")
    for defect, count in sorted(defect_log.items(), key=lambda x: -x[1]):
        print(f"  {defect:<28} {count}")
    return output


def main() -> None:
    global DEFECT_RATE, NULL_RATE

    parser = argparse.ArgumentParser(
        description="Generate messy customers_raw.csv")
    parser.add_argument("num_rows", nargs="?", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, default=RAW_PATH)
    parser.add_argument("--manifest", type=Path, nargs="?", default=None,
                        const=MANIFEST_PATH,
                        help="also write the defect ground-truth CSV "
                             f"(default path: {MANIFEST_PATH})")
    parser.add_argument("--defect-rate", type=float, default=DEFECT_RATE,
                        help="per-column probability of a defect value")
    parser.add_argument("--null-rate", type=float, default=NULL_RATE,
                        help="per-column probability of a null sentinel")
    parser.add_argument("--no-file-corruption", action="store_true",
                        help="skip Section 1 BOM/ragged/encoding corruption")
    args = parser.parse_args()

    if args.defect_rate + args.null_rate >= 1:
        parser.error("--defect-rate + --null-rate must be < 1")

    DEFECT_RATE = args.defect_rate
    NULL_RATE = args.null_rate

    generate(args.num_rows, args.seed, args.output, args.manifest,
             file_corruption=not args.no_file_corruption)


if __name__ == "__main__":
    main()
