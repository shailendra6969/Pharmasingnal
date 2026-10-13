"""Repository layer for admin refresh-job tracking (spec §46: routes →
services → repositories). Durable so job status survives API restarts.
"""
from __future__ import annotations

import psycopg2


def connect(database_url: str):
    return psycopg2.connect(database_url, connect_timeout=5)


def create_job(database_url: str, quarter: str, params: dict) -> int:
    import json

    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO refresh_jobs (quarter, params) "
                "VALUES (%s, %s) RETURNING job_id",
                (quarter, json.dumps(params)),
            )
            job_id = cur.fetchone()[0]
        conn.commit()
        return job_id
    finally:
        conn.close()


def mark_running(database_url: str, job_id: int) -> None:
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_jobs SET status = 'running', started_at = now() "
                "WHERE job_id = %s AND status = 'queued'", (job_id,))
        conn.commit()
    finally:
        conn.close()


def finish_job(database_url: str, job_id: int, status: str,
               report: dict | None = None, error: str | None = None) -> None:
    import json

    assert status in ("succeeded", "failed")
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_jobs SET status = %s, finished_at = now(), "
                "error = %s, report = %s WHERE job_id = %s",
                (status, error, json.dumps(report) if report is not None
                 else None, job_id),
            )
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    (job_id, quarter, params, status, started, finished, error, report,
     created) = row
    return {
        "job_id": job_id,
        "quarter": quarter,
        "params": params,
        "status": status,
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "error": error,
        "report": report,
        "created_at": created.isoformat() if created else None,
    }


_SELECT = ("job_id, quarter, params, status, started_at, finished_at, "
           "error, report, created_at")


def get_job(database_url: str, job_id: int) -> dict | None:
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_SELECT} FROM refresh_jobs "
                        "WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_jobs(database_url: str, limit: int = 20) -> list[dict]:
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_SELECT} FROM refresh_jobs "
                        "ORDER BY job_id DESC LIMIT %s", (limit,))
            return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def has_active_job(database_url: str) -> dict | None:
    """A queued/running job, if any (single-flight guard)."""
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT} FROM refresh_jobs "
                "WHERE status IN ('queued', 'running') "
                "ORDER BY job_id DESC LIMIT 1")
            row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()
