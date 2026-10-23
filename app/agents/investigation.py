"""LangGraph-compatible Investigation Workflow for pharmacovigilance signals (spec §23–26).

Implements the single authoritative investigation workflow with two tools:
1. `get_signal_scores`
2. `search_labels`

Follows Rule 8 (no extra agents) and the verification contract:
- Deterministic numeric verification against SQL records
- Verbatim citation checking against stored SPL chunks
- Causal-language guard enforcing voluntary-reporting boundaries
- Single retry loop on verification failure
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.tools.label_tool import search_labels
from app.tools.signal_tool import get_signal_scores
from app.verification.claim_verifier import (
    Citation,
    StructuredClaim,
    VerificationResult,
    verify_investigation_report,
)

logger = logging.getLogger(__name__)

SCIENTIFIC_DISCLAIMER = (
    "Decision-support screening prototype based on public FAERS spontaneous reports. "
    "Signal detection indicates statistical disproportionality in voluntary reporting, "
    "not clinical causality, true incidence rates, or biological risk probability."
)

_SENTENCE_TERMINATORS = (". ", "! ", "? ")


def derive_structured_claims(
    drug: str,
    event: str,
    signal_data: dict[str, Any] | None,
) -> list[StructuredClaim]:
    """Build the authoritative structured claims for a drug/event pair.

    Shared by the synthesis node and the evaluation harness so both derive
    exactly the same claim set from the same database record.
    """
    if not signal_data:
        return []
    claims: list[StructuredClaim] = []
    for metric, key in (
        ("case_count", "a"),
        ("prr", "prr"),
        ("ror", "ror"),
        ("chi_square", "chi_square"),
        ("background_n", "background_n"),
    ):
        if signal_data.get(key) is not None:
            claims.append(StructuredClaim(drug, event, metric, float(signal_data[key])))
    return claims


def extract_citation_snippet(text: str, matched_terms: list[str] | None) -> str | None:
    """Return a verbatim sentence containing the first matched term.

    The quoted span always starts at a sentence boundary (never mid-clause),
    so citations read correctly in the report while staying verbatim for the
    citation checker. Falls back to the first 150 characters of the chunk when
    no complete sentence qualifies.
    """
    if not text:
        return None
    for term in matched_terms or []:
        idx = text.lower().find(term.lower())
        if idx == -1:
            continue
        start = -1
        for marker in _SENTENCE_TERMINATORS:
            pos = text.rfind(marker, 0, idx)
            if pos > start:
                start = pos
        start = 0 if start < 0 else start + 2
        end = text.find(".", idx)
        end = len(text) if end == -1 else end + 1
        candidate = text[start:end].strip().lstrip(",;:").strip()
        if 10 <= len(candidate) <= 300:
            return candidate
    stripped = text.strip()
    return stripped[:150] if stripped else None


@dataclass
class InvestigationState:
    """State passed through the investigation workflow graph."""

    drug: str
    event: str
    mode: str = "all"
    investigation_type: str = "comprehensive"  # "comprehensive" | "numeric_only"
    require_label_evidence: bool = True
    database_conn: Any = None
    signal_data: dict[str, Any] | None = None
    retrieved_chunks: list[dict[str, Any]] = field(default_factory=list)
    draft_summary: str = ""
    claims: list[StructuredClaim] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    verification: VerificationResult | None = None
    retry_count: int = 0
    final_report: dict[str, Any] | None = None
    trace: list[str] = field(default_factory=list)


class InvestigationWorkflow:
    """State graph orchestrator for adverse-event signal investigation."""

    def __init__(self, conn: Any = None) -> None:
        self.conn = conn

    def run(
        self,
        drug: str,
        event: str,
        mode: str = "all",
        conn: Any = None,
        investigation_type: str = "comprehensive",
        require_label_evidence: bool | None = None,
    ) -> dict[str, Any]:
        """Execute the investigation workflow from start to finish.

        Args:
            drug: Target canonical drug name (e.g. 'ATORVASTATIN').
            event: Adverse event MedDRA term (e.g. 'RHABDOMYOLYSIS').
            mode: Screening mode ('all' or 'primary_suspect').
            conn: Optional database connection (uses self.conn if omitted).
            investigation_type: 'comprehensive' (requires label evidence) or
                'numeric_only' (pure disproportionality metrics query).
            require_label_evidence: Explicit override for evidence requirement.
        """
        active_conn = conn or self.conn
        if active_conn is None:
            raise ValueError("A database connection is required to run the investigation workflow.")

        req_evidence = (
            require_label_evidence
            if require_label_evidence is not None
            else (investigation_type.lower() != "numeric_only")
        )

        state = InvestigationState(
            drug=drug.strip().upper(),
            event=event.strip().upper(),
            mode=mode,
            investigation_type=investigation_type,
            require_label_evidence=req_evidence,
            database_conn=active_conn,
        )

        # Execute node pipeline
        state = self._plan_node(state)
        state = self._gather_evidence_node(state)
        state = self._synthesize_node(state)
        state = self._verify_node(state)

        # Retry loop if verification fails (retry once)
        if state.verification and not state.verification.passed and state.retry_count == 0:
            state = self._retry_node(state)
            state = self._verify_node(state)

        state = self._finalize_node(state)
        return state.final_report or {}

    def _plan_node(self, state: InvestigationState) -> InvestigationState:
        state.trace.append("plan")
        logger.info(
            "Planning investigation for %s / %s (mode=%s, type=%s, req_evidence=%s)",
            state.drug,
            state.event,
            state.mode,
            state.investigation_type,
            state.require_label_evidence,
        )
        return state

    def _gather_evidence_node(
        self, state: InvestigationState, limit: int = 5
    ) -> InvestigationState:
        state.trace.append("gather_evidence")
        # 1. Retrieve disproportionality metrics via Tool 1
        state.signal_data = get_signal_scores(
            state.database_conn,
            drug=state.drug,
            event=state.event,
            mode=state.mode,
        )

        # 2. Retrieve FDA SPL label chunks via Tool 2 if label evidence is required/requested
        if state.require_label_evidence or state.investigation_type == "comprehensive":
            state.retrieved_chunks = search_labels(
                state.database_conn,
                drug=state.drug,
                query=state.event,
                limit=limit,
            )
        else:
            state.retrieved_chunks = []

        return state


    def _synthesize_node(self, state: InvestigationState) -> InvestigationState:
        state.trace.append("synthesize")

        # Formulate structured claims from authoritative signal data
        claims = derive_structured_claims(state.drug, state.event, state.signal_data)
        citations: list[Citation] = []

        # Formulate citations from top retrieved chunks
        label_mentions = []
        for chunk in state.retrieved_chunks[:2]:
            if not isinstance(chunk, dict):
                continue
            text = chunk.get("text", "")
            matched = chunk.get("matched_terms", [])
            sec = chunk.get("section", "")
            subsec = chunk.get("subsection") or ""
            c_id = chunk.get("chunk_id", "")

            # Verbatim sentence containing the matched term (sentence-aligned)
            snippet = extract_citation_snippet(text, matched)

            if snippet and c_id:
                citations.append(Citation(chunk_id=c_id, quoted_span=snippet))
                label_mentions.append(f"{sec} ({subsec}): \"{snippet}\" [Chunk {c_id}]")

        # Synthesize report text adhering strictly to non-causal language
        lines = [
            f"# Pharmacovigilance Signal Investigation: {state.drug} / {state.event}",
            "",
            "## 1. Disproportionality Evidence",
        ]
        if state.signal_data:
            sig = state.signal_data
            prr_str = f"{sig.get('prr', 0):.2f}" if sig.get("prr") is not None else "N/A"
            ror_str = f"{sig.get('ror', 0):.2f}" if sig.get("ror") is not None else "N/A"
            chi_str = f"{sig.get('chi_square', 0):.1f}" if sig.get("chi_square") is not None else "N/A"
            screened = "Yes (Elevated)" if sig.get("prr_screen") else "No"
            lines.extend([
                f"- **Co-occurring Cases ($a$):** {sig.get('a', 0):,}",
                f"- **PRR [95% CI]:** {prr_str} [{sig.get('prr_lower', 0):.2f}–{sig.get('prr_upper', 0):.2f}]",
                f"- **ROR [95% CI]:** {ror_str} [{sig.get('ror_lower', 0):.2f}–{sig.get('ror_upper', 0):.2f}]",
                f"- **Chi-Square (Yates-corrected):** {chi_str}",
                f"- **Statistical Screen Passed:** {screened}",
                f"- **Total Reporting Background ($N$):** {sig.get('background_n', 0):,}",
            ])
        else:
            lines.append("No disproportionality signal record found in database.")

        lines.extend([
            "",
            "## 2. Official FDA Product Label Evidence",
        ])
        if label_mentions:
            lines.append(f"The event '{state.event}' was identified in official labeling:")
            for m in label_mentions:
                lines.append(f"- {m}")
        elif not state.require_label_evidence:
            lines.append("Label evidence retrieval omitted for numeric-only query.")
        else:
            lines.append(f"No explicit mention of '{state.event}' found in top retrieved label sections.")

        lines.extend([
            "",
            "## 3. Epidemiological Synthesis",
            f"FAERS reports indicate statistically elevated reporting disproportionality "
            f"for {state.drug} with {state.event}. Spontaneous reporting reflects voluntary postmarket "
            f"signals and does not establish clinical causality or true incidence rates.",
        ])

        state.draft_summary = "\n".join(lines)
        state.claims = claims
        state.citations = citations
        return state

    def _verify_node(self, state: InvestigationState) -> InvestigationState:
        state.trace.append("verify")
        state.verification = verify_investigation_report(
            claims=state.claims,
            signal_data=state.signal_data,
            citations=state.citations,
            retrieved_chunks=state.retrieved_chunks,
            report_text=state.draft_summary,
            require_label_evidence=state.require_label_evidence,
        )
        logger.info(
            "Verification result: passed=%s (claims=%d/%d, evidence=%s, citations=%s, causal=%s)",
            state.verification.passed,
            state.verification.passed_claims,
            state.verification.total_claims,
            state.verification.evidence_passed,
            state.verification.citation_passed,
            state.verification.causal_guard_passed,
        )
        return state

    def _retry_node(self, state: InvestigationState) -> InvestigationState:
        state.trace.append("retry")
        state.retry_count += 1
        logger.warning(
            "Verification failed on attempt 1. Repairing draft (retry %d)...",
            state.retry_count,
        )
        # Re-gather evidence with a wider retrieval window so the retry works
        # from fresh, broader label context instead of the identical inputs,
        # then re-run synthesis and verification against it.
        state = self._gather_evidence_node(state, limit=10)
        return self._synthesize_node(state)

    def _finalize_node(self, state: InvestigationState) -> InvestigationState:
        state.trace.append("finalize")
        v = state.verification
        status = "VERIFIED" if (v and v.passed) else "FLAGGED_WITH_WARNINGS"

        state.final_report = {
            "drug": state.drug,
            "event": state.event,
            "mode": state.mode,
            "investigation_type": state.investigation_type,
            "status": status,
            "metrics": {
                "case_count": state.signal_data.get("a") if state.signal_data else None,
                "prr": state.signal_data.get("prr") if state.signal_data else None,
                "ror": state.signal_data.get("ror") if state.signal_data else None,
                "chi_square": state.signal_data.get("chi_square") if state.signal_data else None,
                "screened": bool(state.signal_data.get("prr_screen")) if state.signal_data else False,
            },
            "evidence": {
                "retrieved_chunks_count": len(state.retrieved_chunks),
                "citations_count": len(state.citations),
                "sections_searched": list({
                    c.get("section") or "UNKNOWN"
                    for c in state.retrieved_chunks
                    if isinstance(c, dict)
                }),
                "evidence_required": state.require_label_evidence,
                "evidence_passed": v.evidence_passed if v else False,
            },
            "summary": state.draft_summary,
            "citations": [
                {"chunk_id": c.chunk_id, "quoted_span": c.quoted_span}
                for c in state.citations
            ],
            "verification": v.summary() if v else None,
            "scientific_disclaimer": SCIENTIFIC_DISCLAIMER,
            "trace": state.trace,
        }
        return state

