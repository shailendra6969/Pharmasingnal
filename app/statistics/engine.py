"""Signal computation engine (spec §9, §11, §12 persistence).

Full-background rule (spec §5, Rule 6): every 2x2 uses ALL current reports in
the analyzed quarters as the background — never a drug subset.

                    Event       No Event
    Drug              a            b = (a+b) - a
    Other drugs       c = (a+c) - a   d = N - a - b - c

Division of labor:
- SQL only AGGREGATES COUNTS (pair co-occurrence, drug totals, event totals,
  background N) into staging tables.
- Python computes every statistic via app.statistics.disproportionality —
  the single, hand-tested source of truth for numbers (Rule 4).

Persistence (spec §9): quarters_observed = quarters where a_q >= 1;
quarters_screening = quarters where a_q >= 3 (case floor). Quarterly rows
(period = '2024q1' etc.) are stored for pairs that pass the overall floor so
trend views can show the quarterly history without exploding the table.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from psycopg2.extras import execute_batch

from app.statistics.disproportionality import Table2x2, compute

# Modern FAERS role codes + legacy AERS codes for primary suspect (spec §11)
PRIMARY_SUSPECT_ROLES = ("PS", "SUS")


@dataclass
class EngineResult:
    mode: str                    # 'all' | 'primary_suspect'
    background_n: int
    drug_totals: int             # distinct drug-case rows in mode
    event_totals: int
    pairs_considered: int        # pairs meeting overall case floor
    pairs_screened_prr: int
    pairs_screened_ror: int
    quarterly_rows: int
    rows_written: int
    duration_seconds: float
    scores: list[dict] = field(default_factory=list)


def _drug_staging_sql(primary_suspect_only: bool) -> str:
    role_filter = ""
    if primary_suspect_only:
        placeholders = ",".join(f"'{r}'" for r in PRIMARY_SUSPECT_ROLES)
        role_filter = f"WHERE dr.role IN ({placeholders})"
    # DISTINCT: one row per (case, drug, quarter) — a case may list the same
    # drug multiple times (different drug_seq); statistics are CASE counts,
    # not row counts (regression: negative 'c' cells on real 2024q1 data).
    return (
        "SELECT DISTINCT dr.primaryid, dr.drug_normalized AS drug, "
        "cc.source_quarter "
        "FROM case_drugs dr "
        "JOIN stg_cur cc ON cc.primaryid = dr.primaryid "
        f"{role_filter}"
    )


def compute_signals(
    conn,
    *,
    primary_suspect_only: bool = False,
    min_cases: int = 3,
    quarters: list[str] | None = None,
    include_quarter_series: bool = True,
    replace: bool = True,
    progress: bool = False,
) -> EngineResult:
    """Compute full-background signal scores and persist to signal_scores.

    Deterministic: identical database state -> identical scores.
    """
    started = time.time()
    mode = "primary_suspect" if primary_suspect_only else "all"
    scores: list[dict] = []          # signal_scores rows (period='all')
    quarterly_scores: list[dict] = []  # signal_quarterly rows (volume only)

    cur = conn.cursor()

    def run(sql: str, params: tuple = ()) -> None:
        cur.execute(sql, params)

    try:
        run("DROP TABLE IF EXISTS stg_cur")
        run("DROP TABLE IF EXISTS stg_drug")
        run("DROP TABLE IF EXISTS stg_event")
        if quarters:
            run("CREATE UNLOGGED TABLE stg_cur AS "
                "SELECT primaryid, source_quarter FROM cases "
                "WHERE is_current AND source_quarter IN %s", (tuple(quarters),))
        else:
            run("CREATE UNLOGGED TABLE stg_cur AS "
                "SELECT primaryid, source_quarter FROM cases WHERE is_current")

        drug_sql = _drug_staging_sql(primary_suspect_only)
        run(f"CREATE UNLOGGED TABLE stg_drug AS {drug_sql}")
        run("CREATE UNLOGGED TABLE stg_event AS "
            "SELECT DISTINCT ev.primaryid, ev.event_term AS event, "
            "cc.source_quarter "
            "FROM case_events ev JOIN stg_cur cc ON cc.primaryid = ev.primaryid")

        run("SELECT count(*) FROM stg_cur")
        background_n = cur.fetchone()[0]
        run("SELECT count(*) FROM stg_drug")
        drug_totals = cur.fetchone()[0]
        run("SELECT count(*) FROM stg_event")
        event_totals = cur.fetchone()[0]

        # Pair co-occurrence per quarter
        run("""
            CREATE UNLOGGED TABLE stg_pair_q AS
            SELECT d.drug, e.event, d.source_quarter, count(*) AS a_q
            FROM stg_drug d
            JOIN stg_event e ON e.primaryid = d.primaryid
            GROUP BY 1, 2, 3
        """)
        run("CREATE UNLOGGED TABLE stg_pair_all AS "
            "SELECT drug, event, sum(a_q)::bigint AS a "
            "FROM stg_pair_q GROUP BY 1, 2 HAVING sum(a_q) >= %s",
            (min_cases,))
        run("CREATE UNLOGGED TABLE stg_drug_n AS "
            "SELECT drug, count(*)::bigint AS n FROM stg_drug GROUP BY 1")
        run("CREATE UNLOGGED TABLE stg_event_n AS "
            "SELECT event, count(*)::bigint AS n FROM stg_event GROUP BY 1")

        if progress:
            print(f"[signals] mode={mode} N={background_n} "
                  f"drug_rows={drug_totals} event_rows={event_totals}", flush=True)

        run("""
            SELECT p.drug, p.event, p.a,
                   dn.n AS drug_n, en.n AS event_n
            FROM stg_pair_all p
            JOIN stg_drug_n dn ON dn.drug = p.drug
            JOIN stg_event_n en ON en.event = p.event
            ORDER BY p.drug, p.event
        """)
        pair_rows = cur.fetchall()
        pairs_considered = len(pair_rows)

        # Quarterly counts for those pairs (for persistence + trend rows)
        quarterly: dict[tuple[str, str], list[tuple[str, int]]] = {}
        if include_quarter_series and pair_rows:
            run("""
                SELECT q.drug, q.event, q.source_quarter, q.a_q
                FROM stg_pair_q q
                JOIN stg_pair_all p ON p.drug = q.drug AND p.event = q.event
                ORDER BY q.drug, q.event, q.source_quarter
            """)
            for drug, event, quarter, a_q in cur.fetchall():
                quarterly.setdefault((drug, event), []).append((quarter, a_q))

        # ---- statistics in Python (authoritative) ----
        for drug, event, a, drug_n, event_n in pair_rows:
            a, drug_n, event_n = int(a), int(drug_n), int(event_n)
            b = drug_n - a
            c = event_n - a
            d = background_n - a - b - c
            stat = compute(Table2x2(a, b, c, d), min_cases=min_cases)

            qrows = quarterly.get((drug, event), [])
            quarters_observed = sum(1 for _, qa in qrows if qa >= 1)
            quarters_screening = sum(1 for _, qa in qrows if qa >= min_cases)

            score = {
                "drug": drug, "event": event, "period": "all",
                "a": a, "b": b, "c": c, "d": d, "case_count": a,
                "prr": stat.prr, "prr_lower": stat.prr_lower,
                "prr_upper": stat.prr_upper,
                "ror": stat.ror, "ror_lower": stat.ror_lower,
                "ror_upper": stat.ror_upper, "chi_square": stat.chi_square,
                "prr_screen": stat.prr_screen, "ror_screen": stat.ror_screen,
                "insufficient": stat.insufficient,
                "continuity_corrected": stat.continuity_corrected,
                "quarters_observed": quarters_observed,
                "quarters_screening": quarters_screening,
                "background_n": background_n,
                "drug_case_count": drug_n, "event_case_count": event_n,
                "primary_suspect_mode": primary_suspect_only,
            }
            scores.append(score)
            for quarter, a_q in qrows:
                quarterly_scores.append({
                    "drug": drug, "event": event, "quarter": quarter,
                    "a": int(a_q), "background_n": background_n,
                    "primary_suspect_mode": primary_suspect_only,
                })

        # ---- persist ----
        if replace:
            run("DELETE FROM signal_scores WHERE primary_suspect_mode = %s",
                (primary_suspect_only,))
            if quarters:
                run("DELETE FROM signal_quarterly "
                    "WHERE primary_suspect_mode = %s AND quarter IN %s",
                    (primary_suspect_only, tuple(quarters)))
            else:
                run("DELETE FROM signal_quarterly "
                    "WHERE primary_suspect_mode = %s",
                    (primary_suspect_only,))
        insert_sql = """
            INSERT INTO signal_scores (
                drug, event, period, a, b, c, d, case_count,
                prr, prr_lower, prr_upper, ror, ror_lower, ror_upper,
                chi_square, primary_suspect_mode,
                prr_screen, ror_screen, insufficient, continuity_corrected,
                quarters_observed, quarters_screening,
                background_n, drug_case_count, event_case_count
            ) VALUES (
                %(drug)s, %(event)s, %(period)s, %(a)s, %(b)s, %(c)s, %(d)s,
                %(case_count)s, %(prr)s, %(prr_lower)s, %(prr_upper)s,
                %(ror)s, %(ror_lower)s, %(ror_upper)s, %(chi_square)s,
                %(primary_suspect_mode)s, %(prr_screen)s, %(ror_screen)s,
                %(insufficient)s, %(continuity_corrected)s,
                %(quarters_observed)s, %(quarters_screening)s,
                %(background_n)s, %(drug_case_count)s, %(event_case_count)s
            )
            ON CONFLICT (drug, event, period, primary_suspect_mode) DO UPDATE SET
                a = EXCLUDED.a, b = EXCLUDED.b, c = EXCLUDED.c, d = EXCLUDED.d,
                case_count = EXCLUDED.case_count,
                prr = EXCLUDED.prr, prr_lower = EXCLUDED.prr_lower,
                prr_upper = EXCLUDED.prr_upper,
                ror = EXCLUDED.ror, ror_lower = EXCLUDED.ror_lower,
                ror_upper = EXCLUDED.ror_upper,
                chi_square = EXCLUDED.chi_square,
                prr_screen = EXCLUDED.prr_screen,
                ror_screen = EXCLUDED.ror_screen,
                insufficient = EXCLUDED.insufficient,
                continuity_corrected = EXCLUDED.continuity_corrected,
                quarters_observed = EXCLUDED.quarters_observed,
                quarters_screening = EXCLUDED.quarters_screening,
                background_n = EXCLUDED.background_n,
                drug_case_count = EXCLUDED.drug_case_count,
                event_case_count = EXCLUDED.event_case_count,
                created_at = now()
        """
        for i in range(0, len(scores), 2000):
            execute_batch(cur, insert_sql, scores[i:i + 2000], page_size=2000)

        quarterly_insert_sql = """
            INSERT INTO signal_quarterly
                (drug, event, quarter, a, background_n, primary_suspect_mode)
            VALUES (%(drug)s, %(event)s, %(quarter)s, %(a)s, %(background_n)s,
                    %(primary_suspect_mode)s)
            ON CONFLICT (drug, event, quarter, primary_suspect_mode) DO UPDATE SET
                a = EXCLUDED.a,
                background_n = EXCLUDED.background_n,
                created_at = now()
        """
        for i in range(0, len(quarterly_scores), 2000):
            execute_batch(cur, quarterly_insert_sql,
                          quarterly_scores[i:i + 2000], page_size=2000)

        # cleanup staging
        for t in ("stg_pair_q", "stg_pair_all", "stg_drug_n", "stg_event_n",
                  "stg_drug", "stg_event", "stg_cur"):
            run(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return EngineResult(
        mode=mode,
        background_n=background_n,
        drug_totals=drug_totals,
        event_totals=event_totals,
        pairs_considered=pairs_considered,
        pairs_screened_prr=sum(1 for s in scores
                               if s["period"] == "all" and s["prr_screen"]),
        pairs_screened_ror=sum(1 for s in scores
                               if s["period"] == "all" and s["ror_screen"]),
        quarterly_rows=len(quarterly_scores),
        rows_written=len(scores) + len(quarterly_scores),
        duration_seconds=time.time() - started,
        scores=[s for s in scores if s["period"] == "all"],
    )
