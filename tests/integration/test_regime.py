"""Quarterly regime detector tests: hand-built synthetic reporting regimes.

Data design (all synthetic rows carry a recognizable prefix/drug name so
cleanup is surgical and other tests' fixture data is untouched):

    BULKDRUGX / BURSTEVT   2024q1-q4 co-occurrence a = 2, 3, 3, 12 with all
                           12 of the 2024q4 cases single-event -> bulk flag.
                           Hand-computed: baseline median 3, volume ratio
                           12/3 = 4.0; single share 1.00 vs baseline mean
                           (0.5 + 0.3333 + 0.3333)/3 = 0.389.

    RAMPDRUGX / RAMPEVT    drug holds 20 cases per quarter (2024q1-q4),
                           pair share 1/20 -> 2/20 -> 4/20 -> 12/20 with
                           every pair case listing two reactions (so the
                           bulk detector cannot fire) -> notoriety ramp.
                           Hand-computed: baseline share mean
                           (0.05 + 0.1 + 0.2)/3 = 0.117, ratio 0.6/0.1167
                           = 5.15, 3 increases over 3 transitions.

    STEADYDRUGX / STEADYEVT  a = 5 every quarter, 10 drug cases/quarter,
                           constant mix -> evaluated, never flagged.

signal_scores rows carry prr_screen = TRUE so the pairs are candidates;
signal_quarterly carries the a series (all-reports mode).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.statistics.regime import DEFAULT_THRESHOLDS, evaluate_pair  # noqa: E402

DRUGS = ("BULKDRUGX", "RAMPDRUGX", "STEADYDRUGX")
FILLER = "FILLEREVTFILLER"


def _cleanup(url: str) -> None:
    import psycopg2

    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM cases WHERE primaryid LIKE 'rgm-%'")
        cur.execute("DELETE FROM signal_quarterly WHERE drug IN %s",
                    (DRUGS,))
        cur.execute("DELETE FROM signal_scores WHERE drug IN %s", (DRUGS,))
    conn.close()


@pytest.fixture()
def regime_db(test_db_url):
    _cleanup(test_db_url)      # leftovers from a killed run must not leak
    yield test_db_url
    _cleanup(test_db_url)


def _mk_case(cur, pid: str, quarter: str, drugs: tuple, events: tuple):
    cur.execute(
        "INSERT INTO cases (primaryid, case_id, source_quarter, is_current) "
        "VALUES (%s, %s, %s, TRUE)", (pid, pid, quarter))
    for d in drugs:
        cur.execute(
            "INSERT INTO case_drugs (primaryid, case_id, drug_normalized, "
            "role) VALUES (%s, %s, %s, 'SUS')", (pid, pid, d))
    for e in events:
        cur.execute(
            "INSERT INTO case_events (primaryid, case_id, event_term) "
            "VALUES (%s, %s, %s)", (pid, pid, e))


def _load_synthetic(url: str) -> None:
    import psycopg2

    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        # ---- bulk pair: a = 2, 3, 3, 12; q4 all single-event -----------
        bulk_q = {"2024q1": (2, 1), "2024q2": (3, 1), "2024q3": (3, 1),
                  "2024q4": (12, 12)}   # quarter -> (cases, single events)
        for q, (n_cases, n_single) in bulk_q.items():
            for i in range(n_cases):
                pid = f"rgm-bulk-{q}-{i}"
                events = ("BURSTEVT",) if i < n_single \
                    else ("BURSTEVT", FILLER)
                _mk_case(cur, pid, q, ("BULKDRUGX",), events)
            cur.execute(
                "INSERT INTO signal_quarterly (drug, event, quarter, a, "
                "background_n, primary_suspect_mode) VALUES "
                "(%s, 'BURSTEVT', %s, %s, 100, FALSE)",
                ("BULKDRUGX", q, n_cases))

        # ---- ramp pair: 20 drug cases/qtr, shares .05/.1/.2/.6 ----------
        ramp_a = {"2024q1": 1, "2024q2": 2, "2024q3": 4, "2024q4": 12}
        for q, a in ramp_a.items():
            for i in range(a):        # pair cases: two reactions each
                _mk_case(cur, f"rgm-ramp-{q}-{i}", q, ("RAMPDRUGX",),
                         ("RAMPEVT", FILLER))
            for i in range(20 - a):   # the drug's other cases that quarter
                _mk_case(cur, f"rgm-rampo-{q}-{i}", q, ("RAMPDRUGX",),
                         (FILLER,))
            cur.execute(
                "INSERT INTO signal_quarterly (drug, event, quarter, a, "
                "background_n, primary_suspect_mode) VALUES "
                "(%s, 'RAMPEVT', %s, %s, 100, FALSE)",
                ("RAMPDRUGX", q, a))

        # ---- steady pair: a = 5 every quarter, 10 drug cases/qtr --------
        for q in ("2024q1", "2024q2", "2024q3", "2024q4"):
            for i in range(5):
                events = ("STEADYEVT",) if i < 2 else ("STEADYEVT", FILLER)
                _mk_case(cur, f"rgm-steady-{q}-{i}", q, ("STEADYDRUGX",),
                         events)
            for i in range(5):
                _mk_case(cur, f"rgm-steadyo-{q}-{i}", q, ("STEADYDRUGX",),
                         (FILLER,))
            cur.execute(
                "INSERT INTO signal_quarterly (drug, event, quarter, a, "
                "background_n, primary_suspect_mode) VALUES "
                "(%s, 'STEADYEVT', %s, 5, 100, FALSE)",
                ("STEADYDRUGX", q))

        # ---- screened detections so the pairs become candidates ---------
        for drug, event in (("BULKDRUGX", "BURSTEVT"),
                            ("RAMPDRUGX", "RAMPEVT"),
                            ("STEADYDRUGX", "STEADYEVT")):
            cur.execute(
                "INSERT INTO signal_scores (drug, event, period, a, b, c, d, "
                "case_count, prr_screen, primary_suspect_mode) VALUES "
                "(%s, %s, 'all', 10, 10, 10, 10, 10, TRUE, FALSE)",
                (drug, event))
    conn.close()


# ---------------------------------------------------------------------------
# pure detection logic (no database)
# ---------------------------------------------------------------------------

def test_evaluate_pair_bulk_flag_hand_computed():
    series = [
        {"quarter": "2024q1", "a": 2, "single_event_cases": 1,
         "drug_cases": 2},
        {"quarter": "2024q2", "a": 3, "single_event_cases": 1,
         "drug_cases": 3},
        {"quarter": "2024q3", "a": 3, "single_event_cases": 1,
         "drug_cases": 3},
        {"quarter": "2024q4", "a": 12, "single_event_cases": 12,
         "drug_cases": 12},
    ]
    flags = evaluate_pair(series)
    assert [f["flag"] for f in flags] == ["bulk_single_event"]
    m = flags[0]["metrics"]
    assert m["cases"] == 12
    assert m["baseline_median_cases"] == 3.0
    assert m["volume_ratio"] == 4.0
    assert m["single_event_share"] == 1.0
    assert m["baseline_single_event_share"] == 0.389   # 0.38888.. rounded


def test_evaluate_pair_ramp_flag_hand_computed():
    shares = [1, 2, 4, 12]
    series = [{"quarter": f"2024q{i + 1}", "a": a, "single_event_cases": 0,
               "drug_cases": 20} for i, a in enumerate(shares)]
    flags = evaluate_pair(series)
    assert [f["flag"] for f in flags] == ["notoriety_ramp"]
    m = flags[0]["metrics"]
    assert m["within_drug_share"] == 0.6            # 12/20
    assert m["baseline_share_mean"] == 0.117        # (0.05+0.1+0.2)/3
    assert m["share_ratio"] == 5.14                 # 0.6 / 0.11666..
    assert m["quarters_with_share"] == 4


def test_evaluate_pair_steady_series_is_silent():
    series = [{"quarter": f"2024q{i + 1}", "a": 5, "single_event_cases": 2,
               "drug_cases": 10} for i in range(4)]
    assert evaluate_pair(series) == []


def test_evaluate_pair_needs_baseline_history():
    series = [{"quarter": "2024q3", "a": 3, "single_event_cases": 3,
               "drug_cases": 3},
              {"quarter": "2024q4", "a": 90, "single_event_cases": 90,
               "drug_cases": 90}]
    assert evaluate_pair(series) == []      # one prior quarter is not a regime


def test_evaluate_pair_bulk_suppresses_ramp():
    """A one-quarter single-event jump must not also claim gradual ramp."""
    series = [
        {"quarter": "2024q1", "a": 2, "single_event_cases": 1,
         "drug_cases": 100},
        {"quarter": "2024q2", "a": 3, "single_event_cases": 1,
         "drug_cases": 100},
        {"quarter": "2024q3", "a": 60, "single_event_cases": 58,
         "drug_cases": 100},
    ]
    flags = evaluate_pair(series)
    assert [f["flag"] for f in flags] == ["bulk_single_event"]


def test_evaluate_pair_spike_without_single_events_is_not_bulk():
    """Volume spike with multi-event reports: not a bulk artifact, and a
    one-step jump off a flat share baseline is not a gradual ramp either
    (the +0.005 first step is below ramp_min_step)."""
    series = [
        {"quarter": "2024q1", "a": 2, "single_event_cases": 0,
         "drug_cases": 200},
        {"quarter": "2024q2", "a": 3, "single_event_cases": 0,
         "drug_cases": 200},
        {"quarter": "2024q3", "a": 60, "single_event_cases": 0,
         "drug_cases": 200},
    ]
    assert evaluate_pair(series) == []


def test_thresholds_are_overridable():
    series = [{"quarter": "2024q1", "a": 2, "single_event_cases": 1,
               "drug_cases": 2},
              {"quarter": "2024q2", "a": 3, "single_event_cases": 1,
               "drug_cases": 3},
              {"quarter": "2024q3", "a": 3, "single_event_cases": 1,
               "drug_cases": 3},
              {"quarter": "2024q4", "a": 12, "single_event_cases": 12,
               "drug_cases": 12}]
    strict = {**DEFAULT_THRESHOLDS, "single_event_share_min": 1.01}
    assert evaluate_pair(series, strict) == []   # impossible bar -> no flag


# ---------------------------------------------------------------------------
# end-to-end detect_regimes against the test database
# ---------------------------------------------------------------------------

def test_detect_regimes_flags_synthetic_artifacts(regime_db):
    import psycopg2

    from app.statistics.regime import detect_regimes

    _load_synthetic(regime_db)
    conn = psycopg2.connect(regime_db)
    try:
        res = detect_regimes(conn, min_cases=3)
    finally:
        conn.close()

    assert res["status"] == "ok"
    assert res["evaluated_quarter"] == "2024q4"
    assert res["candidates_evaluated"] == 3        # bulk + ramp + steady
    assert res["flag_counts"] == {"bulk_single_event": 1,
                                  "notoriety_ramp": 1}
    by_key = {(f["flag"], f["drug"]): f for f in res["flags"]}
    assert set(by_key) == {("bulk_single_event", "BULKDRUGX"),
                           ("notoriety_ramp", "RAMPDRUGX")}

    bulk = by_key[("bulk_single_event", "BULKDRUGX")]
    assert bulk["event"] == "BURSTEVT" and bulk["quarter"] == "2024q4"
    assert bulk["metrics"]["cases"] == 12
    assert bulk["metrics"]["volume_ratio"] == 4.0
    assert bulk["metrics"]["single_event_share"] == 1.0
    assert "bulk submission burst" in bulk["reason"]

    ramp = by_key[("notoriety_ramp", "RAMPDRUGX")]
    assert ramp["event"] == "RAMPEVT" and ramp["quarter"] == "2024q4"
    assert ramp["metrics"]["within_drug_share"] == 0.6
    assert ramp["metrics"]["share_ratio"] == 5.14
    assert ramp["metrics"]["drug_cases"] == 20
    assert "reporting-regime shift" in ramp["reason"]

    # the steady pair was audited and stayed silent
    assert all(f["drug"] != "STEADYDRUGX" for f in res["flags"])
    # parameters recorded for auditability
    assert res["parameters"]["min_cases"] == 3
    assert res["parameters"]["max_candidates"] == 1000


def test_detect_regimes_empty_candidates_is_valid(regime_db):
    import psycopg2

    from app.statistics.regime import detect_regimes

    conn = psycopg2.connect(regime_db)
    try:
        # no pair clears this floor -> zero candidates is a valid result
        res = detect_regimes(conn, min_cases=10 ** 9)
    finally:
        conn.close()
    assert res["status"] == "ok"
    assert res["evaluated_quarter"] is None
    assert res["candidates_evaluated"] == 0
    assert res["flags"] == []
    assert res["flag_counts"] == {"bulk_single_event": 0,
                                  "notoriety_ramp": 0}
