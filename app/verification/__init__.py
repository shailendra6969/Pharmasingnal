"""Verification package (spec §27–32, Phase 6)."""
from app.verification.claim_verifier import (
    Citation,
    StructuredClaim,
    VerificationResult,
    check_causal_language,
    verify_citations,
    verify_investigation_report,
    verify_numeric_claims,
)

__all__ = [
    "Citation",
    "StructuredClaim",
    "VerificationResult",
    "check_causal_language",
    "verify_citations",
    "verify_investigation_report",
    "verify_numeric_claims",
]
