from __future__ import annotations

import pytest

from app.faers.parse import discover_quarter_files, normalize_quarter, parse_quarter


def test_normalize_quarter_valid():
    assert normalize_quarter("2024q1") == "2024q1"
    assert normalize_quarter("2024Q3") == "2024q3"
    assert normalize_quarter(" 2025q2 ") == "2025q2"


def test_normalize_quarter_invalid():
    with pytest.raises(ValueError):
        normalize_quarter("2024")
    with pytest.raises(ValueError):
        normalize_quarter("2024q5")


def test_discover_accepts_yyqN_filenames(fixture_q1):
    files = discover_quarter_files(fixture_q1, "2024q1")
    assert set(files) == {"demo", "drug", "reac"}
    assert files["demo"].name == "DEMO24q1.txt"


def test_parse_quarter_fixture(fixture_q1):
    parsed = parse_quarter(fixture_q1, "2024q1")
    assert parsed["demo"].raw_rows == 7
    assert parsed["drug"].raw_rows == 9
    assert parsed["reac"].raw_rows == 9
    assert parsed["demo"].field_map["primaryid"] == "PRIMARYID"
    assert parsed["demo"].field_map["fda_dt"] == "FDA_DT"
    assert parsed["drug"].field_map["drugname"] == "DRUGNAME"
    assert parsed["reac"].field_map["pt"] == "PT"
    # Raw strings preserved exactly (no silent NA coercion)
    first_pid = parsed["demo"].frame[parsed["demo"].field_map["primaryid"]].iloc[0]
    assert first_pid == "100001"


def test_parse_quarter_missing_dataset(fixture_q1, tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_quarter(tmp_path, "2024q1")  # empty dir
