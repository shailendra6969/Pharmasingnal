"""Evaluation case definitions (spec §8, §35).

Baseline regression cases for the RAG + investigation pipeline. The
evaluation contract requires three failure categories to be covered:

- ``unsupported_claim``  — a numeric claim diverges from ``signal_scores``
- ``missing_evidence``   — a comprehensive report with no retrieved label chunks
- ``incorrect_citation`` — a quoted span that is not present in its chunk

plus one positive control that must verify end to end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Scenario identifiers
SCENARIO_STANDARD = "standard"
SCENARIO_UNSUPPORTED_CLAIM = "unsupported_claim"
SCENARIO_MISSING_EVIDENCE = "missing_evidence"
SCENARIO_INCORRECT_CITATION = "incorrect_citation"

# Expected workflow statuses
VERIFIED = "VERIFIED"
FLAGGED = "FLAGGED_WITH_WARNINGS"


@dataclass(frozen=True)
class EvalCase:
    """One evaluation question wired to a deterministic execution scenario."""

    question_id: str
    question: str
    category: str
    expected_status: str  # VERIFIED | FLAGGED_WITH_WARNINGS
    expected_drug: str
    expected_event: str
    expected_sources: list[str] = field(default_factory=list)
    scenario: str = SCENARIO_STANDARD
    mode: str = "all"
    tamper: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


BASELINE_CASES: list[EvalCase] = [
    EvalCase(
        question_id="eval_valid_atorvastatin_rhabdomyolysis",
        question=(
            "Does ATORVASTATIN / RHABDOMYOLYSIS verify end to end with "
            "disproportionality claims, retrieved label evidence and citations?"
        ),
        category="valid_signal",
        expected_status=VERIFIED,
        expected_drug="ATORVASTATIN",
        expected_event="RHABDOMYOLYSIS",
        expected_sources=[
            "WARNINGS_AND_PRECAUTIONS",
            "ADVERSE_REACTIONS",
            "DRUG_INTERACTIONS",
        ],
        scenario=SCENARIO_STANDARD,
        rationale=(
            "Positive control: numeric claims must match signal_scores, at least "
            "one chunk must be retrieved, citations must be grounded verbatim, "
            "and the causal guard must pass."
        ),
    ),
    EvalCase(
        question_id="eval_unsupported_claim_atorvastatin",
        question=(
            "Does a numeric claim that diverges from the authoritative "
            "signal_scores record fail verification?"
        ),
        category="unsupported_claim",
        expected_status=FLAGGED,
        expected_drug="ATORVASTATIN",
        expected_event="RHABDOMYOLYSIS",
        expected_sources=["WARNINGS_AND_PRECAUTIONS", "ADVERSE_REACTIONS"],
        scenario=SCENARIO_UNSUPPORTED_CLAIM,
        tamper={"metric": "case_count", "claimed_value": 123456.0},
        rationale=(
            "A hallucinated case count must be rejected against the database "
            "record even when evidence and citations are otherwise sound."
        ),
    ),
    EvalCase(
        question_id="eval_incorrect_citation_atorvastatin",
        question=(
            "Does a fabricated citation span that is absent from the retrieved "
            "chunk text fail verification?"
        ),
        category="incorrect_citation",
        expected_status=FLAGGED,
        expected_drug="ATORVASTATIN",
        expected_event="RHABDOMYOLYSIS",
        expected_sources=["WARNINGS_AND_PRECAUTIONS", "ADVERSE_REACTIONS"],
        scenario=SCENARIO_INCORRECT_CITATION,
        tamper={
            "quoted_span": (
                "This quoted sentence was invented by the agent and does not "
                "appear anywhere in the product label."
            )
        },
        rationale=(
            "Citations must be checked verbatim against retrieved source text; "
            "an invented quotation must never pass."
        ),
    ),
    EvalCase(
        question_id="eval_missing_evidence_prednisone",
        question=(
            "Can PREDNISONE / FATIGUE be marked VERIFIED when the database "
            "holds no label chunks for that drug?"
        ),
        category="missing_evidence",
        expected_status=FLAGGED,
        expected_drug="PREDNISONE",
        expected_event="FATIGUE",
        expected_sources=[],
        scenario=SCENARIO_MISSING_EVIDENCE,
        rationale=(
            "A missing source must not produce a successful verification: the "
            "signal exists, so numeric claims pass, but absent label evidence "
            "must keep the report out of VERIFIED."
        ),
    ),
]
