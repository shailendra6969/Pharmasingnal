"""Signal scores retrieval tool for pharmacovigilance investigation (spec §23–26, Phase 5).

Provides deterministic access to PRR/ROR disproportionality metrics, 2x2 contingency
tables, chi-square statistics, and quarterly exposure series.
"""
from __future__ import annotations

import math
from typing import Any

from psycopg2.extras import RealDictCursor


def get_signal_scores(
    conn,
    drug: str,
    event: str,
    mode: str = "all",
) -> dict[str, Any] | None:
    """Retrieve full disproportionality metrics and history for a drug/event pair.

    Args:
        conn: psycopg2 connection.
        drug: Canonical drug name (e.g. 'ATORVASTATIN').
        event: MedDRA adverse event term (e.g. 'RHABDOMYOLYSIS').
        mode: Screening mode ('all' or 'primary_suspect').

    Returns:
        Structured dictionary containing:
        - contingency table cells (a, b, c, d)
        - background_n, drug_case_count, event_case_count
        - prr, prr_lower, prr_upper
        - ror, ror_lower, ror_upper
        - chi_square
        - prr_screen, ror_screen
        - quarters_observed, quarters_screening
        - sensitivity_counterpart (comparison vs opposite mode)
        - quarterly_history (list of quarterly a and background_n points)
        Returns None if pair is not found in signal_scores.
    """
    drug_clean = drug.strip().upper()
    event_clean = event.strip().upper()
    primary_suspect = (mode.lower() in ("primary_suspect", "primary-suspect", "ps"))

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Fetch target mode record
        cur.execute(
            """
            SELECT signal_id, drug, event, period, a, b, c, d, case_count,
                   prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
                   chi_square, prr_screen, ror_screen, insufficient,
                   continuity_corrected, background_n, drug_case_count,
                   event_case_count, quarters_observed, quarters_screening,
                   primary_suspect_mode, created_at
            FROM signal_scores
            WHERE drug = %s AND event = %s AND period = 'all'
              AND primary_suspect_mode = %s
            LIMIT 1;
            """,
            (drug_clean, event_clean, primary_suspect),
        )
        row = cur.fetchone()
        if not row:
            return None

        result = dict(row)

        # Sanitize NaN/Inf in floats for JSON compatibility
        for k in ("prr", "prr_lower", "prr_upper", "ror", "ror_lower", "ror_upper", "chi_square"):
            v = result.get(k)
            if v is not None and (math.isnan(v) or math.isinf(v)):
                result[k] = None

        # Fetch sensitivity counterpart (opposite mode)
        counter_mode = not primary_suspect
        cur.execute(
            """
            SELECT signal_id, drug, event, period, a, b, c, d, case_count,
                   prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
                   chi_square, prr_screen, ror_screen, insufficient,
                   continuity_corrected, background_n, drug_case_count,
                   event_case_count, primary_suspect_mode
            FROM signal_scores
            WHERE drug = %s AND event = %s AND period = 'all'
              AND primary_suspect_mode = %s
            LIMIT 1;
            """,
            (drug_clean, event_clean, counter_mode),
        )
        counter_row = cur.fetchone()
        result["sensitivity_counterpart"] = dict(counter_row) if counter_row else None

        # Fetch quarterly history
        cur.execute(
            """
            SELECT quarter, a, background_n, primary_suspect_mode
            FROM signal_quarterly
            WHERE drug = %s AND event = %s AND primary_suspect_mode = %s
            ORDER BY quarter ASC;
            """,
            (drug_clean, event_clean, primary_suspect),
        )
        result["quarterly_history"] = [dict(q) for q in cur.fetchall()]

    return result
