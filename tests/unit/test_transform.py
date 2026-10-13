from __future__ import annotations

from app.faers.parse import parse_quarter
from app.faers.transform import UNKNOWN_DRUG, transform_quarter


def test_transform_counts_all_stages(fixture_q1):
    parsed = parse_quarter(fixture_q1, "2024q1")
    rows, stats = transform_quarter(parsed)

    # cases: 7 raw - 2 rejected = 5
    assert len(rows["cases"]) == 5
    assert stats.demo.rejected_rows == 2

    # age / sex stats
    assert stats.ages_missing == 1       # 100002
    assert stats.ages_unparseable == 1   # 100004 (age 200)
    assert stats.sexes_unknown == 2      # 100002 blank + 100004 'U'

    # drug stats: 9 raw - 1 rejected(blank primaryid) - 1 orphan - 1 duplicate = 6
    assert len(rows["case_drugs"]) == 6
    assert stats.drug_rows_orphan == 1
    assert stats.duplicate_drug_rows == 1
    assert stats.drugs_unknown_name == 1
    assert stats.drugs_normalized == 5

    # event stats: 9 raw - 1 blank PT - 1 orphan - 1 duplicate = 6
    assert len(rows["case_events"]) == 6
    assert stats.event_rows_orphan == 1
    assert stats.duplicate_event_rows == 1
    assert stats.events_normalized == 6


def test_transform_normalizes_values(fixture_q1):
    parsed = parse_quarter(fixture_q1, "2024q1")
    rows, _ = transform_quarter(parsed)

    by_pid = {c[0]: c for c in rows["cases"]}
    assert by_pid["100001"][4] == 45.0            # age years
    assert by_pid["100002"][4] is None             # missing age
    assert abs(by_pid["100003"][4] - 7 / 12) < 1e-4  # 7 months
    assert by_pid["100004"][4] is None             # invalid age rejected
    assert by_pid["100001"][5] == "F"
    assert by_pid["100004"][5] is None             # unknown sex
    assert by_pid["100001"][2] == "2024-01-15"     # event date normalized
    assert by_pid["100001"][3] == "2024-02-01"     # report date normalized

    drugs = {d[2]: d for d in rows["case_drugs"]}  # keyed by drug_seq
    assert drugs["1"][3] == "METFORMIN"            # alias applied
    assert drugs["2"][3] == "LISINOPRIL"
    assert drugs["1"][5] == "SUS"
    assert drugs["1"][6] == "ORAL"
    unknown = [d for d in rows["case_drugs"] if d[3] == UNKNOWN_DRUG]
    assert len(unknown) == 1
    assert unknown[0][1] == "90004"                # case_id attached

    events = {(e[0], e[2]) for e in rows["case_events"]}
    assert ("100001", "NAUSEA") in events
    assert ("100001", "VOMITING") in events


def test_transform_is_deterministic(fixture_q1):
    a, sa = transform_quarter(parse_quarter(fixture_q1, "2024q1"))
    b, sb = transform_quarter(parse_quarter(fixture_q1, "2024q1"))
    assert a == b
    assert sa.as_dict() == sb.as_dict()
