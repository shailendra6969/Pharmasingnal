"""Unit and isolation tests for the Focus Drug Alias migration package (spec 004).

These tests validate:
1. Integrity and safety rules of forward.sql and rollback.sql scripts.
2. Invariant checks: rollback must NEVER use drug_original.
3. Functional verification of before-image snapshot, forward update, and exact
   rollback restoration using isolated temporary tables on the test database
   (never production).
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

MIGRATION_DIR = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "migrations"
    / "004_focus_drug_brand_aliases"
)


def _first_statement(sql: str) -> str:
    """Return first non-comment, non-empty line."""
    for line in sql.splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            return line
    return ""


def test_migration_files_exist():
    assert (MIGRATION_DIR / "forward.sql").is_file()
    assert (MIGRATION_DIR / "rollback.sql").is_file()
    assert (MIGRATION_DIR / "preflight_inspection.sql").is_file()
    assert (MIGRATION_DIR / "postflight_inspection.sql").is_file()


def test_forward_sql_safety_contracts():
    sql = (MIGRATION_DIR / "forward.sql").read_text(encoding="utf-8")

    # Atomic transaction
    assert _first_statement(sql) == "BEGIN;", "First statement must be BEGIN;"
    assert sql.strip().endswith("COMMIT;"), "Forward SQL must end with COMMIT;"

    # Uses persistent before-image table
    assert "case_drugs_before_image" in sql
    assert "20261015_focus_drug_aliases" in sql

    # Preserves drug_original strictly
    assert "cd.drug_original IS DISTINCT FROM b.drug_original" in sql

    # Covers all 7 target brand aliases
    for brand in ["LIPITOR", "GLUCOPHAGE", "GLUCOPHAGE XR", "ADVIL", "MOTRIN", "PRILOSEC", "ZOLOFT"]:
        assert f"'{brand}'" in sql


def test_rollback_sql_never_uses_drug_original():
    sql = (MIGRATION_DIR / "rollback.sql").read_text(encoding="utf-8")

    # Atomic transaction
    assert _first_statement(sql) == "BEGIN;", "First statement must be BEGIN;"
    assert sql.strip().endswith("COMMIT;"), "Rollback SQL must end with COMMIT;"

    # Critical requirement: Never restore from drug_original
    assert "drug_normalized = drug_original" not in sql
    assert "drug_normalized = cd.drug_original" not in sql

    # Must restore strictly from before-image pre_drug_normalized
    assert "SET drug_normalized = b.pre_drug_normalized" in sql
    assert "20261015_focus_drug_aliases" in sql

    # Must record restored_at timestamp
    assert "SET restored_at = now()" in sql


def test_preflight_inspection_runs_cleanly_on_test_db():
    """preflight_inspection.sql must run to completion on a pre-migration DB.

    Its before-image query has to be existence-guarded: case_drugs_before_image
    does not exist before forward.sql runs, and an unguarded SELECT would abort
    the script with 'relation does not exist'.
    """
    import psycopg2
    from app.config import settings

    sql = (MIGRATION_DIR / "preflight_inspection.sql").read_text(encoding="utf-8")
    try:
        conn = psycopg2.connect(settings.test_database_url())
    except psycopg2.OperationalError:
        pytest.skip("Test database not reachable; skipping preflight inspection test")

    try:
        conn.autocommit = True  # read-only inspection statements
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


RUNBOOK = MIGRATION_DIR / "RUNBOOK.md"


def test_runbook_covers_the_execution_sequence():
    """The operator runbook must cover every step the migration is gated on."""
    assert RUNBOOK.is_file(), "operator runbook is missing from the package"
    text = RUNBOOK.read_text(encoding="utf-8").lower()
    for required in (
        "backup and restore rehearsal",   # section 1
        "quiesce",                        # section 2
        "--snapshot",                     # section 3
        "forward.sql",                    # section 4
        "postflight_inspection.sql",      # section 4
        "report_figures.py", "--diff",   # section 4: the post-execution proof
        "compute_signals.py", "--mode both",  # section 5: immediate recompute
        "rollback.sql",                   # section 6
        "evidence log",                   # section 7
    ):
        assert required in text, f"runbook does not cover {required!r}"


def test_runbook_psql_commands_carry_the_connection_string():
    """psql does not read DATABASE_URL, so every invocation must pass it.

    Regression: the first draft of the runbook ran `psql -v ON_ERROR_STOP=1 -f
    forward.sql` without a connection string, which fails with exit 2 before
    executing anything — exactly the failure mode an operator must not meet
    mid-window.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    offenders = [
        line for line in text.splitlines()
        if line.lstrip().startswith("psql")
        and " -f " in line
        and "$DATABASE_URL" not in line
        and "postgresql://" not in line
    ]
    assert offenders == [], f"psql -f without a connection string: {offenders}"


