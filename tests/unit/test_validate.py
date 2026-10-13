from __future__ import annotations

from pathlib import Path

import pytest

from app.faers.parse import parse_file, parse_quarter
from app.faers.validate import FileValidationError, validate_file


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_required_column_raises(tmp_path):
    f = _write(tmp_path / "DEMO24q1.txt", "PRIMARYID\tFOO\n123\tbar\n")
    parsed = parse_file(f, "demo", "2024q1")
    with pytest.raises(FileValidationError) as exc:
        validate_file(parsed)
    assert "caseid" in str(exc.value).lower()


def test_row_level_rejections_counted(fixture_q1):
    parsed = parse_quarter(fixture_q1, "2024q1")
    demo_df, res = validate_file(parsed["demo"])
    assert res.raw_rows == 7
    assert res.accepted_rows == 5
    assert res.rejected_rows == 2
    assert res.rejection_reasons == {"missing_primaryid": 1, "missing_caseid": 1}
    # Rejected rows are excluded, accepted rows intact
    pids = demo_df[demo_df["PRIMARYID"].str.strip() != ""]["PRIMARYID"].tolist()
    assert "100001" in pids and "100050" in pids


def test_blank_pt_rejected_and_counted(fixture_q1):
    parsed = parse_quarter(fixture_q1, "2024q1")
    df, res = validate_file(parsed["reac"])
    assert res.rejected_rows == 1
    assert res.rejection_reasons == {"missing_pt": 1}
    assert res.accepted_rows == 8


def test_blank_drugname_counted_not_dropped(fixture_q1):
    parsed = parse_quarter(fixture_q1, "2024q1")
    df, res = validate_file(parsed["drug"])
    assert res.accepted_rows == 8
    assert res.missingness.get("blank_drugname") == 1


def test_missingness_reported(fixture_q1):
    parsed = parse_quarter(fixture_q1, "2024q1")
    _, res = validate_file(parsed["demo"])
    assert res.missingness["missing_age"] == 1
    assert res.missingness["missing_sex"] == 1
