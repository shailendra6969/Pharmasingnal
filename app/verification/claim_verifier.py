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

# Prohibited causal certainty phrases and direct assertions
# Voluntary spontaneous reporting cannot establish direct clinical causality.
PROHIBITED_CAUSAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(proves?|proven)\s+(that\s+)?(\w+\s+)?causes?\b", re.I),
    re.compile(r"\b(proves?|proven)\s+causality\b", re.I),
    re.compile(r"\bproves?\s+that\b", re.I),
    re.compile(r"\bproves?\s+(\w+\s+)?is\s+(harmful|toxic|dangerous|safe)\b", re.I),
    re.compile(r"\bdefinitive(ly)?\s+causes?\b", re.I),
    re.compile(r"\bclinical(ly)?\s+proven\s+cause\b", re.I),
    re.compile(r"\bestablish(es|ed|ing)?\s+that\b", re.I),
    re.compile(r"\bestablish(es|ed|ing)?\s+(a\s+)?causal\b", re.I),
    re.compile(r"\bestablish(es|ed|ing)?\s+causation\b", re.I),
    re.compile(r"\bconclusive\s+proof\b", re.I),
    re.compile(r"\bconclusive\s+evidence\s+that\b", re.I),
]

_PASSIVE_CAUSAL_PATTERN = re.compile(r"\b(is|are|was|were|been|being)\s+caused\s+by\b", re.I)
_ACTIVE_CAUSAL_PATTERN = re.compile(r"\b(\w+)\s+(causes?|caused)\s+(\w+)\b", re.I)
_MODAL_AUXILIARIES = {"may", "might", "can", "could", "would", "should"}
_NON_CAUSAL_FOLLOWING = {
    "of", "for", "to", "in",
    "is", "are", "was", "were", "be", "been",
    "include", "includes", "included",
    "remains", "remained", "seem", "seems", "seemed", "appear", "appears", "appeared",
    "exist", "exists", "existed",
}
_DETERMINERS_PRE = {
    "the", "a", "an", "this", "that", "these", "those",
    "common", "major", "underlying", "root", "primary", "probable", "possible",
}
_NEGATION_PREFIX_PATTERN = re.compile(
    r"\b(not|never|cannot|can't|does\s+not|doesn't|did\s+not|didn't|do\s+not|don't|fails?\s+to|failed\s+to|unlikely\s+to|without|no\s+evidence\s+that|unknown\s+whether|whether)\b",
    re.I,
)


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
    evidence_passed: bool = True
    evidence_errors: list[str] = field(default_factory=list)
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
            "evidence": {
                "passed": self.evidence_passed,
                "errors": self.evidence_errors,
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
    require_claims: bool = True,
) -> tuple[bool, int, int, list[dict[str, Any]]]:
    """Verify structured claims against authoritative database signal metrics."""
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
        ] if claims else [
            {
                "metric": "signal_data",
                "claimed": None,
                "expected": "authoritative signal record",
                "passed": False,
                "reason": "Signal not found in authoritative database",
            }
        ]
        return False, 0, len(details), details

    if not claims:
        if require_claims:
            details = [
                {
                    "metric": "claims",
                    "claimed": 0,
                    "expected": ">=1 structured claim",
                    "passed": False,
                    "reason": "No structured claims provided to verify against signal data",
                }
            ]
            return False, 0, 1, details
        return True, 0, 0, []

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
    citations: list[Citation] | None,
    retrieved_chunks: list[dict[str, Any]] | None,
    require_citations: bool = False,
) -> tuple[bool, list[str]]:
    """Verify that cited quotation spans exist verbatim in retrieved chunks."""
    c_list = citations or []
    chunks_list = retrieved_chunks or []
    errors: list[str] = []

    if not c_list:
        if require_citations:
            errors.append("No citations provided: investigation requires grounded label citations.")
            return False, errors
        return True, []

    chunk_map = {
        c["chunk_id"]: c.get("text", "")
        for c in chunks_list
        if isinstance(c, dict) and "chunk_id" in c
    }

    for cit in c_list:
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
    """Audit prose for prohibited causal assertions; flag violations.

    Detects both compound phrases (e.g., 'definitively causes', 'proves that')
    and direct causal assertions (e.g., 'Drug causes Event', 'Event is caused by Drug'),
    while allowing safe negated clauses, modal expressions ('may cause', 'may potentially cause'),
    infinitives ('to cause'), and non-causal noun phrases ('the cause is', 'causes of', 'causes include').
    """
    violations: list[str] = []
    # Split text into sentence segments
    sentences = re.split(r"[.!?;\n]+", text)

    for sent in sentences:
        s_clean = sent.strip()
        if not s_clean:
            continue

        # 1. Check certainty and proof patterns
        for pat in PROHIBITED_CAUSAL_PATTERNS:
            for match in pat.finditer(s_clean):
                pre_text = s_clean[:match.start()]
                clause_pre = pre_text.split(",")[-1]
                if not _NEGATION_PREFIX_PATTERN.search(clause_pre):
                    violations.append(f"Prohibited causal assertion: '{match.group(0)}'")

        # 2. Check passive causation: '(is|are|was|were|been|being) caused by'
        for match in _PASSIVE_CAUSAL_PATTERN.finditer(s_clean):
            pre_text = s_clean[:match.start()]
            clause_pre = pre_text.split(",")[-1]
            if not _NEGATION_PREFIX_PATTERN.search(clause_pre):
                violations.append(f"Prohibited causal assertion: '{match.group(0)}'")

        # 3. Check active causation: '[subject] causes/caused [object]'
        for match in _ACTIVE_CAUSAL_PATTERN.finditer(s_clean):
            subj = match.group(1).lower()
            obj = match.group(3).lower()

            pre_text = s_clean[:match.start()]
            clause_pre = pre_text.split(",")[-1].strip()
            pre_words = clause_pre.split()

            # Skip noun phrases: preceded by determiner or subj is determiner ('the cause is', 'common causes include')
            if subj in _DETERMINERS_PRE or (pre_words and pre_words[-1].lower() in _DETERMINERS_PRE and obj in _NON_CAUSAL_FOLLOWING):
                continue

            # Skip noun usages where following token is a linking verb, preposition, or noun complement
            if obj in _NON_CAUSAL_FOLLOWING:
                continue

            # Skip infinitive 'to cause'
            if subj == "to":
                continue

            # Skip modal verbs preceding cause ('may cause', 'can cause')
            if subj in _MODAL_AUXILIARIES:
                continue

            # Look back up to 3 words before the verb for modals (e.g. 'may potentially cause', 'can also cause')
            words_before_verb = [w.lower() for w in pre_words[-3:]] if pre_words else []
            if any(w in _MODAL_AUXILIARIES for w in words_before_verb):
                continue

            # Check negation in the current clause
            if _NEGATION_PREFIX_PATTERN.search(clause_pre) or subj in {"not", "never", "cannot"}:
                continue

            match_str = match.group(0)
            if any(match_str in v for v in violations):
                continue

            violations.append(f"Prohibited causal assertion: '{match_str}'")

    deduped = list(dict.fromkeys(violations))
    return (len(deduped) == 0), deduped


