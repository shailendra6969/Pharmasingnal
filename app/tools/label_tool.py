"""FDA Structured Product Label retrieval tool (spec §18–26, Phase 4–5).

Performs plain-text, section-aware keyword retrieval over `doc_chunks` with:
1. Section-aware weighting (Boxed Warning, Adverse Reactions, Warnings).
2. Cross-section fallback for OTC/Drug Facts formulations (e.g. Ibuprofen/Advil)
   where adverse events are recorded under Warnings rather than formal PLR Adverse Reactions.
3. Strict provenance preservation: chunk_id, section, subsection, source URL, date.
"""
from __future__ import annotations

import json
import re
from typing import Any

from psycopg2.extras import RealDictCursor

# Section relevance weights for pharmacovigilance query boosting
SECTION_WEIGHTS: dict[str, float] = {
    "BOXED_WARNING": 2.5,
    "ADVERSE_REACTIONS": 2.0,
    "WARNINGS_AND_PRECAUTIONS": 1.8,
    "CONTRAINDICATIONS": 1.5,
    "INDICATIONS_AND_USAGE": 1.2,
    "DRUG_INTERACTIONS": 1.2,
    "CLINICAL_PHARMACOLOGY": 1.0,
    "OVERDOSAGE": 1.0,
    "DESCRIPTION": 0.8,
    "OTHER": 0.5,
}

# Fallback search sections for OTC/Drug Facts products lacking formal ADVERSE_REACTIONS
FALLBACK_SECTIONS: list[str] = [
    "WARNINGS_AND_PRECAUTIONS",
    "BOXED_WARNING",
    "CONTRAINDICATIONS",
]


def _tokenize(text: str) -> set[str]:
    """Extract lowercased alphanumeric tokens of length >= 3."""
    return {w.lower() for w in re.findall(r"\b[A-Za-z0-9]{3,}\b", text)}


def _score_chunk(chunk_text: str, section: str, query_tokens: set[str]) -> tuple[float, list[str]]:
    """Compute weighted relevance score and list of matched terms."""
    if not query_tokens:
        return 0.0, []

    text_lower = chunk_text.lower()
    matched = [t for t in query_tokens if t in text_lower]
    if not matched:
        return 0.0, []

    # Frequency and coverage score
    raw_hits = sum(text_lower.count(t) for t in matched)
    coverage = len(matched) / len(query_tokens)
    weight = SECTION_WEIGHTS.get(section, 1.0)
    score = (raw_hits * 0.5 + coverage * 2.0) * weight
    return round(score, 3), matched


def search_labels(
    conn,
    drug: str,
    query: str,
    section: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search doc_chunks for a drug with section weighting and OTC fallback.

    Args:
        conn: psycopg2 connection.
        drug: Canonical drug name (e.g. 'ATORVASTATIN', 'IBUPROFEN').
        query: Query string or event term (e.g. 'RHABDOMYOLYSIS', 'GASTRIC ULCER').
        section: Optional standard section filter (e.g. 'ADVERSE_REACTIONS').
        limit: Maximum number of ranked chunks to return.

    Returns:
        List of ranked chunk dictionaries with relevance score, text, and metadata.
    """
    drug_clean = drug.strip().upper()
    query_tokens = _tokenize(query)
    limit = max(1, min(20, int(limit)))

    def _query_db(target_section: str | None) -> list[dict[str, Any]]:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if target_section:
                cur.execute(
                    """
                    SELECT chunk_id, drug, doc_type, section, subsection,
                           text, source_url, document_date, chunk_index, metadata
                    FROM doc_chunks
                    WHERE drug = %s AND section = %s;
                    """,
                    (drug_clean, target_section),
                )
            else:
                cur.execute(
                    """
                    SELECT chunk_id, drug, doc_type, section, subsection,
                           text, source_url, document_date, chunk_index, metadata
                    FROM doc_chunks
                    WHERE drug = %s;
                    """,
                    (drug_clean,),
                )
            return [dict(r) for r in cur.fetchall()]

    candidates = _query_db(section)

    # OTC / Missing section fallback:
    # If a specific section like ADVERSE_REACTIONS was requested but returned 0 chunks,
    # automatically expand search to WARNINGS_AND_PRECAUTIONS and BOXED_WARNING.
    fallback_used = False
    if not candidates and section in ("ADVERSE_REACTIONS", "BOXED_WARNING"):
        for alt_sec in FALLBACK_SECTIONS:
            alt_candidates = _query_db(alt_sec)
            if alt_candidates:
                candidates.extend(alt_candidates)
                fallback_used = True

    if not candidates:
        return []

    # Score and rank candidates
    scored: list[dict[str, Any]] = []
    for c in candidates:
        text = c.get("text", "")
        sec = c.get("section", "OTHER")
        score, matched = _score_chunk(text, sec, query_tokens)
        if score > 0 or not query_tokens:
            meta = c.get("metadata")
            meta_dict = meta if isinstance(meta, dict) else json.loads(meta or "{}")
            scored.append({
                "chunk_id": c["chunk_id"],
                "drug": c["drug"],
                "section": c["section"],
                "subsection": c["subsection"],
                "text": text,
                "source_url": c.get("source_url"),
                "document_date": c.get("document_date"),
                "score": score,
                "matched_terms": matched,
                "metadata": meta_dict,
                "fallback_searched": fallback_used,
            })

    # Sort descending by score, then ascending by chunk_index
    scored.sort(key=lambda x: (x["score"], -x.get("metadata", {}).get("chunk_index", 0)), reverse=True)
    return scored[:limit]
