"""Unit tests for investigation tools: get_signal_scores and search_labels (spec §23–26)."""
from __future__ import annotations

import pytest

from app.tools.label_tool import (
    FALLBACK_SECTIONS,
    SECTION_WEIGHTS,
    _score_chunk,
    _tokenize,
    search_labels,
)
from app.tools.signal_tool import get_signal_scores


class MockCursor:
    def __init__(self, fetchone_results: list[dict | None] = None, fetchall_results: list[list[dict]] = None):
        self._fetchone = list(fetchone_results or [])
        self._fetchall = list(fetchall_results or [])
        self.queries: list[str] = []

    def execute(self, query: str, params: tuple = ()):
        self.queries.append(query)

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockConnection:
    def __init__(self, cursor: MockCursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        return self._cursor


def test_tokenize_clean_words():
    tokens = _tokenize("Acute Kidney Injury (AKI) secondary to Rhabdomyolysis!")
    assert "acute" in tokens
    assert "kidney" in tokens
    assert "injury" in tokens
    assert "rhabdomyolysis" in tokens
    assert "to" not in tokens  # < 3 chars


def test_score_chunk_section_weighting():
    text = "Rare fatalities have occurred as a result of rhabdomyolysis."
    tokens = {"rhabdomyolysis"}

    boxed_score, m1 = _score_chunk(text, "BOXED_WARNING", tokens)
    warn_score, m2 = _score_chunk(text, "WARNINGS_AND_PRECAUTIONS", tokens)
    other_score, m3 = _score_chunk(text, "OTHER", tokens)

    assert m1 == m2 == m3 == ["rhabdomyolysis"]
    assert boxed_score > warn_score > other_score
    assert boxed_score == round((0.5 + 2.0) * SECTION_WEIGHTS["BOXED_WARNING"], 3)


def test_get_signal_scores_structure():
    mock_signal = {
        "signal_id": 101,
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
        "insufficient": False,
        "continuity_corrected": False,
        "background_n": 2831775,
        "drug_case_count": 66207,
        "event_case_count": 5043,
        "quarters_observed": 8,
        "quarters_screening": 8,
        "primary_suspect_mode": False,
        "created_at": None,
    }
    mock_counter = dict(mock_signal, primary_suspect_mode=True, a=350, prr=10.2)
    mock_history = [
        {"quarter": "2024q1", "a": 110, "background_n": 350000, "primary_suspect_mode": False},
        {"quarter": "2024q2", "a": 120, "background_n": 355000, "primary_suspect_mode": False},
    ]

    cur = MockCursor(
        fetchone_results=[mock_signal, mock_counter],
        fetchall_results=[mock_history],
    )
    conn = MockConnection(cur)

    res = get_signal_scores(conn, "ATORVASTATIN", "RHABDOMYOLYSIS", mode="all")
    assert res is not None
    assert res["drug"] == "ATORVASTATIN"
    assert res["event"] == "RHABDOMYOLYSIS"
    assert res["a"] == 909
    assert res["prr"] == 9.18
    assert res["sensitivity_counterpart"]["a"] == 350
    assert len(res["quarterly_history"]) == 2


def test_get_signal_scores_not_found():
    cur = MockCursor(fetchone_results=[None])
    conn = MockConnection(cur)
    res = get_signal_scores(conn, "UNKNOWN", "EVENT")
    assert res is None


def test_search_labels_with_fallback_for_otc():
    """Verify search_labels activates fallback to WARNINGS when ADVERSE_REACTIONS is empty."""
    # First query for ADVERSE_REACTIONS returns empty []
    # Second query for WARNINGS_AND_PRECAUTIONS returns Advil stomach bleeding chunk
    advil_chunk = {
        "chunk_id": "advil_001",
        "drug": "IBUPROFEN",
        "doc_type": "SPL_LABEL",
        "section": "WARNINGS_AND_PRECAUTIONS",
        "subsection": "Stomach bleeding warning",
        "text": "Stomach bleeding warning: This product contains an NSAID, which may cause severe stomach bleeding.",
        "source_url": "https://dailymed.example.com",
        "document_date": "2026-07-29",
        "chunk_index": 1,
        "metadata": {"set_id": "advil-set-id"},
    }

    cur = MockCursor(
        fetchall_results=[
            [],              # Primary ADVERSE_REACTIONS search -> empty
            [advil_chunk],   # Fallback WARNINGS_AND_PRECAUTIONS -> finds chunk
            [],              # Fallback BOXED_WARNING
            [],              # Fallback CONTRAINDICATIONS
        ]
    )
    conn = MockConnection(cur)

    results = search_labels(conn, "IBUPROFEN", "stomach bleeding", section="ADVERSE_REACTIONS")
    assert len(results) == 1
    assert results[0]["chunk_id"] == "advil_001"
    assert results[0]["fallback_searched"] is True
    assert "stomach" in results[0]["matched_terms"]
    assert results[0]["score"] > 0
