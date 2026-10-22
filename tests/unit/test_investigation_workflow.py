"""Unit tests for the investigation workflow state graph (spec §23–26)."""
from __future__ import annotations

import pytest

from app.agents.investigation import (
    SCIENTIFIC_DISCLAIMER,
    InvestigationState,
    InvestigationWorkflow,
    derive_structured_claims,
    extract_citation_snippet,
)
from app.verification.claim_verifier import StructuredClaim


class MockWorkflowCursor:
    def __init__(self, signal_dict: dict | None, chunks: list[dict]):
        self.signal_dict = signal_dict
        self.chunks = chunks

    def execute(self, query: str, params: tuple = ()):
        pass

    def fetchone(self):
        return self.signal_dict

    def fetchall(self):
        return self.chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockWorkflowConnection:
    def __init__(self, signal_dict: dict | None = None, chunks: list[dict] = None):
        self._cur = MockWorkflowCursor(signal_dict, chunks or [])

    def cursor(self, cursor_factory=None):
        return self._cur


def test_investigation_workflow_full_pipeline_success():
    signal_dict = {
        "signal_id": 1,
        "drug": "ATORVASTATIN",
        "event": "RHABDOMYOLYSIS",
        "period": "all",
        "a": 909,
        "b": 65298,
        "c": 4134,
        "d": 2761434,
        "case_count": 909,
        "prr": 9.18,
        "prr_lower": 8.55,
        "prr_upper": 9.86,
        "ror": 9.30,
        "ror_lower": 8.65,
        "ror_upper": 10.00,
        "chi_square": 5437.8,
        "prr_screen": True,
        "ror_screen": True,
        "background_n": 2831775,
        "primary_suspect_mode": False,
    }
    chunk = {
        "chunk_id": "atorv_0001",
        "drug": "ATORVASTATIN",
        "doc_type": "SPL_LABEL",
        "section": "WARNINGS_AND_PRECAUTIONS",
        "subsection": "5.1 Myopathy and Rhabdomyolysis",
        "text": "LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        "source_url": "https://dailymed.example.com",
        "document_date": "2024-04-15",
        "chunk_index": 1,
        "metadata": {"set_id": "set-123"},
    }

    conn = MockWorkflowConnection(signal_dict=signal_dict, chunks=[chunk])
    workflow = InvestigationWorkflow(conn=conn)

    report = workflow.run(drug="ATORVASTATIN", event="RHABDOMYOLYSIS")

    assert report["drug"] == "ATORVASTATIN"
    assert report["event"] == "RHABDOMYOLYSIS"
    assert report["status"] == "VERIFIED"
    assert report["metrics"]["case_count"] == 909
    assert report["metrics"]["prr"] == 9.18
    assert report["metrics"]["screened"] is True

    # Verification checks
    v = report["verification"]
    assert v is not None
    assert v["passed"] is True
    assert v["claims"]["passed"] > 0
    assert v["claims"]["failed"] == 0
    assert v["citations"]["passed"] is True
    assert v["causal_guard"]["passed"] is True

    # Trace progression
    trace = report["trace"]
    assert "plan" in trace
    assert "gather_evidence" in trace
    assert "synthesize" in trace
    assert "verify" in trace
    assert "finalize" in trace
    assert SCIENTIFIC_DISCLAIMER in report["scientific_disclaimer"]


def test_investigation_workflow_requires_connection():
    workflow = InvestigationWorkflow(conn=None)
    with pytest.raises(ValueError, match="database connection is required"):
        workflow.run(drug="ATORVASTATIN", event="RHABDOMYOLYSIS")


def test_investigation_workflow_retry_loop_on_failure(monkeypatch):
    signal_dict = {
        "signal_id": 1,
        "drug": "ATORVASTATIN",
        "event": "RHABDOMYOLYSIS",
        "period": "all",
        "a": 909,
        "b": 65298,
        "c": 4134,
        "d": 2761434,
        "prr": 9.18,
        "prr_lower": 8.55,
        "prr_upper": 9.86,
        "ror": 9.30,
        "ror_lower": 8.65,
        "ror_upper": 10.00,
        "chi_square": 5437.8,
        "prr_screen": True,
        "background_n": 2831775,
        "primary_suspect_mode": False,
    }
    chunk = {
        "chunk_id": "atorv_0001",
        "drug": "ATORVASTATIN",
        "doc_type": "SPL_LABEL",
        "section": "WARNINGS_AND_PRECAUTIONS",
        "subsection": "5.1 Myopathy and Rhabdomyolysis",
        "text": "LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        "source_url": "https://dailymed.example.com",
        "document_date": "2024-04-15",
        "chunk_index": 1,
        "metadata": {"set_id": "set-123"},
    }

    conn = MockWorkflowConnection(signal_dict=signal_dict, chunks=[chunk])
    workflow = InvestigationWorkflow(conn=conn)

    # Simulate attempt 1 introducing a hallucinated claim that triggers retry
    original_synthesize = workflow._synthesize_node

    def flaky_synthesize(state: InvestigationState) -> InvestigationState:
        state = original_synthesize(state)
        if state.retry_count == 0:
            # Inject a hallucinated claim on attempt 0
            state.claims.append(StructuredClaim(state.drug, state.event, "prr", 999.0))
        return state

    monkeypatch.setattr(workflow, "_synthesize_node", flaky_synthesize)

    report = workflow.run(drug="ATORVASTATIN", event="RHABDOMYOLYSIS")
    # Verify retry occurred
    assert "retry" in report["trace"]
    # Final state repaired
    assert report["status"] == "VERIFIED"


