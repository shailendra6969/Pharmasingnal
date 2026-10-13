"""Signal repository (spec §14–17, §46: repositories layer).

Handles database access for signal discovery, disproportionality metrics,
drug/event profiles, and dashboard statistics.
"""
from __future__ import annotations

import math
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor


def connect(database_url: str):
    return psycopg2.connect(database_url, connect_timeout=10)


def list_signals(
    conn,
    *,
    drug: str | None = None,
    event: str | None = None,
    mode: str = "all",
    screened_only: bool = False,
    prr_screen: bool | None = None,
    ror_screen: bool | None = None,
    min_cases: int = 3,
    sort_by: str = "a",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    """Search and paginate disproportionality signals from signal_scores."""
    primary_suspect = (mode == "primary_suspect")
    where_clauses = [
        "period = 'all'",
        "primary_suspect_mode = %s",
        "a >= %s",
    ]
    params: list[Any] = [primary_suspect, max(1, int(min_cases))]

    if drug:
        drug_clean = drug.strip().upper()
        if "%" in drug_clean:
            where_clauses.append("drug LIKE %s")
            params.append(drug_clean)
        else:
            where_clauses.append("drug ILIKE %s")
            params.append(f"%{drug_clean}%")

    if event:
        event_clean = event.strip().upper()
        if "%" in event_clean:
            where_clauses.append("event LIKE %s")
            params.append(event_clean)
        else:
            where_clauses.append("event ILIKE %s")
            params.append(f"%{event_clean}%")

    if screened_only:
        where_clauses.append("(prr_screen OR ror_screen)")
    else:
        if prr_screen is not None:
            where_clauses.append("prr_screen = %s")
            params.append(bool(prr_screen))
        if ror_screen is not None:
            where_clauses.append("ror_screen = %s")
            params.append(bool(ror_screen))

    where_sql = " AND ".join(where_clauses)

    valid_sorts = {
        "a": "a",
        "case_count": "a",
        "prr": "COALESCE(prr, 0)",
        "ror": "COALESCE(ror, 0)",
        "chi_square": "COALESCE(chi_square, 0)",
        "drug": "drug",
        "event": "event",
    }
    col = valid_sorts.get(sort_by.lower(), "a")
    dir_sql = "ASC" if order.lower() == "asc" else "DESC"
    order_sql = f"{col} {dir_sql}, signal_id {dir_sql}"

    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    offset = (page - 1) * page_size

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Total count for pagination
        cur.execute(f"SELECT COUNT(*) AS total FROM signal_scores WHERE {where_sql}", tuple(params))
        total = cur.fetchone()["total"]

        # Page records
        cur.execute(
            f"""
            SELECT signal_id, drug, event, period, a, b, c, d, case_count,
                   prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
                   chi_square, prr_screen, ror_screen, insufficient,
                   continuity_corrected, background_n, drug_case_count,
                   event_case_count, quarters_observed, quarters_screening,
                   primary_suspect_mode
            FROM signal_scores
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT %s OFFSET %s
            """,
            tuple(params + [page_size, offset]),
        )
        items = [dict(row) for row in cur.fetchall()]

    total_pages = math.ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


def get_signal_by_id(conn, signal_id: int) -> dict[str, Any] | None:
    """Fetch complete signal details, sensitivity counterpart, and quarterly series."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT signal_id, drug, event, period, a, b, c, d, case_count,
                   prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
                   chi_square, prr_screen, ror_screen, insufficient,
                   continuity_corrected, background_n, drug_case_count,
                   event_case_count, quarters_observed, quarters_screening,
                   primary_suspect_mode, created_at
            FROM signal_scores
            WHERE signal_id = %s
            """,
            (signal_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        res = dict(row)

        # Look up sensitivity counterpart (all vs primary_suspect)
        counter_mode = not res["primary_suspect_mode"]
        cur.execute(
            """
            SELECT signal_id, drug, event, period, a, b, c, d, case_count,
                   prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
                   chi_square, prr_screen, ror_screen, insufficient,
                   continuity_corrected, background_n, drug_case_count,
                   event_case_count, primary_suspect_mode
            FROM signal_scores
            WHERE drug = %s AND event = %s AND period = %s
              AND primary_suspect_mode = %s
            LIMIT 1
            """,
            (res["drug"], res["event"], res["period"], counter_mode),
        )
        counter_row = cur.fetchone()
        res["sensitivity_counterpart"] = dict(counter_row) if counter_row else None

        # Fetch quarterly history from signal_quarterly
        cur.execute(
            """
            SELECT quarter, a, background_n, primary_suspect_mode
            FROM signal_quarterly
            WHERE drug = %s AND event = %s AND primary_suspect_mode = %s
            ORDER BY quarter ASC
            """,
            (res["drug"], res["event"], res["primary_suspect_mode"]),
        )
        res["quarterly_history"] = [dict(q) for q in cur.fetchall()]

    return res


def get_drug_profile(conn, drug: str) -> dict[str, Any] | None:
    """Comprehensive drug safety profile with demographics, top signals, and roles."""
    drug_upper = drug.strip().upper()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Check if drug exists in cases
        cur.execute(
            """
            SELECT COUNT(DISTINCT c.primaryid) AS total_cases,
                   COUNT(DISTINCT cd.role) AS distinct_roles
            FROM case_drugs cd
            JOIN cases c ON c.primaryid = cd.primaryid AND c.is_current
            WHERE cd.drug_normalized = %s
            """,
            (drug_upper,),
        )
        base = cur.fetchone()
        if not base or base["total_cases"] == 0:
            return None

        total_cases = base["total_cases"]

        # Role breakdown
        cur.execute(
            """
            SELECT COALESCE(cd.role, 'UNKNOWN') AS role, COUNT(DISTINCT cd.primaryid) AS cases
            FROM case_drugs cd
            JOIN cases c ON c.primaryid = cd.primaryid AND c.is_current
            WHERE cd.drug_normalized = %s
            GROUP BY 1 ORDER BY 2 DESC
            """,
            (drug_upper,),
        )
        roles = [dict(r) for r in cur.fetchall()]

        # Demographics: Sex
        cur.execute(
            """
            SELECT COALESCE(c.sex, 'UNKNOWN') AS sex, COUNT(*) AS cases
            FROM case_drugs cd
            JOIN cases c ON c.primaryid = cd.primaryid AND c.is_current
            WHERE cd.drug_normalized = %s
            GROUP BY 1 ORDER BY 2 DESC
            """,
            (drug_upper,),
        )
        sex_distribution = [dict(r) for r in cur.fetchall()]

        # Demographics: Age groups
        cur.execute(
            """
            SELECT
                CASE
                    WHEN c.age_years IS NULL THEN 'Unknown'
                    WHEN c.age_years < 18 THEN '<18 Pediatric'
                    WHEN c.age_years BETWEEN 18 AND 44 THEN '18-44 Adult'
                    WHEN c.age_years BETWEEN 45 AND 64 THEN '45-64 Middle'
                    ELSE '65+ Senior'
                END AS age_group,
                COUNT(*) AS cases
            FROM case_drugs cd
            JOIN cases c ON c.primaryid = cd.primaryid AND c.is_current
            WHERE cd.drug_normalized = %s
            GROUP BY 1
            ORDER BY cases DESC
            """,
            (drug_upper,),
        )
        age_distribution = [dict(r) for r in cur.fetchall()]

        # Top adverse reactions from signal_scores
        cur.execute(
            """
            SELECT signal_id, event, a, b, c, d, prr, ror, chi_square,
                   prr_screen, ror_screen, continuity_corrected
            FROM signal_scores
            WHERE drug = %s AND period = 'all' AND NOT primary_suspect_mode
            ORDER BY prr_screen DESC, a DESC, prr DESC
            LIMIT 20
            """,
            (drug_upper,),
        )
        top_signals = [dict(r) for r in cur.fetchall()]

        # Count of screened signals
        cur.execute(
            """
            SELECT COUNT(*) AS screened_count
            FROM signal_scores
            WHERE drug = %s AND period = 'all' AND NOT primary_suspect_mode AND prr_screen
            """,
            (drug_upper,),
        )
        screened_count = cur.fetchone()["screened_count"]

        # Reporting quarterly trend
        cur.execute(
            """
            SELECT c.source_quarter AS quarter, COUNT(DISTINCT cd.primaryid) AS cases
            FROM case_drugs cd
            JOIN cases c ON c.primaryid = cd.primaryid AND c.is_current
            WHERE cd.drug_normalized = %s
            GROUP BY 1 ORDER BY 1 ASC
            """,
            (drug_upper,),
        )
        quarterly_trend = [dict(r) for r in cur.fetchall()]

    return {
        "drug": drug_upper,
        "total_cases": total_cases,
        "screened_signals_count": screened_count,
        "roles": roles,
        "sex_distribution": sex_distribution,
        "age_distribution": age_distribution,
        "top_signals": top_signals,
        "quarterly_trend": quarterly_trend,
    }


def get_event_profile(conn, event: str) -> dict[str, Any] | None:
    """Comprehensive adverse event profile with demographics and top implicated drugs."""
    event_upper = event.strip().upper()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Check if event exists
        cur.execute(
            """
            SELECT COUNT(DISTINCT c.primaryid) AS total_cases
            FROM case_events ce
            JOIN cases c ON c.primaryid = ce.primaryid AND c.is_current
            WHERE ce.event_term = %s
            """,
            (event_upper,),
        )
        base = cur.fetchone()
        if not base or base["total_cases"] == 0:
            return None

        total_cases = base["total_cases"]

        # Demographics: Sex
        cur.execute(
            """
            SELECT COALESCE(c.sex, 'UNKNOWN') AS sex, COUNT(*) AS cases
            FROM case_events ce
            JOIN cases c ON c.primaryid = ce.primaryid AND c.is_current
            WHERE ce.event_term = %s
            GROUP BY 1 ORDER BY 2 DESC
            """,
            (event_upper,),
        )
        sex_distribution = [dict(r) for r in cur.fetchall()]

        # Demographics: Age groups
        cur.execute(
            """
            SELECT
                CASE
                    WHEN c.age_years IS NULL THEN 'Unknown'
                    WHEN c.age_years < 18 THEN '<18 Pediatric'
                    WHEN c.age_years BETWEEN 18 AND 44 THEN '18-44 Adult'
                    WHEN c.age_years BETWEEN 45 AND 64 THEN '45-64 Middle'
                    ELSE '65+ Senior'
                END AS age_group,
                COUNT(*) AS cases
            FROM case_events ce
            JOIN cases c ON c.primaryid = ce.primaryid AND c.is_current
            WHERE ce.event_term = %s
            GROUP BY 1
            ORDER BY cases DESC
            """,
            (event_upper,),
        )
        age_distribution = [dict(r) for r in cur.fetchall()]

        # Top implicated drugs from signal_scores
        cur.execute(
            """
            SELECT signal_id, drug, a, b, c, d, prr, ror, chi_square,
                   prr_screen, ror_screen, continuity_corrected
            FROM signal_scores
            WHERE event = %s AND period = 'all' AND NOT primary_suspect_mode
            ORDER BY prr_screen DESC, a DESC, prr DESC
            LIMIT 20
            """,
            (event_upper,),
        )
        top_drugs = [dict(r) for r in cur.fetchall()]

        # Quarterly event volume
        cur.execute(
            """
            SELECT c.source_quarter AS quarter, COUNT(DISTINCT ce.primaryid) AS cases
            FROM case_events ce
            JOIN cases c ON c.primaryid = ce.primaryid AND c.is_current
            WHERE ce.event_term = %s
            GROUP BY 1 ORDER BY 1 ASC
            """,
            (event_upper,),
        )
        quarterly_trend = [dict(r) for r in cur.fetchall()]

    return {
        "event": event_upper,
        "total_cases": total_cases,
        "sex_distribution": sex_distribution,
        "age_distribution": age_distribution,
        "top_drugs": top_drugs,
        "quarterly_trend": quarterly_trend,
    }


def get_dashboard_stats(conn, focus_drugs: tuple[str, ...] = ()) -> dict[str, Any]:
    """Overview statistics for dashboard display."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Cases count
        cur.execute("SELECT COUNT(*) AS count FROM cases WHERE is_current")
        cases_count = cur.fetchone()["count"]

        # Signal totals
        cur.execute(
            """
            SELECT
                COUNT(*) AS total_pairs,
                COUNT(*) FILTER (WHERE prr_screen) AS prr_screened,
                COUNT(*) FILTER (WHERE ror_screen) AS ror_screened
            FROM signal_scores
            WHERE period = 'all' AND NOT primary_suspect_mode
            """
        )
        sig_stats = cur.fetchone()

        # Primary suspect signal totals
        cur.execute(
            """
            SELECT
                COUNT(*) AS ps_pairs,
                COUNT(*) FILTER (WHERE prr_screen) AS ps_prr_screened
            FROM signal_scores
            WHERE period = 'all' AND primary_suspect_mode
            """
        )
        ps_stats = cur.fetchone()

        # Distinct drugs and events count
        cur.execute(
            """
            SELECT COUNT(DISTINCT drug) AS drug_count,
                   COUNT(DISTINCT event) AS event_count
            FROM signal_scores
            WHERE period = 'all' AND NOT primary_suspect_mode
            """
        )
        distinct_counts = cur.fetchone()

        # Focus drugs snapshot
        focus_list = []
        for fd in focus_drugs:
            fd_clean = fd.strip().upper()
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_pairs,
                    COUNT(*) FILTER (WHERE prr_screen) AS screened_pairs,
                    MAX(a) AS max_cases
                FROM signal_scores
                WHERE drug = %s AND period = 'all' AND NOT primary_suspect_mode
                """,
                (fd_clean,),
            )
            fd_row = cur.fetchone()

            # Top signal for this focus drug
            cur.execute(
                """
                SELECT signal_id, event, a, prr, ror, chi_square
                FROM signal_scores
                WHERE drug = %s AND period = 'all' AND NOT primary_suspect_mode AND prr_screen
                ORDER BY a DESC
                LIMIT 1
                """,
                (fd_clean,),
            )
            top_sig = cur.fetchone()

            focus_list.append({
                "drug": fd_clean,
                "total_pairs": fd_row["total_pairs"] if fd_row else 0,
                "screened_pairs": fd_row["screened_pairs"] if fd_row else 0,
                "max_cases": fd_row["max_cases"] if fd_row else 0,
                "top_signal": dict(top_sig) if top_sig else None,
            })

        # Top 10 prominent signals across the dataset
        cur.execute(
            """
            SELECT signal_id, drug, event, a, prr, ror, chi_square, quarters_observed
            FROM signal_scores
            WHERE period = 'all' AND NOT primary_suspect_mode AND prr_screen AND a >= 25
            ORDER BY a DESC, prr DESC
            LIMIT 10
            """
        )
        prominent_signals = [dict(r) for r in cur.fetchall()]

        # Distinct quarters monitored
        cur.execute(
            """
            SELECT DISTINCT source_quarter
            FROM cases
            WHERE is_current
            ORDER BY source_quarter ASC
            """
        )
        quarters = [r["source_quarter"] for r in cur.fetchall()]

    return {
        "cases_count": cases_count,
        "distinct_drugs": distinct_counts["drug_count"] if distinct_counts else 0,
        "distinct_events": distinct_counts["event_count"] if distinct_counts else 0,
        "total_pairs": sig_stats["total_pairs"] if sig_stats else 0,
        "screened_prr_count": sig_stats["prr_screened"] if sig_stats else 0,
        "screened_ror_count": sig_stats["ror_screened"] if sig_stats else 0,
        "primary_suspect_pairs": ps_stats["ps_pairs"] if ps_stats else 0,
        "primary_suspect_screened": ps_stats["ps_prr_screened"] if ps_stats else 0,
        "quarters": quarters,
        "focus_drugs": focus_list,
        "prominent_signals": prominent_signals,
    }


def search_drugs(conn, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Autocomplete search for normalized drug names."""
    q_clean = query.strip().upper()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT drug, COUNT(*) AS signal_count, MAX(a) AS max_cases
            FROM signal_scores
            WHERE drug ILIKE %s AND period = 'all' AND NOT primary_suspect_mode
            GROUP BY drug
            ORDER BY max_cases DESC
            LIMIT %s
            """,
            (f"%{q_clean}%", max(1, min(50, limit))),
        )
        return [dict(r) for r in cur.fetchall()]


def search_events(conn, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Autocomplete search for adverse event terms."""
    q_clean = query.strip().upper()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT event, COUNT(*) AS signal_count, MAX(a) AS max_cases
            FROM signal_scores
            WHERE event ILIKE %s AND period = 'all' AND NOT primary_suspect_mode
            GROUP BY event
            ORDER BY max_cases DESC
            LIMIT %s
            """,
            (f"%{q_clean}%", max(1, min(50, limit))),
        )
        return [dict(r) for r in cur.fetchall()]
