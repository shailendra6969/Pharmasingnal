"""Integration tests for investigation tools and LangGraph workflow (spec §23–26).

Runs end-to-end against settings.test_database_url() (never touching production data).
"""
from __future__ import annotations

import psycopg2
import pytest

from app.agents.investigation import InvestigationWorkflow
from app.config import settings
from app.rag.spl_parser import DocChunk
from app.repositories import doc_repository
from app.tools.label_tool import search_labels
from app.tools.signal_tool import get_signal_scores


@pytest.fixture
def test_db_setup():
    test_url = settings.test_database_url()
    try:
        conn = psycopg2.connect(test_url)
    except psycopg2.OperationalError:
        pytest.skip("Test database not reachable; skipping investigation integration tests")

    # Clean and insert test data
    with conn.cursor() as cur:
        cur.execute("DELETE FROM doc_chunks WHERE drug = 'ATORVASTATIN';")
        cur.execute("DELETE FROM signal_scores WHERE drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS';")
        cur.execute("DELETE FROM signal_quarterly WHERE drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS';")

        # Insert synthetic signal_scores record
        cur.execute(
            """
            INSERT INTO signal_scores (
                drug, event, period, a, b, c, d, case_count,
                prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
                chi_square, prr_screen, ror_screen, background_n,
                drug_case_count, event_case_count, primary_suspect_mode
            ) VALUES (
                'ATORVASTATIN', 'RHABDOMYOLYSIS', 'all', 909, 65298, 4134, 2761434, 909,
                9.18, 8.55, 9.86, 9.30, 8.65, 10.00,
                5437.8, TRUE, TRUE, 2831775,
                66207, 5043, FALSE
            );
            """
        )
        cur.execute(
            """
            INSERT INTO signal_quarterly (
                drug, event, quarter, a, background_n, primary_suspect_mode
            ) VALUES
                ('ATORVASTATIN', 'RHABDOMYOLYSIS', '2024q1', 110, 350000, FALSE),
                ('ATORVASTATIN', 'RHABDOMYOLYSIS', '2024q2', 120, 355000, FALSE);
            """
        )
    conn.commit()

    # Insert synthetic doc_chunks
    chunks = [
        DocChunk(
            chunk_id="test_atorv_001",
            drug="ATORVASTATIN",
            doc_type="SPL_LABEL",
            section="WARNINGS_AND_PRECAUTIONS",
            subsection="5.1 Myopathy and Rhabdomyolysis",
            text="LIPITOR may cause myopathy and rhabdomyolysis. Acute kidney injury has occurred.",
            source_url="https://dailymed.nlm.nih.gov/test",
            document_date="2024-04-15",
            chunk_index=1,
            metadata={"set_id": "test-lipitor-setid", "section_title": "5.1 Myopathy and Rhabdomyolysis"},
        ),
        DocChunk(
            chunk_id="test_atorv_002",
            drug="ATORVASTATIN",
            doc_type="SPL_LABEL",
            section="ADVERSE_REACTIONS",
            subsection="6.1 Clinical Trials Experience",
            text="Common adverse reactions in clinical trials include nasopharyngitis and arthralgia.",
            source_url="https://dailymed.nlm.nih.gov/test",
            document_date="2024-04-15",
            chunk_index=2,
            metadata={"set_id": "test-lipitor-setid", "section_title": "6.1 Clinical Trials Experience"},
        ),
    ]
    doc_repository.replace_label_chunks(conn, "ATORVASTATIN", "test-lipitor-setid", chunks)

    yield conn

    # Teardown
    with conn.cursor() as cur:
        cur.execute("DELETE FROM doc_chunks WHERE drug = 'ATORVASTATIN';")
        cur.execute("DELETE FROM signal_scores WHERE drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS';")
        cur.execute("DELETE FROM signal_quarterly WHERE drug = 'ATORVASTATIN' AND event = 'RHABDOMYOLYSIS';")
    conn.commit()
    conn.close()


def test_tool_get_signal_scores_live_db(test_db_setup):
    res = get_signal_scores(test_db_setup, "ATORVASTATIN", "RHABDOMYOLYSIS")
    assert res is not None
    assert res["drug"] == "ATORVASTATIN"
    assert res["event"] == "RHABDOMYOLYSIS"
    assert res["a"] == 909
    assert res["prr"] == 9.18
    assert res["prr_screen"] is True
    assert len(res["quarterly_history"]) == 2


def test_tool_search_labels_live_db(test_db_setup):
    results = search_labels(test_db_setup, "ATORVASTATIN", "rhabdomyolysis")
    assert len(results) > 0
    top = results[0]
    assert top["chunk_id"] == "test_atorv_001"
    assert top["section"] == "WARNINGS_AND_PRECAUTIONS"
    assert "rhabdomyolysis" in top["text"].lower()


def test_investigation_workflow_end_to_end_on_test_db(test_db_setup):
    workflow = InvestigationWorkflow(conn=test_db_setup)
    report = workflow.run(drug="ATORVASTATIN", event="RHABDOMYOLYSIS")

    assert report["status"] == "VERIFIED"
    assert report["drug"] == "ATORVASTATIN"
    assert report["event"] == "RHABDOMYOLYSIS"
    assert report["metrics"]["case_count"] == 909
    assert report["metrics"]["prr"] == 9.18
    assert report["metrics"]["screened"] is True

    # Verification passed
    v = report["verification"]
    assert v["passed"] is True
    assert v["claims"]["total"] >= 4
    assert v["claims"]["failed"] == 0
    assert v["citations"]["passed"] is True
    assert v["causal_guard"]["passed"] is True
    assert len(report["citations"]) > 0