def test_investigation_workflow_empty_evidence_produces_flagged_with_warnings():
    signal_dict = {
        "signal_id": 2,
        "drug": "IBUPROFEN",
        "event": "GASTRIC ULCER",
        "period": "all",
        "a": 166,
        "b": 10000,
        "c": 4000,
        "d": 2800000,
        "case_count": 166,
        "prr": 8.52,
        "prr_lower": 7.32,
        "prr_upper": 9.91,
        "ror": 8.56,
        "ror_lower": 7.35,
        "ror_upper": 9.97,
        "chi_square": 1005.4,
        "prr_screen": True,
        "background_n": 2831775,
        "primary_suspect_mode": False,
    }
    # No chunks available (like current production doc_chunks table)
    conn = MockWorkflowConnection(signal_dict=signal_dict, chunks=[])
    workflow = InvestigationWorkflow(conn=conn)

    report = workflow.run(drug="IBUPROFEN", event="GASTRIC ULCER", investigation_type="comprehensive")

    assert report["status"] == "FLAGGED_WITH_WARNINGS"
    assert report["verification"]["passed"] is False
    assert report["verification"]["evidence"]["passed"] is False
    assert len(report["verification"]["evidence"]["errors"]) > 0


def test_investigation_workflow_numeric_only_passes_without_label_evidence():
    signal_dict = {
        "signal_id": 2,
        "drug": "IBUPROFEN",
        "event": "GASTRIC ULCER",
        "period": "all",
        "a": 166,
        "b": 10000,
        "c": 4000,
        "d": 2800000,
        "case_count": 166,
        "prr": 8.52,
        "prr_lower": 7.32,
        "prr_upper": 9.91,
        "ror": 8.56,
        "ror_lower": 7.35,
        "ror_upper": 9.97,
        "chi_square": 1005.4,
        "prr_screen": True,
        "background_n": 2831775,
        "primary_suspect_mode": False,
    }
    conn = MockWorkflowConnection(signal_dict=signal_dict, chunks=[])
    workflow = InvestigationWorkflow(conn=conn)

    report = workflow.run(drug="IBUPROFEN", event="GASTRIC ULCER", investigation_type="numeric_only")

    assert report["status"] == "VERIFIED"
    assert report["investigation_type"] == "numeric_only"
    assert report["evidence"]["evidence_required"] is False
    assert report["verification"]["passed"] is True
    assert report["verification"]["evidence"]["passed"] is True


def test_investigation_workflow_causal_statement_fails_verification(monkeypatch):
    signal_dict = {
        "signal_id": 1,
        "drug": "ATORVASTATIN",
        "event": "RHABDOMYOLYSIS",
        "period": "all",
        "a": 909,
        "b": 65298,
        "c": 4134,
        "d": 2761434,
        "case_count": 909,
        "prr": 9.18,
        "prr_lower": 8.55,
        "prr_upper": 9.86,
        "ror": 9.30,
        "ror_lower": 8.65,
        "ror_upper": 10.00,
        "chi_square": 5437.8,
        "prr_screen": True,
        "background_n": 2831775,
        "primary_suspect_mode": False,
    }
    chunk = {
        "chunk_id": "atorv_0001",
        "drug": "ATORVASTATIN",
        "doc_type": "SPL_LABEL",
        "section": "WARNINGS_AND_PRECAUTIONS",
        "subsection": "5.1 Myopathy and Rhabdomyolysis",
        "text": "LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        "source_url": "https://dailymed.example.com",
        "document_date": "2024-04-15",
        "chunk_index": 1,
        "metadata": {"set_id": "set-123"},
    }
    conn = MockWorkflowConnection(signal_dict=signal_dict, chunks=[chunk])
    workflow = InvestigationWorkflow(conn=conn)

    # Monkeypatch synthesize to inject direct causal assertion
    orig_synth = workflow._synthesize_node

    def causal_synth(state: InvestigationState) -> InvestigationState:
        state = orig_synth(state)
        state.draft_summary += "\nIbuprofen causes gastric ulcer."
        return state

    monkeypatch.setattr(workflow, "_synthesize_node", causal_synth)

    report = workflow.run(drug="ATORVASTATIN", event="RHABDOMYOLYSIS")

    assert report["status"] == "FLAGGED_WITH_WARNINGS"
    assert report["verification"]["passed"] is False
    assert report["verification"]["causal_guard"]["passed"] is False
    assert any("causes gastric" in v for v in report["verification"]["causal_guard"]["violations"])


