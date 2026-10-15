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


def test_focus_drug_brand_aliases():
    """Explicit brand-to-ingredient mappings for the 5 focus drugs."""
    assert nz.normalize_drug_name("LIPITOR") == "ATORVASTATIN"
    assert nz.normalize_drug_name("GLUCOPHAGE") == "METFORMIN"
    assert nz.normalize_drug_name("GLUCOPHAGE XR") == "METFORMIN"
    assert nz.normalize_drug_name("ADVIL") == "IBUPROFEN"
    assert nz.normalize_drug_name("MOTRIN") == "IBUPROFEN"
    assert nz.normalize_drug_name("PRILOSEC") == "OMEPRAZOLE"
    assert nz.normalize_drug_name("ZOLOFT") == "SERTRALINE"


def test_focus_drug_case_and_whitespace():
    """Case-insensitive and whitespace-collapsed brand name normalization."""
    assert nz.normalize_drug_name("  lipitor  ") == "ATORVASTATIN"
    assert nz.normalize_drug_name("Glucophage") == "METFORMIN"
    assert nz.normalize_drug_name("  glucophage   xr  ") == "METFORMIN"
    assert nz.normalize_drug_name("advil") == "IBUPROFEN"
    assert nz.normalize_drug_name("  Motrin  ") == "IBUPROFEN"
    assert nz.normalize_drug_name("prilosec") == "OMEPRAZOLE"
    assert nz.normalize_drug_name("  Zoloft ") == "SERTRALINE"


def test_combination_products_remain_distinct():
    """Multi-active-ingredient combination products must NEVER be merged
    blindly into single-ingredient canonical names."""
    assert nz.normalize_drug_name("ADVIL PM") == "ADVIL PM"
    assert nz.normalize_drug_name("ADVIL DUAL ACTION") == "ADVIL DUAL ACTION"
    assert nz.normalize_drug_name("MOTRIN PM") == "MOTRIN PM"
    assert nz.normalize_drug_name("CADUET") == "CADUET"
    assert nz.normalize_drug_name("LIPTRUZET") == "LIPTRUZET"
    assert nz.normalize_drug_name("JANUMET") == "JANUMET"
    assert nz.normalize_drug_name("GLUCOVANCE") == "GLUCOVANCE"
    assert nz.normalize_drug_name("SYNJARDY") == "SYNJARDY"
    assert nz.normalize_drug_name("ZEGERID") == "ZEGERID"
    assert nz.normalize_drug_name("VIMOVO") == "VIMOVO"


def test_distinct_active_ingredients_remain_distinct():
    """Chemically distinct agents in the same or related classes must
    remain separate and never collide."""
    assert nz.normalize_drug_name("CRESTOR") == "CRESTOR"         # Rosuvastatin != Atorvastatin
    assert nz.normalize_drug_name("NEXIUM") == "NEXIUM"           # Esomeprazole != Omeprazole
    assert nz.normalize_drug_name("PROZAC") == "PROZAC"           # Fluoxetine != Sertraline
    assert nz.normalize_drug_name("ALEVE") == "ALEVE"             # Naproxen != Ibuprofen
    assert nz.normalize_drug_name("JANUVIA") == "JANUVIA"         # Sitagliptin != Metformin



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
