"""Evaluation harness for the investigation pipeline (spec §8, §35).

Executes baseline evaluation cases against a live database, scores each
outcome against the case's expectation, and persists questions/results to
``eval_questions`` / ``eval_results``.

Deterministic by design: the current pipeline calls no LLM, so
``token_cost`` is recorded as 0.0 and every outcome is reproducible.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.investigation import (
    InvestigationWorkflow,
    derive_structured_claims,
)
from app.evaluation.cases import (
    BASELINE_CASES,
    EvalCase,
    SCENARIO_INCORRECT_CITATION,
    SCENARIO_MISSING_EVIDENCE,
    SCENARIO_UNSUPPORTED_CLAIM,
    VERIFIED,
)
from app.repositories.loader import transaction
from app.tools.label_tool import search_labels
from app.tools.signal_tool import get_signal_scores
from app.verification.claim_verifier import Citation, StructuredClaim, verify_investigation_report

logger = logging.getLogger(__name__)

_UPSERT_QUESTION_SQL = """
    INSERT INTO eval_questions (
        question_id, question, category, expected_answer,
        expected_drug, expected_event, expected_sources
    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (question_id) DO UPDATE SET
        question = EXCLUDED.question,
        category = EXCLUDED.category,
        expected_answer = EXCLUDED.expected_answer,
        expected_drug = EXCLUDED.expected_drug,
        expected_event = EXCLUDED.expected_event,
        expected_sources = EXCLUDED.expected_sources
"""

_INSERT_RESULT_SQL = """
    INSERT INTO eval_results (
        question_id, generated_answer, retrieved_chunks, claims,
        verification_results, retrieval_metrics, numeric_accuracy,
        citation_correctness, verification_accuracy, latency, token_cost
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@dataclass
class CaseResult:
    """Scored outcome of a single evaluation case."""

    case: EvalCase
    status: str  # actual workflow status (VERIFIED | FLAGGED_WITH_WARNINGS)
    matched_expectation: bool | None  # None => skipped (precondition unmet)
    skip_reason: str | None
    generated_answer: str
    retrieved_chunks: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    retrieval_metrics: dict[str, Any] = field(default_factory=dict)
    numeric_accuracy: float | None = None
    citation_correctness: float | None = None
    verification_accuracy: float | None = None
    latency: float = 0.0
    token_cost: float = 0.0

    @property
    def outcome(self) -> str:
        if self.matched_expectation is None:
            return "SKIP"
        return "PASS" if self.matched_expectation else "FAIL"

    def summary(self) -> dict[str, Any]:
        return {
            "question_id": self.case.question_id,
            "category": self.case.category,
            "scenario": self.case.scenario,
            "expected_status": self.case.expected_status,
            "actual_status": self.status,
            "outcome": self.outcome,
            "skip_reason": self.skip_reason,
            "numeric_accuracy": self.numeric_accuracy,
            "citation_correctness": self.citation_correctness,
            "verification_accuracy": self.verification_accuracy,
            "latency": round(self.latency, 4),
            "token_cost": self.token_cost,
            "retrieval_metrics": self.retrieval_metrics,
        }


@dataclass
class EvaluationReport:
    """Aggregate of all case results from one evaluation run."""

    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.matched_expectation is True)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.matched_expectation is False)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.matched_expectation is None)

    @property
    def all_matched(self) -> bool:
        """Skipped cases do not fail the run, but are reported separately."""
        return self.failed == 0

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "all_matched": self.all_matched,
            "cases": [r.summary() for r in self.results],
        }


def _count_label_chunks(conn, drug: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_chunks WHERE drug = %s;", (drug.strip().upper(),))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _chunks_for_verification(retrieved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"chunk_id": c.get("chunk_id"), "text": c.get("text", "")}
        for c in retrieved
        if c.get("chunk_id")
    ]


def _citations_from_report(report: dict[str, Any]) -> list[Citation]:
    return [
        Citation(chunk_id=c["chunk_id"], quoted_span=c["quoted_span"])
        for c in report.get("citations", [])
        if c.get("chunk_id")
    ]


