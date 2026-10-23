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


def test_causal_language_guard_rejects_direct_assertions_and_variants():
    direct_assertions = [
        "Ibuprofen causes gastric ulcers.",
        "Gastric ulcers are caused by ibuprofen.",
        "The data does not prove causation, but ibuprofen causes gastric ulcers.",
        "The evidence proves that ibuprofen is harmful.",
        "This establishes that the drug causes the event.",
        "Adverse event reports prove that the drug is harmful.",
        "Data proves that metformin is toxic.",
        "Ibuprofen caused acute kidney injury.",
    ]
    for text in direct_assertions:
        passed, violations = check_causal_language(text)
        assert passed is False, f"Expected violation for direct assertion: '{text}'"
        assert len(violations) > 0


def test_causal_language_guard_allows_safe_negations_and_modals():
    safe_texts = [
        "The cause is unknown.",
        "Common causes include stress.",
        "The potential to cause reactions exists.",
        "It is unlikely to cause adverse events.",
        "The drug may potentially cause gastric ulcers.",
        "The drug can also cause reactions.",
        "Spontaneous reporting does not establish clinical causality or true incidence rates.",
        "The causes of gastric ulcer are multifactorial.",
        "LIPITOR may cause myopathy and rhabdomyolysis in treated patients.",
        "Ibuprofen can cause severe stomach bleeding.",
        "FAERS data does not prove that ibuprofen causes gastric ulcer.",
        "The event was not caused by the medication.",
        "Statistical association does not establish that the drug causes the reaction.",
        "Investigation of potential causes of adverse events in postmarket surveillance.",
        "The observed disproportionality is a cause for further epidemiological study.",
        "It is unknown whether the drug causes this effect.",
        "Reports do not prove causality.",
        "Treatment might cause minor dizziness.",
    ]
    for text in safe_texts:
        passed, violations = check_causal_language(text)
        assert passed is True, f"Incorrectly flagged safe text: '{text}' (violations: {violations})"
        assert violations == []


def test_verify_numeric_claims_fails_when_signal_data_is_none():
    # Both with claims and with empty claims, missing signal data must fail
    passed_with_claims, _, f_cnt1, details1 = verify_numeric_claims(
        [StructuredClaim("UNKNOWN", "EVENT", "prr", 2.0)], None
    )
    assert passed_with_claims is False
    assert f_cnt1 == 1
    assert "Signal not found" in details1[0]["reason"]

    passed_empty, _, f_cnt2, details2 = verify_numeric_claims([], None)
    assert passed_empty is False
    assert f_cnt2 == 1
    assert "Signal not found" in details2[0]["reason"]


def test_verify_numeric_claims_empty_claims_requirement():
    # When required, empty claims must fail
    passed_req, _, f_cnt, details = verify_numeric_claims([], SAMPLE_SIGNAL_DATA, require_claims=True)
    assert passed_req is False
    assert f_cnt == 1
    assert "No structured claims provided" in details[0]["reason"]

    # When not required, empty claims pass
    passed_opt, p_cnt, f_cnt_opt, _ = verify_numeric_claims([], SAMPLE_SIGNAL_DATA, require_claims=False)
    assert passed_opt is True
    assert p_cnt == 0
    assert f_cnt_opt == 0


def test_verify_citations_empty_citations_behavior():
    # When required, empty citations must fail
    passed_req, errors_req = verify_citations([], SAMPLE_CHUNKS, require_citations=True)
    assert passed_req is False
    assert any("No citations provided" in e for e in errors_req)

    # When not required, empty citations pass
    passed_opt, errors_opt = verify_citations([], SAMPLE_CHUNKS, require_citations=False)
    assert passed_opt is True
    assert errors_opt == []


def test_verify_citations_handles_none_and_malformed_chunks_safely():
    malformed_chunks = [
        None,
        "not a dict",
        {"chunk_id": "chunk_001", "text": "myopathy and rhabdomyolysis"},
        {"no_chunk_id": "bad"},
    ]
    citations = [
        Citation("chunk_001", "myopathy and rhabdomyolysis"),
    ]
    # Should not raise TypeError and successfully match the valid chunk
    passed, errors = verify_citations(citations, malformed_chunks)
    assert passed is True
    assert errors == []


def test_full_investigation_report_verification_success():
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
        require_label_evidence=True,
    )
    assert res.passed is True
    assert res.total_claims == 2
    assert res.evidence_passed is True
    assert res.citation_passed is True
    assert res.causal_guard_passed is True


def test_investigation_report_fails_when_evidence_is_absent():
    claims = [
        StructuredClaim("IBUPROFEN", "GASTRIC ULCER", "case_count", 909),
    ]
    # No chunks, no citations
    res = verify_investigation_report(
        claims=claims,
        signal_data=SAMPLE_SIGNAL_DATA,
        citations=[],
        retrieved_chunks=[],
        report_text="Signal investigation for IBUPROFEN / GASTRIC ULCER.",
        require_label_evidence=True,
    )
    assert res.passed is False
    assert res.evidence_passed is False
    assert any("No label evidence retrieved" in e for e in res.evidence_errors)
    assert any("No label citations provided" in e for e in res.evidence_errors)


def test_investigation_report_fails_when_signal_data_is_none():
    citations = [
        Citation("chunk_001", "myopathy and rhabdomyolysis"),
    ]
    # Missing signal data (unknown drug/event pair)
    res = verify_investigation_report(
        claims=[],
        signal_data=None,
        citations=citations,
        retrieved_chunks=SAMPLE_CHUNKS,
        report_text="Signal investigation for UNKNOWN / PAIR.",
        require_label_evidence=True,
    )
    assert res.passed is False
    assert res.claim_details[0]["passed"] is False
    assert "Signal not found" in res.claim_details[0]["reason"]


def test_investigation_report_passes_for_numeric_only_without_label_evidence():
    claims = [
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "case_count", 909),
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "prr", 9.18),
    ]
    res = verify_investigation_report(
        claims=claims,
        signal_data=SAMPLE_SIGNAL_DATA,
        citations=[],
        retrieved_chunks=[],
        report_text="Pure numerical signal summary for ATORVASTATIN / RHABDOMYOLYSIS.",
        require_label_evidence=False,
    )
    assert res.passed is True
    assert res.evidence_passed is True
    assert res.citation_passed is True
    assert res.causal_guard_passed is True


def test_investigation_report_fails_on_causal_assertion():
    claims = [
        StructuredClaim("ATORVASTATIN", "RHABDOMYOLYSIS", "case_count", 909),
    ]
    citations = [
        Citation("chunk_001", "myopathy and rhabdomyolysis"),
    ]
    causal_text = (
        "Signal report for ATORVASTATIN / RHABDOMYOLYSIS. "
        "Atorvastatin causes rhabdomyolysis in treated patients."
    )
    res = verify_investigation_report(
        claims=claims,
        signal_data=SAMPLE_SIGNAL_DATA,
        citations=citations,
        retrieved_chunks=SAMPLE_CHUNKS,
        report_text=causal_text,
        require_label_evidence=True,
    )
    assert res.passed is False
    assert res.causal_guard_passed is False
    assert any("causes rhabdomyolysis" in v for v in res.causal_violations)


