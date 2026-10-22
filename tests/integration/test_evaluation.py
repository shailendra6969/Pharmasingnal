"""Integration tests for the evaluation harness against the test database (spec §35).

Seeds synthetic signals + label chunks, runs the full baseline case set and
verifies both scored outcomes and persistence into eval_questions/eval_results.
"""
from __future__ import annotations

import psycopg2
import pytest

from app.config import settings
from app.evaluation import BASELINE_CASES, run_evaluation
from app.rag.spl_parser import DocChunk
from app.repositories import doc_repository

_SEED_SIGNALS_SQL = """
    INSERT INTO signal_scores (
        drug, event, period, a, b, c, d, case_count,
        prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
        chi_square, prr_screen, ror_screen, background_n,
        drug_case_count, event_case_count, primary_suspect_mode
    ) VALUES (%s, %s, 'all', 909, 65298, 4134, 2761434, %s,
              %s, %s, %s, %s, %s, %s,
              5437.8, TRUE, TRUE, 2831775,
              66207, 5043, FALSE)
"""

_EVALUATION_CHUNKS = [
    DocChunk(
        chunk_id="eval_atorv_001",
        drug="ATORVASTATIN",
        doc_type="SPL_LABEL",
        section="WARNINGS_AND_PRECAUTIONS",
        subsection="5.1 Myopathy and Rhabdomyolysis",
        text="LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        source_url="https://dailymed.example.com/eval-set",
        document_date="2024-04-15",
        chunk_index=1,
        metadata={"set_id": "test-eval-setid"},
    ),
    DocChunk(
        chunk_id="eval_atorv_002",
        drug="ATORVASTATIN",
        doc_type="SPL_LABEL",
        section="ADVERSE_REACTIONS",
        subsection="6.2 Postmarketing Experience",
        text="Cases of rhabdomyolysis have been reported with statin therapy.",
        source_url="https://dailymed.example.com/eval-set",
        document_date="2024-04-15",
        chunk_index=2,
        metadata={"set_id": "test-eval-setid"},
    ),
]

_CLEANUP_SQL = [
    "DELETE FROM eval_results WHERE question_id LIKE 'eval_%';",
    "DELETE FROM eval_questions WHERE question_id LIKE 'eval_%';",
    "DELETE FROM doc_chunks WHERE drug IN ('ATORVASTATIN', 'PREDNISONE');",
    "DELETE FROM signal_scores WHERE (drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS')"
    " OR (drug = 'PREDNISONE' AND event = 'FATIGUE');",
    "DELETE FROM signal_quarterly WHERE (drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS')"
    " OR (drug = 'PREDNISONE' AND event = 'FATIGUE');",
]


@pytest.fixture
def eval_conn():
    try:
        conn = psycopg2.connect(settings.test_database_url())
    except psycopg2.OperationalError:
        pytest.skip("Test database not reachable; skipping evaluation integration tests")

    with conn.cursor() as cur:
        for sql in _CLEANUP_SQL:
            cur.execute(sql)
        # Positive / tamper scenarios: ATORVASTATIN / RHABDOMYOLYSIS
        cur.execute(_SEED_SIGNALS_SQL, ("ATORVASTATIN", "RHABDOMYOLYSIS", 909, 9.18, 8.55, 9.86, 9.30, 8.65, 10.00))
        # Missing-evidence scenario: real signal, but deliberately no label chunks
        cur.execute(_SEED_SIGNALS_SQL, ("PREDNISONE", "FATIGUE", 9356, 2.58, 2.40, 2.77, 2.60, 2.42, 2.79))
    conn.commit()
    doc_repository.replace_label_chunks(
        conn, "ATORVASTATIN", "test-eval-setid", _EVALUATION_CHUNKS
    )

    yield conn

    with conn.cursor() as cur:
        for sql in _CLEANUP_SQL:
            cur.execute(sql)
    conn.commit()
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
