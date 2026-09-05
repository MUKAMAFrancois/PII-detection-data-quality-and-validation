"""
test_validators.py
==================
Pins the behaviour the brief specifies, so a regression fails loudly.

Covers the rule table (Part 3), the three normalizations (Part 4) and the
five masking rules (Part 5). The masking cases are the brief's own worked
examples, carried in config.MASKING_RULES.

Usage:
    python -m pytest tests/ -q
    python -m tests.test_validators      # no pytest installed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src import config as C  # noqa: E402
from src.cleaner import (  # noqa: E402
    clean_customer_id,
    clean_date,
    clean_email,
    clean_income,
    clean_name,
    clean_phone,
    clean_status,
)
from src.masker import find_leaks, mask_frame, mask_value, verify_rules  # noqa: E402
from src.pii_detector import redact  # noqa: E402
from src.validator import recoverable, validate_frame  # noqa: E402


# --------------------------------------------------------------------------- #
# Part 3 — the brief's rule table
# --------------------------------------------------------------------------- #

def _row(**overrides) -> dict[str, str]:
    """A record that passes every rule, minus whatever the test breaks."""
    base = {
        "customer_id": "1001",
        "first_name": "John",
        "last_name": "Doe",
        "email": "john.doe@gmail.com",
        "phone": "555-123-4567",
        "date_of_birth": "1985-03-15",
        "address": "123 Main Street, Springfield",
        "income": "52000.00",
        "account_status": "active",
        "created_date": "2018-10-25",
    }
    base.update(overrides)
    return base


def _rules_for(**overrides) -> set[str]:
    frame = pd.DataFrame([_row(**overrides)], columns=C.SCHEMA)
    return {f"{f.column}.{f.rule}" for f in validate_frame(frame).failures}


def test_clean_record_passes():
    assert _rules_for() == set()


def test_customer_id_rules():
    assert "customer_id.not_integer" in _rules_for(customer_id="AB-1234")
    assert "customer_id.not_positive" in _rules_for(customer_id="0")
    assert "customer_id.not_positive" in _rules_for(customer_id="-5")
    assert "customer_id.missing_required" in _rules_for(customer_id="N/A")


def test_customer_id_uniqueness_is_dataset_level():
    frame = pd.DataFrame([_row(), _row()], columns=C.SCHEMA)
    rules = {f.rule for f in validate_frame(frame).failures}
    assert "not_unique" in rules


def test_name_rules():
    assert "first_name.not_alphabetic" in _rules_for(first_name="J0hn")
    assert "first_name.length_out_of_range" in _rules_for(first_name="J")
    assert "last_name.length_out_of_range" in _rules_for(last_name="T" * 51)
    assert "first_name.missing_required" in _rules_for(first_name="")


def test_names_the_naive_rule_would_reject():
    # Real customers whose names a plain [A-Za-z]+ rule would fail.
    for name in ("O'Brien", "van der Berg", "Mary-Jane", "Ngũgĩ", "José"):
        assert _rules_for(last_name=name) == set(), name


def test_email_and_phone_rules():
    assert "email.invalid_email_format" in _rules_for(email="john.doe@gmail")
    assert "phone.invalid_phone_format" in _rules_for(phone="(555) 123-4567")


def test_date_rules():
    assert "date_of_birth.not_iso_date" in _rules_for(date_of_birth="03/15/1985")
    assert "date_of_birth.not_iso_date" in _rules_for(date_of_birth="invalid_date")
    assert "date_of_birth.sentinel_date" in _rules_for(date_of_birth="9999-12-31")
    assert "date_of_birth.out_of_range" in _rules_for(date_of_birth="1820-05-01")
    assert "date_of_birth.out_of_range" in _rules_for(date_of_birth="2099-01-01")
    assert "created_date.not_iso_date" in _rules_for(created_date="2021-03-15 09:00:00")


def test_address_and_income_rules():
    assert "address.length_out_of_range" in _rules_for(address="12 St")
    assert "address.length_out_of_range" in _rules_for(address="x" * 201)
    assert "income.negative" in _rules_for(income="-1500")
    assert "income.exceeds_max" in _rules_for(income="50000000")
    assert "income.not_numeric" in _rules_for(income="fifty thousand")


def test_account_status_rules():
    for good in ("active", "inactive", "suspended"):
        assert _rules_for(account_status=good) == set()
    assert "account_status.invalid_category" in _rules_for(account_status="banned")


def test_recoverability_never_overpromises():
    # A cleaner must be able to honour every "recoverable" verdict.
    assert recoverable("customer_id", "not_integer", "1042.0")
    assert not recoverable("customer_id", "not_positive", "0")
    assert not recoverable("customer_id", "not_integer", "1.042E+00")
    assert recoverable("date_of_birth", "not_iso_date", "03/15/1985")
    assert not recoverable("date_of_birth", "not_iso_date", "invalid_date")
    assert recoverable("account_status", "invalid_category", "dormant")
    assert not recoverable("account_status", "invalid_category", "banned")


# --------------------------------------------------------------------------- #
# Part 4 — normalization
# --------------------------------------------------------------------------- #

def test_phone_normalizes_to_the_brief_format():
    for raw in ("(555) 123-4567", "555.123.4567", "5551234567",
                "+1 555-123-4567", "555 123 4567", "555-123-4567 x89"):
        value, _, _ = clean_phone(raw)
        assert value == "555-123-4567", raw


def test_phone_removes_what_cannot_be_ten_digits():
    for raw in ("555-CAL-LME", "711-7287", "53049664849", "000-000-0000"):
        value, action, _ = clean_phone(raw)
        assert value == "" and action == "nulled", raw


def test_phone_keeps_valid_international():
    # Deleting a real contact to satisfy a US format rule would lose data.
    for raw in ("+44 20 7946 0958", "+250 788 123 456"):
        value, action, _ = clean_phone(raw)
        assert value == raw and action == "unfixable", raw


def test_dates_normalize_to_iso():
    cases = {
        "03/15/1985": "1985-03-15",
        "1985/03/15": "1985-03-15",
        "15-03-1985": "1985-03-15",
        "March 15, 1985": "1985-03-15",
        "1985-03-15 00:00:00": "1985-03-15",
        "1985-03-15T00:00:00+02:00": "1985-03-15",
    }
    for raw, want in cases.items():
        value, _, _ = clean_date(raw)
        assert value == want, f"{raw} -> {value}"


def test_unparseable_dates_are_removed():
    for raw in ("invalid_date", "1985-02-30", "0000-00-00", "9999-12-31"):
        value, action, _ = clean_date(raw)
        assert value == "" and action == "nulled", raw


def test_names_title_case():
    assert clean_name("john")[0] == "John"
    assert clean_name("JOHN")[0] == "John"
    assert clean_name("jOhN")[0] == "John"
    assert clean_name("  grace ")[0] == "Grace"


def test_title_case_does_not_corrupt_particles():
    # Plain .title() turns these into "Van Der Berg" / "De La Cruz".
    for name in ("van der Berg", "de la Cruz"):
        value, action, _ = clean_name(name)
        assert value == name and action == "ok", name


def test_income_and_status_normalization():
    assert clean_income("$52,000")[0] == "52000.00"
    assert clean_income("52000 USD")[0] == "52000.00"
    assert clean_income("(15000)")[0] == "-15000.00"
    assert clean_income("50k")[0] == "50000.00"
    assert clean_income("1.234,56")[0] == "1234.56"
    assert clean_status("ACTIVE")[0] == "active"
    assert clean_status("in-active")[0] == "inactive"
    assert clean_status("dormant")[0] == "inactive"
    assert clean_status("banned")[1] == "unfixable"


def test_customer_id_and_email_normalization():
    assert clean_customer_id("0001042")[0] == "1042"
    assert clean_customer_id("1042.0")[0] == "1042"
    assert clean_customer_id("AB-1234")[1] == "unfixable"
    assert clean_email("John.Doe@GMAIL.COM")[0] == "john.doe@gmail.com"


def test_nothing_is_imputed():
    # A gap must stay a gap; the placeholder still reads as absent.
    assert clean_name("")[0] == ""
    assert clean_income("N/A")[0] == ""
    assert not C.is_present(C.MISSING_PLACEHOLDER)


# --------------------------------------------------------------------------- #
# Part 5 — masking
# --------------------------------------------------------------------------- #

def test_masking_matches_the_briefs_examples():
    for column, strategy, before, produced, ok in verify_rules():
        assert ok, f"{column} ({strategy}): {before!r} -> {produced!r}"


def test_masking_covers_every_address_in_a_multi_address_cell():
    # Partitioning on the first '@' once left the second address readable.
    masked = mask_value("email", "leila.test@x.com; leila.test2@y.com")
    assert "leila.test2@y.com" not in masked
    assert not C.EMAIL_SCAN_RE.search(masked)


def test_masking_leaves_gaps_alone():
    for column in ("first_name", "email", "phone", "address", "date_of_birth"):
        assert mask_value(column, "") == ""
        assert mask_value(column, C.MISSING_PLACEHOLDER) == C.MISSING_PLACEHOLDER


def test_masking_does_not_leak_on_malformed_input():
    frame = pd.DataFrame([
        _row(email="not-an-email", phone="+44 20 7946 0958",
             date_of_birth="not-a-date"),
        _row(customer_id="1002", email="a@b.com; c@d.com"),
    ], columns=C.SCHEMA)
    masked, _ = mask_frame(frame)
    assert find_leaks(frame, masked) == []


def test_unmasked_columns_are_declared_not_forgotten():
    unmasked = set(C.PII_FIELDS) - set(C.MASKING_RULES)
    assert unmasked == {"income", "customer_id"}
    assert "income" in C.UNMASKED_PII_COLUMNS


def test_report_redaction_never_returns_the_original():
    for value in ("john.doe@gmail.com", "555-123-4567", "John",
                  "123 Main Street, Springfield"):
        assert redact(value) != value
        assert value.lower() not in redact(value).lower()


# --------------------------------------------------------------------------- #
# Config invariants
# --------------------------------------------------------------------------- #

def test_config_covers_every_column():
    assert set(C.COLUMN_TYPES) == set(C.SCHEMA)
    assert set(C.MISSING_VALUE_STRATEGY) == set(C.SCHEMA)
    assert set(C.PII_FIELDS) | set(C.NON_PII_COLUMNS) == set(C.SCHEMA)


def test_status_tables_do_not_overlap():
    assert set(C.STATUS_CANONICAL_MAP) & set(C.STATUS_UNMAPPABLE) == set()
    assert set(C.STATUS_CANONICAL_MAP.values()) == C.VALID_STATUSES


def test_paths_are_rendered_relative():
    assert C.rel(C.RAW_PATH) == "data/raw/customers_raw.csv"
    assert not Path(C.rel(C.CLEANED_PATH)).is_absolute()


# --------------------------------------------------------------------------- #
# Runner for environments without pytest
# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
