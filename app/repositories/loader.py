"""PostgreSQL loader (spec §7, §46: repositories layer).

Design:
- Rows land via COPY into staging tables, then INSERT ... ON CONFLICT DO
  NOTHING into the real tables. Re-running an ingestion is therefore
  idempotent: duplicate primaryids are skipped and counted.
- The dedup decision (spec §7 version handling) is applied as one UPDATE
  that flags is_current per case_id using the pure rule from
  app/faers/dedupe.py.
"""
from __future__ import annotations

import io
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg2
from psycopg2 import sql as pg_sql

from app.faers.dedupe import VersionRecord, select_current_versions

CASE_COLS = ("primaryid", "case_id", "event_date", "report_date", "age_years",
             "sex", "country", "reporter_type", "version", "report_version",
             "source_quarter")
DRUG_COLS = ("primaryid", "case_id", "drug_seq", "drug_normalized",
             "drug_original", "role", "route", "dose_text")
EVENT_COLS = ("primaryid", "case_id", "event_term")


def connect(database_url: str):
    return psycopg2.connect(database_url)


@contextmanager
def transaction(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _copy_rows(cur, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    buf = io.StringIO()
    for row in rows:
        vals = []
        for v in row:
            if v is None:
                vals.append("\\N")
            else:
                s = str(v)
                s = s.replace("\\", "\\\\").replace("\t", " ").replace("\n", " ").replace("\r", " ")
                vals.append(s)
        buf.write("\t".join(vals) + "\n")
    buf.seek(0)
    cur.copy_expert(
        f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT text)",
        buf,
    )


@dataclass
class LoadStats:
    cases_inserted: int = 0
    cases_skipped: int = 0
    drugs_inserted: int = 0
    drugs_skipped: int = 0
    events_inserted: int = 0
    events_skipped: int = 0
    marked_current: int = 0
    marked_stale: int = 0
    details: dict = field(default_factory=dict)


def _staging_ddl(cur) -> None:
    cur.execute("DROP TABLE IF EXISTS _stg_cases")
    cur.execute("DROP TABLE IF EXISTS _stg_case_drugs")
    cur.execute("DROP TABLE IF EXISTS _stg_case_events")
    cur.execute("""CREATE UNLOGGED TABLE _stg_cases (
        primaryid TEXT, case_id TEXT, event_date TEXT, report_date TEXT,
        age_years DOUBLE PRECISION, sex TEXT, country TEXT,
        reporter_type TEXT, version TEXT, report_version TEXT,
        source_quarter TEXT)""")
    cur.execute("""CREATE UNLOGGED TABLE _stg_case_drugs (
        primaryid TEXT, case_id TEXT, drug_seq TEXT, drug_normalized TEXT,
        drug_original TEXT, role TEXT, route TEXT, dose_text TEXT)""")
    cur.execute("""CREATE UNLOGGED TABLE _stg_case_events (
        primaryid TEXT, case_id TEXT, event_term TEXT)""")

    cur.execute("""CREATE UNLOGGED TABLE _stg_current (
        case_id TEXT PRIMARY KEY, primaryid TEXT)""")


def load_quarter(conn, quarter: str, quarter_rows: dict[str, list[tuple]]) -> LoadStats:
    """Load one quarter's normalized rows (idempotent)."""
    stats = LoadStats()
    with transaction(conn):
        cur = conn.cursor()
        _staging_ddl(cur)

        _copy_rows(cur, "_stg_cases", CASE_COLS, quarter_rows.get("cases", []))
        _copy_rows(cur, "_stg_case_drugs", DRUG_COLS, quarter_rows.get("case_drugs", []))
        _copy_rows(cur, "_stg_case_events", EVENT_COLS, quarter_rows.get("case_events", []))

        cur.execute("""
            INSERT INTO cases (primaryid, case_id, event_date, report_date,
                               age_years, sex, country, reporter_type, version,
                               source_quarter)
            SELECT primaryid, case_id, event_date, report_date, age_years,
                   sex, country, reporter_type, COALESCE(version, report_version),
                   source_quarter
            FROM _stg_cases
            ON CONFLICT (primaryid) DO NOTHING
        """)
        stats.cases_inserted = cur.rowcount
        stats.cases_skipped = len(quarter_rows.get("cases", [])) - stats.cases_inserted

        cur.execute("""
            INSERT INTO case_drugs (primaryid, case_id, drug_seq,
                                    drug_normalized, drug_original, role,
                                    route, dose_text)
            SELECT d.primaryid, d.case_id, d.drug_seq, d.drug_normalized,
                   d.drug_original, d.role, d.route, d.dose_text
            FROM _stg_case_drugs d
            JOIN cases c ON c.primaryid = d.primaryid
            WHERE c.source_quarter = %s
            ON CONFLICT DO NOTHING
        """, (quarter,))
        stats.drugs_inserted = cur.rowcount
        stats.drugs_skipped = len(quarter_rows.get("case_drugs", [])) - stats.drugs_inserted

        cur.execute("""
            INSERT INTO case_events (primaryid, case_id, event_term)
            SELECT e.primaryid, e.case_id, e.event_term
            FROM _stg_case_events e
            JOIN cases c ON c.primaryid = e.primaryid
            WHERE c.source_quarter = %s
            ON CONFLICT DO NOTHING
        """, (quarter,))
        stats.events_inserted = cur.rowcount
        stats.events_skipped = len(quarter_rows.get("case_events", [])) - stats.events_inserted

        cur.execute("DROP TABLE IF EXISTS _stg_cases")
        cur.execute("DROP TABLE IF EXISTS _stg_case_drugs")
        cur.execute("DROP TABLE IF EXISTS _stg_case_events")
        cur.execute("DROP TABLE IF EXISTS _stg_current")
    return stats


def apply_dedup(conn, version_records: list[VersionRecord]) -> LoadStats:
    """Compute current versions with the pure rule and flag them in the DB.

    IMPORTANT: `version_records` MUST cover every quarter loaded in the
    database — case_ids absent from the records are flagged stale, so a
    partial record set would wrongly demote out-of-scope cases.
    run_ingestion._dedup_pass guarantees completeness by reading DEMO files
    for all quarters present in `cases.source_quarter`.
    """
    winners = select_current_versions(version_records)
    stats = LoadStats()
    with transaction(conn):
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS _stg_current")
        cur.execute("CREATE UNLOGGED TABLE _stg_current (case_id TEXT PRIMARY KEY, primaryid TEXT)")
        _copy_rows(cur, "_stg_current", ("case_id", "primaryid"),
                   [(cid, pid) for cid, pid in winners.items()])
        cur.execute("""
            UPDATE cases c
            SET is_current = (s.primaryid = c.primaryid)
            FROM _stg_current s
            WHERE s.case_id = c.case_id
        """)
        cur.execute("""
            UPDATE cases c
            SET is_current = FALSE
            WHERE NOT EXISTS (SELECT 1 FROM _stg_current s WHERE s.case_id = c.case_id)
        """)
        cur.execute("SELECT count(*) FROM cases WHERE is_current")
        stats.marked_current = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM cases WHERE NOT is_current")
        stats.marked_stale = cur.fetchone()[0]
        cur.execute("DROP TABLE IF EXISTS _stg_current")
    return stats


def database_counts(conn) -> dict[str, int]:
    tables = ["cases", "case_drugs", "case_events", "signal_scores",
              "doc_chunks", "eval_questions", "eval_results"]
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(sql_count(t))
            out[t] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM cases WHERE is_current")
        out["cases_current"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM cases WHERE NOT is_current")
        out["cases_stale"] = cur.fetchone()[0]
        cur.execute("SELECT count(DISTINCT source_quarter) FROM cases")
        out["source_quarters"] = cur.fetchone()[0]
        cur.execute("SELECT count(DISTINCT drug_normalized) FROM case_drugs")
        out["distinct_drugs"] = cur.fetchone()[0]
        cur.execute("SELECT count(DISTINCT event_term) FROM case_events")
        out["distinct_events"] = cur.fetchone()[0]
    return out


def sql_count(table: str):
    return pg_sql.SQL("SELECT count(*) FROM {}").format(pg_sql.Identifier(table))