def test_migration_functional_lifecycle_on_test_db():
    """Verify forward + rollback lifecycle against isolated temp tables.

    Uses settings.test_database_url() only. Never points to production.
    """
    import psycopg2
    from app.config import settings

    test_url = settings.test_database_url()
    try:
        conn = psycopg2.connect(test_url)
    except psycopg2.OperationalError:
        pytest.skip("Test database not reachable; skipping functional DB test")

    conn.autocommit = False
    cur = conn.cursor()

    try:
        # Create isolated temporary tables
        cur.execute("""
            CREATE TEMPORARY TABLE temp_case_drugs (
                id BIGSERIAL PRIMARY KEY,
                primaryid TEXT NOT NULL,
                drug_seq TEXT NOT NULL,
                drug_normalized TEXT NOT NULL,
                drug_original TEXT
            );

            CREATE TEMPORARY TABLE temp_before_image (
                snapshot_id BIGSERIAL PRIMARY KEY,
                migration_id TEXT NOT NULL,
                case_drug_id BIGINT NOT NULL,
                primaryid TEXT NOT NULL,
                drug_seq TEXT,
                pre_drug_normalized TEXT NOT NULL,
                post_drug_normalized TEXT NOT NULL,
                drug_original TEXT,
                captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                restored_at TIMESTAMPTZ,
                UNIQUE (migration_id, case_drug_id)
            );
        """)

        # Insert synthetic test data: brands + controls
        cur.execute("""
            INSERT INTO temp_case_drugs (primaryid, drug_seq, drug_normalized, drug_original)
            VALUES
                ('1001', '1', 'LIPITOR', 'Lipitor 20mg tab'),
                ('1002', '1', 'GLUCOPHAGE', 'Glucophage 500mg'),
                ('1003', '1', 'GLUCOPHAGE XR', 'Glucophage XR 750mg'),
                ('1004', '1', 'ADVIL', 'Advil Liqui-Gels'),
                ('1005', '1', 'MOTRIN', 'Motrin IB'),
                ('1006', '1', 'PRILOSEC', 'Prilosec OTC'),
                ('1007', '1', 'ZOLOFT', 'Zoloft 50mg'),
                ('1008', '1', 'CRESTOR', 'Crestor 10mg'),    -- Non-target control
                ('1009', '1', 'JANUVIA', 'Januvia 100mg');   -- Non-target control
        """)

        # 1. Execute Snapshot
        cur.execute("""
            INSERT INTO temp_before_image (
                migration_id, case_drug_id, primaryid, drug_seq,
                pre_drug_normalized, post_drug_normalized, drug_original
            )
            SELECT 
                '20261015_focus_drug_aliases',
                cd.id, cd.primaryid, cd.drug_seq, cd.drug_normalized,
                CASE
                    WHEN cd.drug_normalized = 'LIPITOR' THEN 'ATORVASTATIN'
                    WHEN cd.drug_normalized IN ('GLUCOPHAGE', 'GLUCOPHAGE XR') THEN 'METFORMIN'
                    WHEN cd.drug_normalized IN ('ADVIL', 'MOTRIN') THEN 'IBUPROFEN'
                    WHEN cd.drug_normalized = 'PRILOSEC' THEN 'OMEPRAZOLE'
                    WHEN cd.drug_normalized = 'ZOLOFT' THEN 'SERTRALINE'
                END,
                cd.drug_original
            FROM temp_case_drugs cd
            WHERE cd.drug_normalized IN ('LIPITOR', 'GLUCOPHAGE', 'GLUCOPHAGE XR', 'ADVIL', 'MOTRIN', 'PRILOSEC', 'ZOLOFT');
        """)
        assert cur.rowcount == 7, "Exactly 7 brand rows must be snapshotted"

        # 2. Execute Forward Update
        cur.execute("""
            UPDATE temp_case_drugs cd
            SET drug_normalized = b.post_drug_normalized
            FROM temp_before_image b
            WHERE cd.id = b.case_drug_id
              AND b.migration_id = '20261015_focus_drug_aliases';
        """)
        assert cur.rowcount == 7, "Exactly 7 brand rows must be updated"

        # Verify Forward State
        cur.execute("SELECT primaryid, drug_normalized, drug_original FROM temp_case_drugs ORDER BY primaryid")
        rows = cur.fetchall()
        expected_forward = [
            ("1001", "ATORVASTATIN", "Lipitor 20mg tab"),
            ("1002", "METFORMIN", "Glucophage 500mg"),
            ("1003", "METFORMIN", "Glucophage XR 750mg"),
            ("1004", "IBUPROFEN", "Advil Liqui-Gels"),
            ("1005", "IBUPROFEN", "Motrin IB"),
            ("1006", "OMEPRAZOLE", "Prilosec OTC"),
            ("1007", "SERTRALINE", "Zoloft 50mg"),
            ("1008", "CRESTOR", "Crestor 10mg"),
            ("1009", "JANUVIA", "Januvia 100mg"),
        ]
        assert rows == expected_forward

        # 3. Execute Rollback
        cur.execute("""
            UPDATE temp_case_drugs cd
            SET drug_normalized = b.pre_drug_normalized
            FROM temp_before_image b
            WHERE cd.id = b.case_drug_id
              AND b.migration_id = '20261015_focus_drug_aliases';
        """)
        assert cur.rowcount == 7, "Exactly 7 brand rows must be restored"

        # Verify Rollback State
        cur.execute("SELECT primaryid, drug_normalized, drug_original FROM temp_case_drugs ORDER BY primaryid")
        restored_rows = cur.fetchall()
        expected_restored = [
            ("1001", "LIPITOR", "Lipitor 20mg tab"),
            ("1002", "GLUCOPHAGE", "Glucophage 500mg"),
            ("1003", "GLUCOPHAGE XR", "Glucophage XR 750mg"),
            ("1004", "ADVIL", "Advil Liqui-Gels"),
            ("1005", "MOTRIN", "Motrin IB"),
            ("1006", "PRILOSEC", "Prilosec OTC"),
            ("1007", "ZOLOFT", "Zoloft 50mg"),
            ("1008", "CRESTOR", "Crestor 10mg"),
            ("1009", "JANUVIA", "Januvia 100mg"),
        ]
        assert restored_rows == expected_restored

    finally:
        conn.rollback()
        conn.close()
