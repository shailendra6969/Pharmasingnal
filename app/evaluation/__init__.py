"""Evaluation package for the RAG + investigation pipeline (spec §8, §35)."""
from app.evaluation.cases import BASELINE_CASES, EvalCase
from app.evaluation.runner import (
    CaseResult,
    EvaluationReport,
    run_case,
    run_evaluation,
)

__all__ = [
    "BASELINE_CASES",
    "CaseResult",
    "EvalCase",
    "EvaluationReport",
    "run_case",
    "run_evaluation",
]
