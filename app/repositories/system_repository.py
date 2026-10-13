"""Repository layer for operational reads (spec §46: routes → services → repos)."""
from __future__ import annotations

import psycopg2


def connect(database_url: str):
    return psycopg2.connect(database_url, connect_timeout=5)


def check_database(database_url: str) -> tuple[bool, str | None]:
    """Cheap connectivity probe. Returns (ok, error_message)."""
    try:
        conn = connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True, None
    except Exception as exc:  # surfaced in payload, never a stack trace
        return False, f"{type(exc).__name__}: {exc}"[:300]


def latest_quality_report(database_url: str) -> dict | None:
    """Most recent data-quality report (spec §7, §38) or None if never run."""
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT report_id, run_id, generated_at, quarters, report
                FROM data_quality_reports
                ORDER BY report_id DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            report_id, run_id, generated_at, quarters, report = row
            return {
                "report_id": report_id,
                "run_id": run_id,
                "generated_at": generated_at.isoformat(),
                "quarters": list(quarters),
                "report": report,
            }
    finally:
        conn.close()


def last_ingest_run(database_url: str) -> dict | None:
    """Latest pipeline run for monitoring (spec §39) or None."""
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, status, started_at, finished_at,
                       duration_seconds, quarters
                FROM ingest_runs
                ORDER BY run_id DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            run_id, status, started, finished, duration, quarters = row
            return {
                "run_id": run_id,
                "status": status,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat() if finished else None,
                "duration_seconds": duration,
                "quarters": list(quarters),
            }
    finally:
        conn.close()
