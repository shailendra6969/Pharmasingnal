"""Unit tests for the evaluation harness (spec §8, §35).

Uses an in-memory mock connection so scenario scoring, tamper verification
and skip-precondition logic are exercised without PostgreSQL.
"""
from __future__ import annotations

from app.evaluation import BASELINE_CASES, run_case
from app.evaluation.cases import FLAGGED, VERIFIED, EvalCase

# ------------------------------------------------ fixtures / mocks ---------

SIGNAL_ATORVATIN = {
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

SIGNAL_PREDNISONE = {
    **SIGNAL_ATORVATIN,
    "signal_id": 2,
    "drug": "PREDNISONE",
    "event": "FATIGUE",
    "a": 9356,
    "case_count": 9356,
    "prr": 2.58,
    "ror": 2.60,
}

ATORVASTATIN_CHUNKS = [
    {
        "chunk_id": "atorv_0001",
        "drug": "ATORVASTATIN",
        "doc_type": "SPL_LABEL",
        "section": "WARNINGS_AND_PRECAUTIONS",
        "subsection": "5.1 Myopathy and Rhabdomyolysis",
        "text": "LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        "source_url": "https://dailymed.example.com/set-1",
        "document_date": "2024-04-15",
        "chunk_index": 1,
        "metadata": {"set_id": "set-1"},
    },
    {
        "chunk_id": "atorv_0002",
        "drug": "ATORVASTATIN",
        "doc_type": "SPL_LABEL",
        "section": "ADVERSE_REACTIONS",
        "subsection": "6.2 Postmarketing Experience",
        "text": "Cases of rhabdomyolysis have been reported with statin therapy.",
        "source_url": "https://dailymed.example.com/set-1",
        "document_date": "2024-04-15",
        "chunk_index": 2,
        "metadata": {"set_id": "set-1"},
    },
]


class _Store:
    def __init__(self, signals: dict, chunks: dict):
        self.signals = signals
        self.chunks = chunks


class _MockCursor:
    """Routes the exact query shapes used by the tools under test."""

    def __init__(self, store: _Store):
        self._store = store
        self._sql = ""
        self._params: tuple = ()
        self._rows: list | None = []

    def execute(self, query: str, params: tuple = ()):  # noqa: D401
        self._sql = " ".join(query.split())
        self._params = params
        s = self._store

        if "SELECT count(*) FROM doc_chunks" in self._sql:
            drug = params[0]
            self._rows = [(len(s.chunks.get(drug, [])),)]
        elif "FROM signal_quarterly" in self._sql:
            self._rows = []
        elif "FROM signal_scores" in self._sql:
            drug, event, primary_suspect = params[0], params[1], bool(params[2])
            row = s.signals.get((drug, event))
            match = row if (row is not None and bool(row["primary_suspect_mode"]) == primary_suspect) else None
            self._rows = [dict(match)] if match else None
        elif "FROM doc_chunks" in self._sql:
            drug = params[0]
            self._rows = [dict(c) for c in s.chunks.get(drug, [])]
        else:
            raise AssertionError(f"unexpected query in mock: {self._sql}")

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows[0]

    def fetchall(self):
        return list(self._rows or [])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _MockConnection:
    def __init__(self, store: _Store):
        self._store = store

    def cursor(self, cursor_factory=None):
        return _MockCursor(self._store)


def _conn(extra_chunks: dict | None = None) -> _MockConnection:
    chunks = {"ATORVASTATIN": list(ATORVASTATIN_CHUNKS)}
    if extra_chunks:
        chunks.update(extra_chunks)
    store = _Store(
        signals={
            ("ATORVASTATIN", "RHABDOMYOLYSIS"): SIGNAL_ATORVATIN,
            ("PREDNISONE", "FATIGUE"): SIGNAL_PREDNISONE,
        },
        chunks=chunks,
    )
    return _MockConnection(store)


def _case(question_id: str) -> EvalCase:
    for c in BASELINE_CASES:
        if c.question_id == question_id:
            return c
    raise AssertionError(f"missing baseline case {question_id}")


VALID = "eval_valid_atorvastatin_rhabdomyolysis"
UNSUPPORTED = "eval_unsupported_claim_atorvastatin"
INCORRECT = "eval_incorrect_citation_atorvastatin"
MISSING = "eval_missing_evidence_prednisone"

# ------------------------------------------------ case definitions ----------


def test_baseline_cases_cover_required_failure_categories():
    categories = {c.category for c in BASELINE_CASES}
    assert {"valid_signal", "unsupported_claim", "missing_evidence", "incorrect_citation"} <= categories
    ids = [c.question_id for c in BASELINE_CASES]
    assert len(ids) == len(set(ids))
    for c in BASELINE_CASES:
        assert c.expected_status in (VERIFIED, FLAGGED)
        assert c.rationale, f"{c.question_id} must document its rationale"


def test_baseline_cases_include_all_three_required_scenarios():
    scenarios = {c.scenario for c in BASELINE_CASES}
    assert {"unsupported_claim", "missing_evidence", "incorrect_citation"} <= scenarios


# ------------------------------------------------ scenario scoring ----------


def test_positive_case_verifies_end_to_end():
    result = run_case(_conn(), _case(VALID))
    assert result.status == VERIFIED
    assert result.outcome == "PASS"
    assert result.matched_expectation is True
    assert result.numeric_accuracy == 1.0
    assert result.citation_correctness == 1.0
    assert result.verification_accuracy == 1.0
    assert result.retrieval_metrics["chunks_retrieved"] > 0


def test_unsupported_claim_case_is_flagged_and_scored_pass():
    result = run_case(_conn(), _case(UNSUPPORTED))
    assert result.status == FLAGGED
    assert result.outcome == "PASS"
    assert result.numeric_accuracy is not None and result.numeric_accuracy < 1.0
    failed = [d for d in result.claims if not d.get("passed")]
    assert failed, "tampered claim must be recorded as failed"
    # evidence and citations stay sound in this scenario
    assert result.citation_correctness == 1.0


def test_incorrect_citation_case_is_flagged_and_scored_pass():
    result = run_case(_conn(), _case(INCORRECT))
    assert result.status == FLAGGED
    assert result.outcome == "PASS"
    assert result.citation_correctness == 0.0
    cit_errors = (result.verification.get("citations", {}) or {}).get("errors", [])
    assert cit_errors, "fabricated span must produce a citation error"
    # numeric claims remain intact in this scenario
    assert result.numeric_accuracy == 1.0


def test_missing_evidence_case_is_flagged_when_no_chunks():
    result = run_case(_conn(), _case(MISSING))
    assert result.status == FLAGGED
    assert result.outcome == "PASS"
    evidence_errors = (result.verification.get("evidence", {}) or {}).get("errors", [])
    assert any("No label evidence" in e for e in evidence_errors)
    # numeric claims pass: the signal exists, only the evidence gate fails
    assert result.numeric_accuracy == 1.0


def test_missing_evidence_case_skips_when_labels_now_exist():
    conn = _conn(extra_chunks={"PREDNISONE": [dict(ATORVASTATIN_CHUNKS[0], drug="PREDNISONE")]})
    result = run_case(conn, _case(MISSING))
    assert result.matched_expectation is None
    assert result.outcome == "SKIP"
    assert result.skip_reason and "premise no longer holds" in result.skip_reason


def test_run_case_does_not_persist_by_default():
    # persist=False must never touch the connection with writes; the mock
    # connection raises on any unexpected query, so reaching this point proves
    # only read paths were exercised.
    result = run_case(_conn(), _case(VALID))
    assert result.outcome == "PASS"
