from __future__ import annotations

from app.faers.dedupe import VersionRecord, select_current_versions


def test_single_version_wins_by_default():
    out = select_current_versions([VersionRecord("C1", "100", "20240101")])
    assert out == {"C1": "100"}


def test_latest_fda_dt_wins():
    out = select_current_versions([
        VersionRecord("C1", "100", "20240101"),
        VersionRecord("C1", "200", "20240601"),
    ])
    assert out == {"C1": "200"}


def test_tie_on_date_breaks_on_primaryid():
    out = select_current_versions([
        VersionRecord("C1", "100", "20240101"),
        VersionRecord("C1", "200", "20240101"),
    ])
    assert out == {"C1": "200"}


def test_explicit_version_beats_date():
    # An explicit FDA_VERSION=2 report older by date still wins over version=1
    out = select_current_versions([
        VersionRecord("C1", "100", "20240601", fda_version="1"),
        VersionRecord("C1", "200", "20240101", fda_version="2"),
    ])
    assert out == {"C1": "200"}


def test_order_independent():
    recs = [
        VersionRecord("C1", "100", "20240101"),
        VersionRecord("C1", "300", "20240301"),
        VersionRecord("C1", "200", "20240201"),
        VersionRecord("C2", "400", "20240115"),
    ]
    a = select_current_versions(recs)
    b = select_current_versions(list(reversed(recs)))
    assert a == b == {"C1": "300", "C2": "400"}


def test_cross_quarter_versions():
    # Same case_id updated in a later quarter -> new primaryid wins
    recs = [
        VersionRecord("90002", "100002", "20240201"),
        VersionRecord("90002", "200001", "20240505"),
    ]
    assert select_current_versions(recs) == {"90002": "200001"}


def test_missing_dates_do_not_crash():
    recs = [
        VersionRecord("C1", "100", None),
        VersionRecord("C1", "200", None),
    ]
    assert select_current_versions(recs) == {"C1": "200"}


def test_blank_records_ignored():
    out = select_current_versions([
        VersionRecord("", "100", "20240101"),
        VersionRecord("C1", "", "20240101"),
        VersionRecord("C1", "300", "20240101"),
    ])
    assert out == {"C1": "300"}


def test_deterministic_across_repeats():
    recs = [VersionRecord(f"C{i}", str(100 + i), "20240101") for i in range(50)]
    runs = [select_current_versions(recs) for _ in range(5)]
    assert all(r == runs[0] for r in runs)
