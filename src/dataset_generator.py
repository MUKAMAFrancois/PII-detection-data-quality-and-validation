"""
dataset_generator.py
====================
Simulates the messy `customers_raw.csv` for the PII Detection & Data Quality
Validation Pipeline lab.

Defect coverage:
  - Section 1:  File & encoding level      -> _post_process_csv_text()
  - Section 2:  Null sentinels            -> _sentinel() / _maybe_sentinel()
  - Section 3:  customer_id               -> _gen_customer_id()
  - Section 4:  first_name / last_name    -> _gen_name()
  - Section 5:  email                     -> _gen_email()
  - Section 6:  phone                     -> _gen_phone()
  - Section 7:  date_of_birth             -> _gen_dob()
  - Section 8:  address                   -> _gen_address()
  - Section 9:  income                    -> _gen_income()
  - Section 10: account_status            -> _gen_status()
  - Section 11: created_date              -> _gen_created_date()
  - Section 12: Cross-field rules         -> _inject_cross_field_defects()

Usage:
    python -m src.dataset_generator [num_rows] [--seed 42]

Output:
    data/raw/customers_raw.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
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

SEED = 42

# Roughly 70% clean / 30% defective across value-level generators.
CLEAN_WEIGHT = 70

# Null sentinel pool — scattered by every column generator.
SENTINELS = ["NULL", "null", "None", "NaN", "NA", "N/A", "n/a",
             "#N/A", "-", "--", "?", "TBD", "Unknown", "Not Provided", "   "]

# Defect counter.
defect_log: dict[str, int] = {}


def _log(defect: str) -> None:
    defect_log[defect] = defect_log.get(defect, 0) + 1


def _weighted(pairs: Sequence[tuple[Any, int]]) -> Any:
    values, weights = zip(*pairs)
    return random.choices(values, weights=weights, k=1)[0]


_SENTINEL_MARK = object()


def _maybe_sentinel(pairs: Sequence[tuple[Any, int]],
                    sentinel_rate: int = 4) -> str:
    """With some probability, return a null sentinel instead of a value."""
    # The marker defers _sentinel() until it is actually chosen, so the
    # defect counter records emitted sentinels rather than offered ones.
    value = _weighted(list(pairs) + [(_SENTINEL_MARK, sentinel_rate)])
    return _sentinel() if value is _SENTINEL_MARK else value


def _sentinel() -> str:
    _log("null_sentinel")
    return random.choice(SENTINELS)


# --------------------------------------------------------------------------- #
# Section 3 — customer_id
# --------------------------------------------------------------------------- #

_next_id = 1001


def _gen_customer_id() -> str:
    global _next_id
    clean = f"{_next_id}"
    _next_id += 1
    return _maybe_sentinel([
        (clean, CLEAN_WEIGHT),
        (f"-{random.randint(1, 999)}", 2),                    # negative
        ("0", 2),                                             # zero
        (f"AB-{random.randint(1000, 9999)}", 3),              # non-numeric
        (f"{random.randint(1000, 9999)}.0", 3),               # float round-trip
        (f"{random.randint(1000, 9999):07d}", 2),             # leading zeros
        (f"{random.randint(1000, 9999) / 1000:.3E}", 2),      # scientific
    ])


# --------------------------------------------------------------------------- #
# Section 4 — first_name / last_name
# --------------------------------------------------------------------------- #

FIRST_NAMES = ["John", "Jane", "Michael", "Sarah", "David", "Emma", "José",
               "Müller", "Ngũgĩ", "Mary-Jane", "O'Brien", "John 🙂",
               "J0hn", "John!!", "J", "T" * 55, "Test", "asdf", "XXX",
               "J.", " Jr.", "III", "van der", "de la", "Van"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "D'Angelo", "Müller",
              "Ngũgĩ", "Mary-Jane", "O'Brien", "Smith 🙂", "Sm1th", "Smith!!",
              "S", "T" * 55, "Test", "asdf", "XXX", "Jr.", "III",
              "der Berg", "la Cruz", "Berg"]

_CASE_VARIANTS = [
    (lambda n: n, 40),                                   # as-is
    (lambda n: n.lower(), 15),                           # lowercase
    (lambda n: n.upper(), 15),                           # UPPERCASE
    (lambda n: "".join(c.upper() if i % 2 else c.lower()
                       for i, c in enumerate(n)), 10),   # MiXeD
    (lambda n: f"  {n} ", 10),                           # surrounding spaces
    (lambda n: f"{n}\u00a0", 5),                         # non-breaking space
    (lambda n: f"{n}\u200b", 5),                         # zero-width char
]


def _gen_name(pool: list[str]) -> str:
    name = _maybe_sentinel([
        (random.choice(pool), CLEAN_WEIGHT),
        (random.choice(pool), 100),   # names pool already contains defects
    ])
    fn = _weighted(_CASE_VARIANTS)
    return fn(name)


def _gen_first_name() -> str:
    # "Full name in first_name + empty last_name" field misuse (Section 4)
    if random.random() < 0.01:
        _log("full_name_in_first_name")
        return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    return _gen_name(FIRST_NAMES)


def _gen_last_name() -> str:
    return _gen_name(LAST_NAMES)


# --------------------------------------------------------------------------- #
# Section 5 — email
# --------------------------------------------------------------------------- #

_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "company.co.uk"]


def _name_token(value: str, fallback: str) -> str:
    """First token of a name, tolerant of blank/sentinel values."""
    parts = value.lower().split()
    return parts[0] if parts else fallback


def _gen_email(first: str, last: str) -> str:
    base = f"{_name_token(first, 'user')}.{_name_token(last, 'x')}"
    base = base.replace("é", "e").replace("-", "")
    clean = f"{base}@{random.choice(_DOMAINS)}"
    return _maybe_sentinel([
        (clean, CLEAN_WEIGHT),
        (f"{first}_{random.choice(_DOMAINS)}", 3),              # missing @
        (f"{first}@", 3),                                       # missing domain
        (f"{first}@@{random.choice(_DOMAINS)}", 3),             # double @
        (f"{first}@{random.choice(_DOMAINS)}", 3),              # no TLD
        (f"{base} @gmail.com", 3),                              # space inside
        (f"@gmail.com.{base}", 3),                              # wrong order
        (f"{base}..doe@x.com", 3),                              # consecutive dots
        (f".{base}@x.com", 3),                                  # leading dot
        (f"mailto:{base}@x.com", 2),                            # mailto prefix
        (f"{base}@x.com; {base}2@y.com", 2),                    # two addresses
        (f"{base}+newsletter@gmail.com", 4),                    # plus addressing
        (f"{base}@mail.company.co.uk", 4),                      # multi-part TLD
        (f"{base.title()}@GMAIL.COM", 3),                       # mixed case
    ])


# --------------------------------------------------------------------------- #
# Section 6 — phone
# --------------------------------------------------------------------------- #


def _gen_phone() -> str:
    digits = f"{random.randint(200, 999)}{random.randint(100, 999):03d}" \
             f"{random.randint(0, 9999):04d}"
    d1, d2, d3 = digits[:3], digits[3:6], digits[6:]
    return _maybe_sentinel([
        (f"{d1}-{d2}-{d3}", CLEAN_WEIGHT),
        (f"({d1}) {d2}-{d3}", 8),
        (f"{d1}.{d2}.{d3}", 8),
        (f"{d1}{d2}{d3}", 8),
        (f"{d1}-{d2}.{d3}", 4),                                 # mixed seps
        (f"+1 {d1}-{d2}-{d3}", 6),
        (f"+44 20 7946 0958", 3),
        (f"+250 788 123 456", 3),
        (f"{d1}-{d2}-{d3} x89", 4),                             # extension
        (f"{d1}-{random.randint(1000, 9999)}", 4),              # too short
        (f"{digits}9", 3),                                      # too long
        (f"555-CAL-LME", 3),                                    # vanity
        (f"5.55123E+09", 2),                                    # sci notation
        (f"{random.randint(100000000, 999999999)}", 3),         # dropped zero
        ("000-000-0000", 2),                                    # placeholder junk
        ("111-111-1111", 2),
        ("123-456-7890", 2),
    ])


# --------------------------------------------------------------------------- #
# Section 7 — date_of_birth
# --------------------------------------------------------------------------- #


def _fmt_dob(d: date) -> str:
    """Random parse variant of a real date (Section 7)."""
    return _weighted([
        (d.isoformat(), 45),                                    # ISO baseline
        (d.strftime("%m/%d/%Y"), 12),                           # US format
        (d.strftime("%Y/%m/%d"), 6),                            # slash ISO
        (d.strftime("%d-%m-%Y"), 5),                            # swapped
        (d.strftime("%B %d, %Y"), 5),                           # long form
        (d.strftime("%d-%b-%Y"), 4),                            # abbreviated
        (f"{d.isoformat()} 00:00:00", 5),                       # with time
        (f"{d.isoformat()}T00:00:00+02:00", 2),                 # tz-aware
        (d.strftime("%m/%d/%y"), 4),                            # two-digit yr
        (str((d - date(1899, 12, 30)).days), 4),                # Excel serial
        (str((d - date(1970, 1, 1)).days * 86400), 3),           # epoch secs
    ])


def _gen_dob() -> str:
    # Realistic DOB between 1940-01-01 and 2006-12-31
    start = date(1940, 1, 1).toordinal()
    end = date(2006, 12, 31).toordinal()
    d = date.fromordinal(random.randint(start, end))
    return _maybe_sentinel([
        (_fmt_dob(d), CLEAN_WEIGHT),
        ("invalid_date", 4),
        ("1985-02-30", 3),                                      # impossible
        ("0000-00-00", 3),                                      # MySQL artifact
        ("1900-01-01", 3),                                      # epoch sentinel
        ("9999-12-31", 2),                                      # max placeholder
        ("1111-11-11", 2),
        ("1820-05-01", 3),                                      # age > 150
        ("2030-01-01", 3),                                      # future
        ("2010-06-01", 3),                                      # under 18
        ("2000-02-29", 2),                                      # valid leap day
        ("03/04/1985", 2),                                      # ambiguous D/M
    ], sentinel_rate=5)


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
    clean = f"{num} {street}, {city}"
    return _maybe_sentinel([
        (clean, CLEAN_WEIGHT),
        (f"{num} {street}, Apt {random.randint(1, 50)}, {city}", 6),
        (f"{num} {street}", 5),                                 # too short
        (f"{num} {street}, " + "Suite " * 40 + city, 4),        # too long
        (f"{num} {street}", 3),                                 # 10-char boundary
        (f"{num} Main S" + "x" * 190, 2),                       # 200-char approx
        (str(random.randint(10000, 99999)), 4),                 # only numbers
        ("123 MAIN STREET, " + city.upper(), 5),                # ALL CAPS
        ("same as above", 3),                                   # junk
    ])


# --------------------------------------------------------------------------- #
# Section 9 — income
# --------------------------------------------------------------------------- #


def _gen_income() -> str:
    income = round(random.uniform(15_000, 250_000), 2)
    return _maybe_sentinel([
        (f"{income}", CLEAN_WEIGHT),
        (f"{-random.randint(1000, 20000)}", 4),                 # negative
        (f"{random.randint(11_000_000, 99_000_000)}", 3),       # over $10M
        ("10000000", 2),                                        # boundary
        ("0", 3),                                               # ambiguous zero
        (f"${income:,.0f}", 5),                                 # currency symbol
        (f"{income:.0f} USD", 3),                               # suffix
        (f"{income:,.2f}", 5),                                  # comma thousands
        (f"{income:.2f}".replace(".", ",").replace(",", ".", 1)
         .replace(",", "."), 3),                                # european-ish
        (f"({random.randint(1000, 20000)})", 3),                # accounting neg
        (f"{random.randint(20, 99)}k", 3),                      # 52k
        (f"{income:.0f}-{income + 10000:.0f}", 2),              # range
        ("fifty thousand", 2),                                  # spelled out
        ("-1", 2),                                              # numeric sentinel
        ("999999999", 2),
        (f"{income}.5555", 2),                                  # excess precision
    ])


# --------------------------------------------------------------------------- #
# Section 10 — account_status
# --------------------------------------------------------------------------- #

_VALID_STATUSES = ["active", "inactive", "suspended"]


def _gen_status() -> str:
    s = random.choice(_VALID_STATUSES)
    return _maybe_sentinel([
        (s, CLEAN_WEIGHT),
        (s.capitalize(), 6),                                    # Active
        (s.upper(), 4),                                         # ACTIVE
        (f" {s} ", 5),                                          # spaces
        ("in-active", 3), ("in active", 2),                     # variants
        ("actve", 3), ("actiive", 2),                           # typos
        ("closed", 4), ("pending", 3), ("banned", 2),           # invalid values
        ("dormant", 3),                                         # legacy value
        (random.choice("AIS"), 3),                              # letter codes
        (str(random.randint(0, 2)), 3),                         # numeric codes
        (random.choice(["TRUE", "FALSE"]), 2),                  # boolean
    ])


# --------------------------------------------------------------------------- #
# Section 11 — created_date
# --------------------------------------------------------------------------- #


def _gen_created_date(dob: date | None) -> str:
    start = date(2015, 1, 1).toordinal()
    end = date(2024, 12, 31).toordinal()
    d = date.fromordinal(random.randint(start, end))
    if dob:                                                     # illogical
        pass
    value = _maybe_sentinel([
        (d.isoformat(), CLEAN_WEIGHT),
        (d.strftime("%m/%d/%Y"), 8),
        (f"{d.isoformat()} {random.randint(0, 23):02d}:"
         f"{random.randint(0, 59):02d}:10", 8),                 # datetime
        (f"{d.isoformat()}T14:32:10+02:00", 3),                 # tz offset
        (d.strftime("%m/%d/%y"), 4),                            # 2-digit year
        (str(d.toordinal() - date(1899, 12, 30).toordinal()), 3),  # Excel
        ("N/A", 3), ("2023-13-45", 3), ("2023-02-30", 2),       # garbage
        ("2027-01-01", 3),                                      # future
        ("1990-01-01", 2),                                      # pre-founding
    ], sentinel_rate=4)
    return value


# --------------------------------------------------------------------------- #
# Row assembly
# --------------------------------------------------------------------------- #


def _generate_row() -> dict[str, str]:
    first = _gen_first_name()
    last = _gen_last_name()

    # First/last swapped (Section 4 — undetectable without reference data)
    if random.random() < 0.005:
        _log("name_swapped")
        first, last = last, first

    dob_raw = None
    dob = _gen_dob()
    row = {
        "customer_id": _gen_customer_id(),
        "first_name": first,
        "last_name": last,
        "email": _gen_email(first, last),
        "phone": _gen_phone(),
        "date_of_birth": dob,
        "address": _gen_address(),
        "income": _gen_income(),
        "account_status": _gen_status(),
        "created_date": _gen_created_date(dob_raw),
    }
    return row


# --------------------------------------------------------------------------- #
# Section 12 — Cross-field defect injection
# --------------------------------------------------------------------------- #


def _inject_cross_field_defects(rows: list[dict[str, str]]) -> None:
    """Mutates `rows` in place. Deliberate, targeted injections."""

    # (a) Bulk-import block: 200 rows sharing one identical timestamp
    block_ts = "2021-03-15 09:00:00"
    for r in random.sample(rows, 200):
        r["created_date"] = block_ts
    _log("bulk_import_block:200")

    # (b) Shared address x20 (fraud signal)
    shared_addr = "666 Fraud Lane, Springfield"
    for r in random.sample(rows, 20):
        r["address"] = shared_addr
    _log("shared_address_x20")

    # (c) Duplicate emails across different IDs
    for a, b in zip(random.sample(rows, 10), random.sample(rows, 10)):
        if a is not b:
            b["email"] = a["email"]
            _log("duplicate_email")

    # (d) Duplicate phones across different IDs
    for a, b in zip(random.sample(rows, 8), random.sample(rows, 8)):
        if a is not b:
            b["phone"] = a["phone"]
            _log("duplicate_phone")

    # (e) created_date before date_of_birth
    victims = random.sample(rows, 5)
    for r in victims:
        r["created_date"], r["date_of_birth"] = \
            r["date_of_birth"], r["created_date"]
        _log("created_before_dob")

    # (f) Age < 18 at created_date
    for r in random.sample(rows, 5):
        r["date_of_birth"] = "2015-06-01"
        r["created_date"] = "2023-01-01"
        _log("underage_kyc")

    # (g) income=0 with active status (contradiction)
    for r in random.sample(rows, 5):
        r["income"] = "0"
        r["account_status"] = "active"
        _log("zero_income_active")


# --------------------------------------------------------------------------- #
# Section 1 — File & encoding level corruption (post-processing)
# --------------------------------------------------------------------------- #


def _write_ragged_rows(buf: str) -> str:
    """Append raw rows with 11 and 9 fields (extra/missing field)."""
    extra = "9001,Extra,Field,row,with,eleven,fields,like,this,one,here,ok"
    missing = "9002,Missing,Some,Fields,row"
    return buf + extra + "\n" + missing + "\n"


def _post_process_csv_text(buf: str) -> str:
    """Applies Section 1 file-level corruptions to the CSV text."""

    # BOM on header
    buf = "\ufeff" + buf
    _log("bom_header")

    # Embedded newline in one address field + unquoted embedded comma
    lines = buf.split("\n")
    if len(lines) > 100:
        lines[100] = lines[100].replace(", Springfield",
                                        "\nApt 4, Springfield", 1)
        _log("embedded_newline")
        _log("unquoted_comma")
    buf = "\n".join(lines)

    # Unescaped quote in one name (Section 1)
    lines = buf.split("\n")
    if len(lines) > 50:
        lines[50] = lines[50].replace("Smith", 'Smith "Johnny"', 1)
        _log("unescaped_quote")
    buf = "\n".join(lines)

    # Mojibake: corrupt one 'José' into double-encoded form
    if "José" in buf:
        buf = buf.replace("José", "JosÃ©", 1)
        _log("mojibake")

    # Mixed line endings: convert ~5% of \n to \r\n
    out = []
    for line in buf.split("\n"):
        if random.random() < 0.05:
            out.append(line + "\r")
        else:
            out.append(line)
    buf = "\n".join(out)
    _log("mixed_line_endings")

    return buf


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def generate(num_rows: int = 3000, seed: int = SEED,
             output: Path = RAW_PATH) -> Path:
    random.seed(seed)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Generating {num_rows} customer rows...")
    rows = [_generate_row() for _ in range(num_rows)]

    print("Injecting cross-field defects (Section 12)...")
    _inject_cross_field_defects(rows)

    # Write CSV with standard quoting first
    sio = io.StringIO()
    writer = csv.DictWriter(sio, fieldnames=SCHEMA, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    buf = sio.getvalue()

    # Append Section 1 ragged rows + text corruption
    buf = _write_ragged_rows(buf)
    buf = _post_process_csv_text(buf)

    output.write_text(buf, encoding="utf-8")

    print(f"\nSaved -> {output}")
    print(f"Defect injection summary ({sum(defect_log.values())} total):")
    for defect, count in sorted(defect_log.items(), key=lambda x: -x[1]):
        print(f"  {defect:<28} {count}")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate messy customers_raw.csv")
    parser.add_argument("num_rows", nargs="?", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, default=RAW_PATH)
    args = parser.parse_args()
    generate(args.num_rows, args.seed, args.output)
