"""Integration tests for quarterly PRR profiles (app/statistics/qprofile.py).

Tests database-driven quarterly_profiles queries across multi-quarter data,
verifying that SQL staging and profile_verdict classification produce the
expected verdicts on synthetic multi-quarter reporting series.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.statistics.qprofile import quarterly_profiles  # noqa: E402

TEST_DRUGS = ("QP_NEWDRUG", "QP_STEADYDRUG")


def _cleanup(url: str) -> None:
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM cases WHERE primaryid LIKE 'qp-%'")
        cur.execute("DELETE FROM signal_quarterly WHERE drug IN %s", (TEST_DRUGS,))
        cur.execute("DELETE FROM signal_scores WHERE drug IN %s", (TEST_DRUGS,))
    conn.close()


@pytest.fixture()
def qprofile_db(test_db_url):
    _cleanup(test_db_url)
    yield test_db_url
    _cleanup(test_db_url)


def _seed_synthetic_profiles(url: str) -> None:
    """Seed synthetic cases, drugs, events, and signal tables across 4 quarters:
    2099q1, 2099q2, 2099q3, 2099q4.

    1. QP_NEWDRUG / QP_EVENT_BURST:
       - Baseline quarters (q1..q3): low co-occurrence (1/20), low qPRR (< 1.0)
       - Latest quarter (q4): massive co-occurrence (15/20), high qPRR (>= 2.0)
       - Pooled PRR: 2.8 -> expected verdict: elevation_is_new (flagged)

    2. QP_STEADYDRUG / QP_EVENT_STEADY:
       - All quarters (q1..q4): steady elevated co-occurrence (10/20), qPRR >= 2.0
       - Pooled PRR: 3.2 -> expected verdict: consistent (unflagged)
    """
    conn = psycopg2.connect(url)
    conn.autocommit = False
    cur = conn.cursor()

    quarters = ["2099q1", "2099q2", "2099q3", "2099q4"]
    cases_rows = []
    drugs_rows = []
    events_rows = []

    case_idx = 0

    def new_case(q: str):
        nonlocal case_idx
        case_idx += 1
        pid = f"qp-{case_idx:05d}"
        cid = f"qpcase-{case_idx:05d}"
        cases_rows.append((pid, cid, q, True))
        return pid, cid

    for q in quarters:
        # 1. QP_NEWDRUG: 20 cases per quarter
        burst_co_count = 15 if q == "2099q4" else 1
        for i in range(20):
            pid, cid = new_case(q)
            drugs_rows.append((pid, cid, "QP_NEWDRUG", "PS"))
            if i < burst_co_count:
                events_rows.append((pid, cid, "QP_EVENT_BURST"))
            else:
                events_rows.append((pid, cid, "QP_FILLER_EVENT"))

        # 2. QP_STEADYDRUG: 20 cases per quarter, 10 co-occurrences every quarter
        for i in range(20):
            pid, cid = new_case(q)
            drugs_rows.append((pid, cid, "QP_STEADYDRUG", "PS"))
            if i < 10:
                events_rows.append((pid, cid, "QP_EVENT_STEADY"))
            else:
                events_rows.append((pid, cid, "QP_FILLER_EVENT"))

        # Background filler cases: 40 cases per quarter
        # In baseline quarters (q1..q3), seed 10 cases of QP_EVENT_BURST in background
        # so baseline qPRR is ~0.41 (< 1.0). In q4, seed only 1 case in background.
        bg_burst_count = 1 if q == "2099q4" else 10
        for i in range(40):
            pid, cid = new_case(q)
            drugs_rows.append((pid, cid, "QP_BG_DRUG", "SS"))
            if i < bg_burst_count:
                events_rows.append((pid, cid, "QP_EVENT_BURST"))
            else:
                events_rows.append((pid, cid, "QP_BG_EVENT"))

    execute_values(cur, """
        INSERT INTO cases (primaryid, case_id, source_quarter, is_current)
        VALUES %s
    """, cases_rows)
    execute_values(cur, """
        INSERT INTO case_drugs (primaryid, case_id, drug_normalized, role)
        VALUES %s
    """, drugs_rows)
    execute_values(cur, """
        INSERT INTO case_events (primaryid, case_id, event_term)
        VALUES %s
    """, events_rows)

    # Seed signal_scores rows (candidate signals: prr_screen = True)
    execute_values(cur, """
        INSERT INTO signal_scores (drug, event, period, a, b, c, d, case_count,
                                   prr, chi_square, primary_suspect_mode, prr_screen)
        VALUES %s
    """, [
        ("QP_NEWDRUG", "QP_EVENT_BURST", "all", 18, 62, 10, 230, 18, 2.8, 15.0, False, True),
        ("QP_STEADYDRUG", "QP_EVENT_STEADY", "all", 40, 40, 10, 230, 40, 3.2, 25.0, False, True),
    ])

    # Seed signal_quarterly rows
    q_scores = []
    for q in quarters:
        a_new = 15 if q == "2099q4" else 1
        q_scores.append(("QP_NEWDRUG", "QP_EVENT_BURST", q, a_new, 320, False))
        q_scores.append(("QP_STEADYDRUG", "QP_EVENT_STEADY", q, 10, 320, False))

    execute_values(cur, """
        INSERT INTO signal_quarterly (drug, event, quarter, a, background_n, primary_suspect_mode)
        VALUES %s
    """, q_scores)

    conn.commit()
    conn.close()


def test_quarterly_profiles_detection(qprofile_db):
    """End-to-end execution of quarterly_profiles against PostgreSQL."""
    _seed_synthetic_profiles(qprofile_db)

    conn = psycopg2.connect(qprofile_db)
    try:
        report = quarterly_profiles(conn, top=10, mode="all", min_cases=1)
        assert report["status"] == "ok"
        assert report["signals_evaluated"] == 2
        assert report["evaluated_quarter"] == "2099q4"

        counts = report["verdict_counts"]
        assert counts.get("elevation_is_new") == 1
        assert counts.get("consistent") == 1

        flagged = report["flagged"]
        assert len(flagged) == 1
        f = flagged[0]
        assert f["drug"] == "QP_NEWDRUG"
        assert f["event"] == "QP_EVENT_BURST"
        assert f["verdict"] == "elevation_is_new"
        assert f["quarter"] == "2099q4"
        assert f["baseline_median_prr"] < 1.0
        assert f["latest_prr"] >= 2.0
        assert f["quarters_evaluable"] == 4
        assert [r["quarter"] for r in f["series"] if r["quarter"].startswith("2099")] == [
            "2099q1", "2099q2", "2099q3", "2099q4"
        ]
    finally:
        conn.close()


def test_quarterly_profiles_empty_database(qprofile_db):
    """When no signals exist in the database, return an empty report cleanly."""
    conn = psycopg2.connect(qprofile_db)
    try:
        report = quarterly_profiles(conn, top=10, mode="all")
        assert report["status"] == "ok"
        assert report["signals_evaluated"] == 0
        assert report["evaluated_quarter"] is None
        assert report["flagged"] == []
        assert report["verdict_counts"] == {}
    finally:
        conn.close()

