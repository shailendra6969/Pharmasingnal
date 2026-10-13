from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SAMPLES = PROJECT_ROOT / "data" / "samples"
FIXTURE_Q1 = SAMPLES / "fixture_2024q1"
FIXTURE_Q2 = SAMPLES / "fixture_2024q2"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def fixture_q1() -> Path:
    assert FIXTURE_Q1.exists(), f"missing fixture: {FIXTURE_Q1}"
    return FIXTURE_Q1


@pytest.fixture(scope="session")
def fixture_q2() -> Path:
    assert FIXTURE_Q2.exists(), f"missing fixture: {FIXTURE_Q2}"
    return FIXTURE_Q2


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Create + migrate the test database once per session.

    Skips the test session if PostgreSQL is unreachable (reported as skipped,
    never as passed).
    """
    import psycopg2

    from app.config import settings
    from scripts.create_databases import ensure_database
    from scripts.migrate import apply_migrations

    url = settings.test_database_url()
    try:
        ensure_database(url)
        apply_migrations(url)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        with conn.cursor() as cur:
            for table in ("case_events", "case_drugs", "outcomes", "cases",
                          "signal_scores", "doc_chunks", "eval_results",
                          "eval_questions", "data_quality_reports",
                          "ingest_runs", "refresh_jobs"):
                cur.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
        conn.close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL not available for integration tests: {exc}")
    return url