def test_extract_citation_snippet_starts_at_sentence_boundary():
    # The matched term sits mid-sentence after an odd clause separator; the
    # quoted span must begin at the sentence start, never after punctuation.
    text = (
        "Table 2 lists interacting drugs. , breast cancer resistant protein "
        "[BCRP], organic anion-transporting polypeptide and P-glycoprotein "
        "resulting in an increased risk of rhabdomyolysis."
    )
    snippet = extract_citation_snippet(text, ["rhabdomyolysis"])
    assert snippet is not None
    assert snippet.startswith("[BCRP]") or snippet.startswith("breast")
    assert not snippet.startswith(",")
    assert "rhabdomyolysis" in snippet
    # verbatim: the snippet must exist in the source text
    assert snippet in text


def test_extract_citation_snippet_picks_sentence_containing_term():
    text = "First sentence is irrelevant. Second sentence mentions rhabdomyolysis directly. Third sentence."
    snippet = extract_citation_snippet(text, ["rhabdomyolysis"])
    assert snippet == "Second sentence mentions rhabdomyolysis directly."


def test_extract_citation_snippet_falls_back_when_no_match():
    text = "A short label chunk without the queried term."
    snippet = extract_citation_snippet(text, ["rhabdomyolysis"])
    assert snippet == text
    assert extract_citation_snippet("", ["x"]) is None


def test_derive_structured_claims_skips_absent_metrics():
    claims = derive_structured_claims(
        "X", "Y", {"a": 10, "prr": 2.0, "ror": None, "chi_square": 5.0, "background_n": None}
    )
    assert [c.metric for c in claims] == ["case_count", "prr", "chi_square"]
    assert derive_structured_claims("X", "Y", None) == []


def test_retry_regathers_evidence_with_wider_window(monkeypatch):
    signal_dict = {
        "signal_id": 1,
        "drug": "ATORVASTATIN",
        "event": "RHABDOMYOLYSIS",
        "period": "all",
        "a": 909,
        "b": 65298,
        "c": 4134,
        "d": 2761434,
        "case_count": 909,
        "prr": 9.18,
        "prr_lower": 8.55,
        "prr_upper": 9.86,
        "ror": 9.30,
        "ror_lower": 8.65,
        "ror_upper": 10.00,
        "chi_square": 5437.8,
        "prr_screen": True,
        "background_n": 2831775,
        "primary_suspect_mode": False,
    }
    chunk = {
        "chunk_id": "atorv_0001",
        "drug": "ATORVASTATIN",
        "doc_type": "SPL_LABEL",
        "section": "WARNINGS_AND_PRECAUTIONS",
        "subsection": "5.1 Myopathy and Rhabdomyolysis",
        "text": "LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        "source_url": "https://dailymed.example.com",
        "document_date": "2024-04-15",
        "chunk_index": 1,
        "metadata": {"set_id": "set-123"},
    }
    conn = MockWorkflowConnection(signal_dict=signal_dict, chunks=[chunk])
    workflow = InvestigationWorkflow(conn=conn)

    original_synthesize = workflow._synthesize_node
    original_gather = workflow._gather_evidence_node
    gather_limits: list[int] = []

    def flaky_synthesize(state: InvestigationState) -> InvestigationState:
        state = original_synthesize(state)
        if state.retry_count == 0:
            state.claims.append(StructuredClaim(state.drug, state.event, "prr", 999.0))
        return state

    def tracking_gather(state: InvestigationState, limit: int = 5):
        gather_limits.append(limit)
        return original_gather(state, limit=limit)

    monkeypatch.setattr(workflow, "_synthesize_node", flaky_synthesize)
    monkeypatch.setattr(workflow, "_gather_evidence_node", tracking_gather)

    report = workflow.run(drug="ATORVASTATIN", event="RHABDOMYOLYSIS")

    assert "retry" in report["trace"]
    # first attempt + a genuine re-gather on retry, with a wider window
    assert gather_limits == [5, 10]
    assert report["trace"].count("gather_evidence") == 2
    assert report["status"] == "VERIFIED"