def _verify_tampered(
    case: EvalCase,
    report: dict[str, Any],
    signal_data: dict[str, Any] | None,
    retrieved: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Re-run verification with the case's tamper applied; return (status, summary)."""
    claims = derive_structured_claims(case.expected_drug, case.expected_event, signal_data)
    citations = _citations_from_report(report)
    chunks = _chunks_for_verification(retrieved)

    if case.scenario == SCENARIO_UNSUPPORTED_CLAIM:
        metric = str(case.tamper.get("metric", "case_count"))
        value = float(case.tamper.get("claimed_value", 0.0))
        claims = [
            StructuredClaim(c.drug, c.event, metric, value) if c.metric == metric else c
            for c in claims
        ]
        if not any(c.metric == metric for c in claims):
            claims.append(StructuredClaim(case.expected_drug, case.expected_event, metric, value))

    elif case.scenario == SCENARIO_INCORRECT_CITATION:
        fabricated = str(
            case.tamper.get("quoted_span")
            or "Fabricated quotation that appears nowhere in the source label."
        )
        if citations:
            citations = [Citation(citations[0].chunk_id, fabricated), *citations[1:]]
        elif retrieved:
            citations = [Citation(retrieved[0]["chunk_id"], fabricated)]

    result = verify_investigation_report(
        claims=claims,
        signal_data=signal_data,
        citations=citations,
        retrieved_chunks=chunks,
        report_text=report.get("summary", ""),
        require_label_evidence=True,
    )
    status = VERIFIED if result.passed else "FLAGGED_WITH_WARNINGS"
    return status, result.summary()


def run_case(
    conn,
    case: EvalCase,
    *,
    persist: bool = False,
    now: Callable[[], float] = time.perf_counter,
) -> CaseResult:
    """Execute one evaluation case and score it against its expectation."""
    t0 = now()

    report = InvestigationWorkflow(conn=conn).run(
        drug=case.expected_drug, event=case.expected_event, mode=case.mode
    )
    signal_data = get_signal_scores(
        conn, drug=case.expected_drug, event=case.expected_event, mode=case.mode
    )
    retrieved = search_labels(conn, drug=case.expected_drug, query=case.expected_event, limit=5)

    status = str(report.get("status", ""))
    verification = report.get("verification") or {}
    skip_reason: str | None = None
    matched: bool | None = None

    if case.scenario == SCENARIO_MISSING_EVIDENCE:
        if _count_label_chunks(conn, case.expected_drug) > 0:
            # Precondition unmet: labels now exist for this drug, so the case
            # can no longer exercise the missing-evidence path. Reported as
            # SKIP rather than a spurious failure.
            skip_reason = (
                f"label chunks exist for {case.expected_drug}; "
                "case premise no longer holds"
            )
        else:
            matched = status == case.expected_status
    elif case.scenario in (SCENARIO_UNSUPPORTED_CLAIM, SCENARIO_INCORRECT_CITATION):
        status, verification = _verify_tampered(case, report, signal_data, retrieved)
        matched = status == case.expected_status
    else:
        matched = status == case.expected_status

    latency = now() - t0
    v_claims = verification.get("claims", {}) or {}
    total_claims = int(v_claims.get("total", 0) or 0)
    numeric_accuracy = (int(v_claims.get("passed", 0) or 0) / total_claims) if total_claims else None
    citation_passed = bool((verification.get("citations", {}) or {}).get("passed"))
    citation_correctness = 1.0 if citation_passed else 0.0
    verification_accuracy = None if matched is None else (1.0 if matched else 0.0)

    evidence = report.get("evidence", {}) or {}
    sections = evidence.get("sections_searched", []) or []
    retrieval_metrics = {
        "scenario": case.scenario,
        "chunks_retrieved": evidence.get("retrieved_chunks_count", 0),
        "citations": evidence.get("citations_count", 0),
        "sections_searched": sections,
        "expected_sources": case.expected_sources,
        "expected_sources_covered": (
            set(case.expected_sources) <= set(sections) if case.expected_sources else None
        ),
        "evidence_required": evidence.get("evidence_required"),
        "evidence_passed": (verification.get("evidence", {}) or {}).get("passed"),
    }

    result = CaseResult(
        case=case,
        status=status,
        matched_expectation=matched,
        skip_reason=skip_reason,
        generated_answer=str(report.get("summary", "")),
        retrieved_chunks=[
            {
                "chunk_id": c.get("chunk_id"),
                "section": c.get("section"),
                "subsection": c.get("subsection"),
                "score": c.get("score"),
            }
            for c in retrieved[:10]
        ],
        claims=list(v_claims.get("details", []) or []),
        verification=verification,
        retrieval_metrics=retrieval_metrics,
        numeric_accuracy=numeric_accuracy,
        citation_correctness=citation_correctness,
        verification_accuracy=verification_accuracy,
        latency=latency,
        token_cost=0.0,
    )

    logger.info(
        "eval %s [%s] -> %s (expected %s): %s",
        case.question_id, case.scenario, status, case.expected_status, result.outcome,
    )

    if persist:
        _persist(conn, result)
    return result


def _persist(conn, result: CaseResult) -> None:
    """Upsert the question and append the scored result row."""
    case = result.case
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                _UPSERT_QUESTION_SQL,
                (
                    case.question_id,
                    case.question,
                    case.category,
                    f"expected_status={case.expected_status}; {case.rationale}".strip("; "),
                    case.expected_drug,
                    case.expected_event,
                    json.dumps(case.expected_sources),
                ),
            )
            cur.execute(
                _INSERT_RESULT_SQL,
                (
                    case.question_id,
                    result.generated_answer,
                    json.dumps(result.retrieved_chunks),
                    json.dumps(result.claims),
                    json.dumps(result.verification),
                    json.dumps(result.retrieval_metrics),
                    result.numeric_accuracy,
                    result.citation_correctness,
                    result.verification_accuracy,
                    result.latency,
                    result.token_cost,
                ),
            )


def run_evaluation(
    conn,
    cases: list[EvalCase] | None = None,
    *,
    persist: bool = True,
) -> EvaluationReport:
    """Run the baseline (or supplied) case set and return the scored report."""
    active_cases = list(BASELINE_CASES if cases is None else cases)
    report = EvaluationReport(
        results=[run_case(conn, case, persist=persist) for case in active_cases]
    )
    logger.info(
        "evaluation finished: passed=%d failed=%d skipped=%d",
        report.passed, report.failed, report.skipped,
    )
    return report
