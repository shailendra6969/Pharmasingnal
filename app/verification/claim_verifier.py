"""Deterministic claim verifier, citation checker, and causal guard (spec §27–32, Phase 6).

Core safety layer for agentic investigation reports:
1. Numeric verification: Compares structured claims directly against authoritative
   database values (exact match for counts, ±0.05 for statistical ratios).
2. Citation verification: Verifies quoted spans against retrieved label chunks.
3. Causal language guard: Rejects unsupported causal assertions on voluntary FAERS data.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

# Prohibited causal certainty phrases (voluntary spontaneous reporting cannot prove causality)
PROHIBITED_CAUSAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(proves?|proven)\s+(that\s+)?(\w+\s+)?causes?\b", re.I),
    re.compile(r"\b(proves?|proven)\s+causality\b", re.I),
    re.compile(r"\bdefinitive(ly)?\s+causes?\b", re.I),
    re.compile(r"\bclinical(ly)?\s+proven\s+cause\b", re.I),
    re.compile(r"\bestablish(es|ed|ing)?\s+(a\s+)?causal\b", re.I),
    re.compile(r"\bestablish(es|ed|ing)?\s+causation\b", re.I),
    re.compile(r"\bconclusive\s+proof\s+of\s+harm\b", re.I),
    re.compile(r"\bproves?\s+(\w+\s+)?is\s+toxic\b", re.I),
]


@dataclass(frozen=True)
class StructuredClaim:
    """Represents a specific, verifiable metric assertion made by an agent."""

    drug: str
    event: str
    metric: str  # 'case_count' (a), 'b', 'c', 'd', 'prr', 'ror', 'chi_square', etc.
    claimed_value: float
    period: str = "all"


@dataclass(frozen=True)
class Citation:
    """Represents a quoted text snippet linked to a specific doc_chunk."""

    chunk_id: str
    quoted_span: str


@dataclass
class VerificationResult:
    """Result of comprehensive investigation report verification."""

    passed: bool
    total_claims: int = 0
    passed_claims: int = 0
    failed_claims: int = 0
    claim_details: list[dict[str, Any]] = field(default_factory=list)
    citation_passed: bool = True
    citation_errors: list[str] = field(default_factory=list)
    causal_guard_passed: bool = True
    causal_violations: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "claims": {
                "total": self.total_claims,
                "passed": self.passed_claims,
                "failed": self.failed_claims,
                "details": self.claim_details,
            },
            "citations": {
                "passed": self.citation_passed,
                "errors": self.citation_errors,
            },
            "causal_guard": {
                "passed": self.causal_guard_passed,
                "violations": self.causal_violations,
            },
        }


def _normalize_span(text: str) -> str:
    """Normalize whitespace for fuzzy span matching."""
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_numeric_claims(
    claims: list[StructuredClaim],
    signal_data: dict[str, Any] | None,
) -> tuple[bool, int, int, list[dict[str, Any]]]:
    """Verify structured claims against authoritative database signal metrics."""
    if not claims:
        return True, 0, 0, []

    if signal_data is None:
        details = [
            {
                "drug": c.drug,
                "event": c.event,
                "metric": c.metric,
                "claimed": c.claimed_value,
                "expected": None,
                "passed": False,
                "reason": "Signal not found in authoritative database",
            }
            for c in claims
        ]
        return False, 0, len(claims), details

    passed_count = 0
    details: list[dict[str, Any]] = []

    # Map alias metric names
    metric_map = {
        "case_count": "a",
        "cases": "a",
        "co_occurrences": "a",
        "prr_lb": "prr_lower",
        "prr_ub": "prr_upper",
        "ror_lb": "ror_lower",
        "ror_ub": "ror_upper",
        "chi2": "chi_square",
    }

    for c in claims:
        m_key = metric_map.get(c.metric.lower(), c.metric.lower())
        expected = signal_data.get(m_key)

        if expected is None:
            details.append({
                "metric": c.metric,
                "claimed": c.claimed_value,
                "expected": None,
                "passed": False,
                "reason": f"Metric '{c.metric}' not present in signal data",
            })
            continue

        try:
            exp_val = float(expected)
        except (ValueError, TypeError):
            details.append({
                "metric": c.metric,
                "claimed": c.claimed_value,
                "expected": expected,
                "passed": False,
                "reason": f"Expected value '{expected}' is non-numeric",
            })
            continue

        # Exact match for counts; tolerance ±0.05 (or 1% relative) for statistical ratios
        is_count = m_key in ("a", "b", "c", "d", "background_n", "drug_case_count", "event_case_count")
        if is_count:
            passed = abs(c.claimed_value - exp_val) < 1e-4
            tol = 0.0
        else:
            abs_diff = abs(c.claimed_value - exp_val)
            rel_diff = abs_diff / abs(exp_val) if exp_val != 0 else abs_diff
            passed = (abs_diff <= 0.05) or (rel_diff <= 0.01)
            tol = 0.05

        if passed:
            passed_count += 1
            details.append({
                "metric": c.metric,
                "claimed": c.claimed_value,
                "expected": exp_val,
                "passed": True,
            })
        else:
            details.append({
                "metric": c.metric,
                "claimed": c.claimed_value,
                "expected": exp_val,
                "diff": round(abs(c.claimed_value - exp_val), 4),
                "tolerance": tol,
                "passed": False,
                "reason": f"Claimed {c.claimed_value} diverges from authoritative {exp_val} (tol: {tol})",
            })

    all_passed = (passed_count == len(claims))
    return all_passed, passed_count, len(claims) - passed_count, details


def verify_citations(
    citations: list[Citation],
    retrieved_chunks: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Verify that cited quotation spans exist verbatim in retrieved chunks."""
    if not citations:
        return True, []

    chunk_map = {c["chunk_id"]: c.get("text", "") for c in retrieved_chunks}
    errors: list[str] = []

    for cit in citations:
        chunk_text = chunk_map.get(cit.chunk_id)
        if not chunk_text:
            errors.append(f"Cited chunk_id '{cit.chunk_id}' was not found in retrieved chunks")
            continue

        span_norm = _normalize_span(cit.quoted_span)
        if not span_norm:
            errors.append(f"Citation for '{cit.chunk_id}' carries empty quotation span")
            continue

        text_norm = _normalize_span(chunk_text)
        if span_norm not in text_norm:
            errors.append(
                f"Quoted span '{cit.quoted_span[:40]}...' was not found in text of chunk '{cit.chunk_id}'"
            )

    return (len(errors) == 0), errors


def check_causal_language(text: str) -> tuple[bool, list[str]]:
    """Audit prose for prohibited causal assertions; flag violations."""
    violations: list[str] = []
    for pattern in PROHIBITED_CAUSAL_PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(f"Prohibited causal assertion: '{match.group(0)}'")

    return (len(violations) == 0), violations


def verify_investigation_report(
    claims: list[StructuredClaim],
    signal_data: dict[str, Any] | None,
    citations: list[Citation],
    retrieved_chunks: list[dict[str, Any]],
    report_text: str,
) -> VerificationResult:
    """Run full deterministic battery: numeric claims + citation spans + causal guard."""
    num_passed, p_cnt, f_cnt, details = verify_numeric_claims(claims, signal_data)
    cit_passed, cit_errs = verify_citations(citations, retrieved_chunks)
    causal_passed, causal_errs = check_causal_language(report_text)

    overall_passed = num_passed and cit_passed and causal_passed

    return VerificationResult(
        passed=overall_passed,
        total_claims=len(claims),
        passed_claims=p_cnt,
        failed_claims=f_cnt,
        claim_details=details,
        citation_passed=cit_passed,
        citation_errors=cit_errs,
        causal_guard_passed=causal_passed,
        causal_violations=causal_errs,
    )
