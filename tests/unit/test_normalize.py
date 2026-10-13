from __future__ import annotations

import pytest

from app.faers import normalize as nz


# ---- drugs -----------------------------------------------------------------

def test_drug_name_basic():
    # Whitespace collapsed + uppercased; alias table then applies.
    assert nz.normalize_drug_name("  lisinopril   ") == "LISINOPRIL"
    assert nz.normalize_drug_name("  metformin   hydrochloride ") == "METFORMIN"


def test_drug_alias_table():
    assert nz.normalize_drug_name("METFORMIN HYDROCHLORIDE") == "METFORMIN"
    assert nz.normalize_drug_name("ATORVASTATIN CALCIUM") == "ATORVASTATIN"
    # Unknown names are never guessed (Rule 1)
    assert nz.normalize_drug_name("SOMEOBSCUREDRUG XL") == "SOMEOBSCUREDRUG XL"


def test_drug_blank_returns_none():
    assert nz.normalize_drug_name("") is None
    assert nz.normalize_drug_name("   ") is None
    assert nz.normalize_drug_name(None) is None


def test_drug_deterministic():
    a = nz.normalize_drug_name("Sertraline  Hydrochloride")
    b = nz.normalize_drug_name("Sertraline  Hydrochloride")
    assert a == b == "SERTRALINE"


# ---- age -------------------------------------------------------------------

@pytest.mark.parametrize("age,cod,expected", [
    ("45", "YR", 45.0),
    ("45", "DEC", 45.0),
    ("7", "MO", pytest.approx(7 / 12, abs=1e-4)),
    ("14", "WK", pytest.approx(14 / 52, abs=1e-4)),
    ("3", "DY", pytest.approx(3 / 365.25, abs=1e-4)),
    ("30", None, 30.0),          # missing unit defaults to years
    ("", "YR", None),            # missing
    ("abc", "YR", None),         # unparseable
    ("200", "YR", None),         # out of range
    ("-5", "YR", None),
])
def test_age_normalization(age, cod, expected):
    result = nz.normalize_age(age, cod)
    if expected is None:
        assert result is None
    else:
        assert result == expected


# ---- sex -------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("M", "M"), ("F", "F"), ("male", "M"), ("Female", "F"),
    ("U", None), ("", None), (None, None), ("1", None),
])
def test_sex_normalization(raw, expected):
    assert nz.normalize_sex(raw) == expected


# ---- events / countries / dates -------------------------------------------

def test_event_term():
    assert nz.normalize_event_term("  nausea   ") == "NAUSEA"
    assert nz.normalize_event_term("") is None
    assert nz.normalize_event_term(None) is None


def test_country():
    assert nz.normalize_country(" us ") == "US"
    assert nz.normalize_country("") is None


@pytest.mark.parametrize("raw,expected", [
    ("20240115", "2024-01-15"),
    ("202401", "2024-01"),
    ("2024", "2024"),
    ("00000000", None),
    ("", None),
    (None, None),
    ("2024-01-15", "2024-01-15"),
])
def test_date_normalization(raw, expected):
    assert nz.normalize_date(raw) == expected