def verify_investigation_report(
    claims: list[StructuredClaim],
    signal_data: dict[str, Any] | None,
    citations: list[Citation] | None,
    retrieved_chunks: list[dict[str, Any]] | None,
    report_text: str,
    require_label_evidence: bool = True,
    require_claims: bool = True,
) -> VerificationResult:
    """Run full deterministic battery: numeric claims + label evidence + citation spans + causal guard.

    Args:
        claims: Structured metric claims.
        signal_data: Authoritative signal database record.
        citations: Cited quotation spans from label chunks.
        retrieved_chunks: Retrieved document chunks from doc_chunks.
        report_text: Complete synthesized markdown/prose report text.
        require_label_evidence: If True (default for comprehensive investigations),
            requires that retrieved_chunks is non-empty and at least one valid citation
            is supplied and grounded. If False (e.g. numeric_only investigations),
            empty evidence/citations are permitted unless invalid citations are supplied.
        require_claims: If True (default), requires at least one structured claim
            and valid signal_data.
    """
    # 1. Numeric claims verification
    num_passed, p_cnt, f_cnt, details = verify_numeric_claims(
        claims, signal_data, require_claims=require_claims
    )

    # 2. Evidence presence check
    chunks_list = [c for c in (retrieved_chunks or []) if isinstance(c, dict)]
    citations_list = citations or []
    evidence_errors: list[str] = []

    if require_label_evidence:
        if not chunks_list:
            evidence_errors.append("No label evidence retrieved: retrieved_chunks is empty or unpopulated.")
        if not citations_list:
            evidence_errors.append("No label citations provided: investigation requires grounded label citations.")

    evidence_passed = (len(evidence_errors) == 0)

    # 3. Citation span and grounding verification
    cit_passed, cit_errs = verify_citations(
        citations_list,
        chunks_list,
        require_citations=require_label_evidence,
    )

    # 4. Causal language guard
    causal_passed, causal_errs = check_causal_language(report_text)

    # Overall outcome: ALL must pass
    overall_passed = num_passed and evidence_passed and cit_passed and causal_passed

    return VerificationResult(
        passed=overall_passed,
        total_claims=len(claims),
        passed_claims=p_cnt,
        failed_claims=f_cnt,
        claim_details=details,
        evidence_passed=evidence_passed,
        evidence_errors=evidence_errors,
        citation_passed=cit_passed,
        citation_errors=cit_errs,
        causal_guard_passed=causal_passed,
        causal_violations=causal_errs,
    )


