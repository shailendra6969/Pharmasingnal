"""Integration tests for the evaluation harness against the test database (spec §35).

Seeds the shared evaluation dataset (app/evaluation/seed.py — the same rows
CI plants with scripts/seed_evaluation.py), runs the full baseline case set
and verifies both scored outcomes and persistence into eval_questions/eval_results.
"""
from __future__ import annotations

import psycopg2
import pytest

from app.config import settings
from app.evaluation import BASELINE_CASES, run_evaluation
from app.evaluation.seed import cleanup, seed_evaluation_data


@pytest.fixture
def eval_conn():
    try:
        conn = psycopg2.connect(settings.test_database_url())
    except psycopg2.OperationalError:
        pytest.skip("Test database not reachable; skipping evaluation integration tests")

    # Same dataset scripts/seed_evaluation.py plants in CI, so this test and
    # the CI evaluation run can never drift apart.
    seed_evaluation_data(conn)

    yield conn

    cleanup(conn)
    conn.close()


def test_evaluation_run_matches_expectations_and_persists(eval_conn):
    report = run_evaluation(eval_conn, persist=True)

    assert report.failed == 0, [r.summary() for r in report.results if r.outcome == "FAIL"]
    assert report.skipped == 0, [r.summary() for r in report.results if r.outcome == "SKIP"]
    assert report.all_matched is True
    assert len(report.results) == len(BASELINE_CASES)
    for result in report.results:
        assert result.outcome == "PASS", result.summary()

    with eval_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM eval_questions WHERE question_id LIKE 'eval_%'")
        assert cur.fetchone()[0] == len(BASELINE_CASES)

        cur.execute(
            """
            SELECT question_id, numeric_accuracy, citation_correctness,
                   verification_accuracy, verification_results
            FROM eval_results
            WHERE question_id LIKE 'eval_%'
            """
        )
        rows = {r[0]: r for r in cur.fetchall()}

    assert len(rows) == len(BASELINE_CASES)

    # positive control fully accurate
    assert rows["eval_valid_atorvastatin_rhabdomyolysis"][1] == 1.0
    assert rows["eval_valid_atorvastatin_rhabdomyolysis"][2] == 1.0
    assert rows["eval_valid_atorvastatin_rhabdomyolysis"][3] == 1.0

    # unsupported claim: numeric accuracy below 1, still expected-flagged
    assert rows["eval_unsupported_claim_atorvastatin"][1] < 1.0
    assert rows["eval_unsupported_claim_atorvastatin"][3] == 1.0

    # incorrect citation: citation correctness 0, numeric intact
    assert rows["eval_incorrect_citation_atorvastatin"][2] == 0.0
    assert rows["eval_incorrect_citation_atorvastatin"][1] == 1.0

    # missing evidence: gate errors persisted and outcome matched expectation
    missing_verification = rows["eval_missing_evidence_prednisone"][4]
    assert isinstance(missing_verification, dict)
    assert any(
        "No label evidence" in e for e in missing_verification["evidence"]["errors"]
    )
    assert rows["eval_missing_evidence_prednisone"][3] == 1.0


def test_evaluation_second_run_remains_deterministic(eval_conn):
    first = run_evaluation(eval_conn, persist=True)
    second = run_evaluation(eval_conn, persist=True)
    assert first.all_matched and second.all_matched
    assert [r.status for r in first.results] == [r.status for r in second.results]

    with eval_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM eval_results WHERE question_id LIKE 'eval_%'")
        # two runs append two result rows per case
        assert cur.fetchone()[0] == 2 * len(BASELINE_CASES)
