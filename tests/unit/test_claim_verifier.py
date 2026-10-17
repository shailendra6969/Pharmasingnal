"""Unit tests for deterministic claim verifier, citation checker, and causal guard (spec §27–32)."""
from __future__ import annotations

import pytest

from app.verification.claim_verifier import (
    Citation,
    StructuredClaim,
    check_causal_language,
    verify_citations,
    verify_investigation_report,
    verify_numeric_claims,
)

SAMPLE_SIGNAL_DATA = {
    "drug": "ATORVASTATIN",
    "event": "RHABDOMYOLYSIS",
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
    "background_n": 2831775,
}

SAMPLE_CHUNKS = [
    {
        "chunk_id": "chunk_001",
        "text": (
            "LIPITOR may cause myopathy and rhabdomyolysis. "
            "Acute kidney injury secondary to myoglobinuria has occurred."
        ),
    },
    {
        "chunk_id": "chunk_002",
        "text": "Common adverse reactions include diarrhea and nasopharyngitis.",
    },
]


def test_verify_numeric_claims_exact_counts():
    claims = [
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "case_count", 909),
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "background_n", 2831775),
    ]
    passed, p_cnt, f_cnt, details = verify_numeric_claims(claims, SAMPLE_SIGNAL_DATA)
    assert passed is True
    assert p_cnt == 2
    assert f_cnt == 0
    assert all(d["passed"] for d in details)


def test_verify_numeric_claims_tolerance_for_ratios():
    # Claiming 9.17 vs 9.18 (diff 0.01 <= 0.05 tol)
    claims = [
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "prr", 9.17),
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "ror", 9.32),
    ]
    passed, p_cnt, f_cnt, _ = verify_numeric_claims(claims, SAMPLE_SIGNAL_DATA)
    assert passed is True
    assert p_cnt == 2


def test_verify_numeric_claims_detects_hallucinations_and_drift():
    # Claiming 999 cases (actual 909) and PRR 15.0 (actual 9.18)
    claims = [
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "case_count", 999),
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "prr", 15.0),
    ]
    passed, p_cnt, f_cnt, details = verify_numeric_claims(claims, SAMPLE_SIGNAL_DATA)
    assert passed is False
    assert p_cnt == 0
    assert f_cnt == 2
    assert "diverges from authoritative" in details[0]["reason"]
    assert "diverges from authoritative" in details[1]["reason"]


def test_verify_citations_exact_and_fuzzy_spans():
    citations = [
        Citation("chunk_001", "myopathy and rhabdomyolysis"),
        Citation("chunk_002", "Common adverse reactions include diarrhea"),
    ]
    passed, errors = verify_citations(citations, SAMPLE_CHUNKS)
    assert passed is True
    assert errors == []


def test_verify_citations_flags_missing_chunk():
    citations = [
        Citation("chunk_999", "Some text from non-existent chunk"),
    ]
    passed, errors = verify_citations(citations, SAMPLE_CHUNKS)
    assert passed is False
    assert any("was not found" in e for e in errors)


def test_verify_citations_flags_hallucinated_quote():
    citations = [
        Citation("chunk_001", "Completely invented quote not in Lipitor label"),
    ]
    passed, errors = verify_citations(citations, SAMPLE_CHUNKS)
    assert passed is False
    assert any("Quoted span" in e and "was not found" in e for e in errors)


def test_causal_language_guard_clean_epidemiological_text():
    clean_text = (
        "FAERS spontaneous reporting reveals an elevated disproportionality signal "
        "for Atorvastatin and Rhabdomyolysis (PRR 9.18, 909 cases). Rhabdomyolysis is listed "
        "in official product labeling under Warnings. Spontaneous reports reflect voluntary signals "
        "and do not establish clinical risk or causality."
    )
    passed, violations = check_causal_language(clean_text)
    assert passed is True
    assert violations == []


def test_causal_language_guard_rejects_causal_certainty():
    bad_texts = [
        "This disproportionality proves that Lipitor causes rhabdomyolysis.",
        "FAERS data proves causality in these patients.",
        "Atorvastatin definitively causes acute kidney injury.",
        "The findings establish causation between the drug and event.",
        "Clinical analysis offers conclusive proof of harm.",
    ]
    for text in bad_texts:
        passed, violations = check_causal_language(text)
        assert passed is False, f"Expected causal violation for: '{text}'"
        assert len(violations) > 0


def test_full_investigation_report_verification():
    claims = [
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "case_count", 909),
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "prr", 9.18),
    ]
    citations = [
        Citation("chunk_001", "myopathy and rhabdomyolysis"),
    ]
    valid_text = (
        "Signal investigation for ATORVASTATIN / RHABDOMYOLYSIS. "
        "Disproportionality analysis indicates 909 co-occurring cases. "
        "Label warning notes 'myopathy and rhabdomyolysis'."
    )
    res = verify_investigation_report(
        claims=claims,
        signal_data=SAMPLE_SIGNAL_DATA,
        citations=citations,
        retrieved_chunks=SAMPLE_CHUNKS,
        report_text=valid_text,
    )
    assert res.passed is True
    assert res.total_claims == 2
    assert res.citation_passed is True
    assert res.causal_guard_passed is True
