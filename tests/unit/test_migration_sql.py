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


def test_runbook_snapshot_and_rollback_commands_are_self_contained():
    """Two operator-facing failure modes, pinned.

    1. §3: the package ships a baseline snapshot, so a bare `--snapshot` exits 2
       without writing — the documented command must carry `--force`.
    2. §6: rollback is often run from a fresh shell mid-incident, so it must set
       MIG itself instead of inheriting §4's variable.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "report_figures.py --snapshot --force" in text

    section6 = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "MIG=" in section6, "§6 must define MIG before invoking $MIG/rollback.sql"
    assert '-f "$MIG/rollback.sql"' in section6


def _rehearsal_database() -> tuple[str, str]:
    """(db_name, url) of an isolated scratch database for the lifecycle run."""
    from urllib.parse import urlparse, urlunparse
    from app.config import settings

    parsed = urlparse(settings.test_database_url())
    name = "pharmasignal_mig004_rehearsal"
    return name, urlunparse(parsed._replace(path=f"/{name}"))


def test_forward_postflight_rollback_execute_end_to_end():
    """Execute the runbook's own SQL files for real, on a scratch database.

    forward.sql / postflight_inspection.sql / rollback.sql are operator-gated
    against production, so this is the only place they ever run. It mirrors
    runbook §4 and §6 step-for-step and proves, with the files' actual text:

      * preflight runs cleanly pre-migration (guarded before-image probe);
      * guard 2 aborts a NULL-drug_seq unique-index collision AND the abort
        leaves no trace (no before-image table, transaction fully rolled back);
      * forward consolidates, the closing NOTICE reports the row count, and
        postflight verifies 0 brand rows / exact snapshot / drug_original intact;
      * guard 1 blocks double execution, rollback restores every string
        bit-for-bit and stamps restored_at, the double-rollback guard fires,
        and — the §6 retry note — forward stays blocked after a rollback.
    """
    import psycopg2
    from scripts.create_databases import ensure_database
    from scripts.migrate import apply_migrations

    dbname, url = _rehearsal_database()
    admin_url = url.rsplit("/", 1)[0] + "/postgres"

    def _with_admin(sql: str) -> None:
        conn = psycopg2.connect(admin_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.close()

    def run_file(name: str) -> tuple[bool, list[str], str]:
        """Execute a package SQL file the way `psql -f` does (autocommit)."""
        sql = (MIGRATION_DIR / name).read_text(encoding="utf-8")
        conn = psycopg2.connect(url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            return True, list(conn.notices), ""
        except psycopg2.Error as exc:
            return False, list(conn.notices), str(exc).strip().splitlines()[0]
        finally:
            conn.close()

    def query(sql: str, params: tuple = ()):
        conn = psycopg2.connect(url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                # No params -> no %% interpolation, so literal % in the SQL
                # (e.g. LIKE 'RBK%') stays intact.
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                if cur.description:
                    return cur.fetchall()
                return None
        finally:
            conn.close()

    try:
        _with_admin(f'DROP DATABASE IF EXISTS "{dbname}"')   # stale-run leftovers
        ensure_database(url)
        apply_migrations(url)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL not available for lifecycle test: {exc}")

    try:
        # ---- seed: 7 brand rows, 2 controls, a NULL-drug_seq brand, ---------
        # ---- and a case that would collide under guard 2 ---------------------
        conn = psycopg2.connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cases (primaryid, case_id, source_quarter) VALUES
                        ('RBK1001','RBKC01','2024q1'), ('RBK1002','RBKC02','2024q1'),
                        ('RBK1003','RBKC03','2024q1'), ('RBK1004','RBKC04','2024q1'),
                        ('RBK1005','RBKC05','2024q1'), ('RBK1006','RBKC06','2024q1'),
                        ('RBK1007','RBKC07','2024q1'), ('RBK1008','RBKC08','2024q1'),
                        ('RBK1009','RBKC09','2024q1'), ('RBK1010','RBKC10','2024q1'),
                        ('RBK1011','RBKC11','2024q1');
                """)
                cur.execute("""
                    INSERT INTO case_drugs (primaryid, case_id, drug_seq,
                                            drug_normalized, drug_original, role)
                    VALUES
                        ('RBK1001','RBKC01','1','LIPITOR','Lipitor 20mg tab','PS'),
                        ('RBK1002','RBKC02','1','GLUCOPHAGE','Glucophage 500mg','PS'),
                        ('RBK1003','RBKC03','1','GLUCOPHAGE XR','Glucophage XR 750mg','PS'),
                        ('RBK1004','RBKC04','1','ADVIL','Advil Liqui-Gels','PS'),
                        ('RBK1005','RBKC05','1','MOTRIN','Motrin IB','PS'),
                        ('RBK1006','RBKC06','1','PRILOSEC','Prilosec OTC','PS'),
                        ('RBK1007','RBKC07','1','ZOLOFT','Zoloft 50mg','PS'),
                        ('RBK1008','RBKC08','1','CRESTOR','Crestor 10mg','PS'),
                        ('RBK1009','RBKC09','1','JANUVIA','Januvia 100mg','PS'),
                        ('RBK1010','RBKC10',NULL,'LIPITOR','Lipitor 10mg','PS'),
                        ('RBK1011','RBKC11',NULL,'LIPITOR','Lipitor 40mg','PS'),
                        ('RBK1011','RBKC11',NULL,'ATORVASTATIN',NULL,'CON');
                """)
            conn.commit()
        finally:
            conn.close()

        # ---- §4.1: preflight runs cleanly on a pre-migration database -------
        ok, _notices, err = run_file("preflight_inspection.sql")
        assert ok, f"preflight must run cleanly pre-migration: {err}"

        # ---- guard 2: NULL drug_seq collision aborts, leaving no trace -------
        ok, _notices, err = run_file("forward.sql")
        assert not ok, "forward must abort on a NULL-drug_seq unique-index collision"
        assert "uq_case_drugs_pid_drug" in err
        assert query("SELECT to_regclass('case_drugs_before_image')") == [(None,)], (
            "an aborted forward must leave no before-image table behind"
        )
        assert query(
            "SELECT count(*) FROM case_drugs WHERE primaryid = 'RBK1011'"
        ) == [(2,)], "aborted transaction must roll back every statement"

        # ---- clear the collision, then run §4 for real -----------------------
        query("DELETE FROM case_drugs WHERE primaryid = 'RBK1011' "
              "AND drug_normalized = 'ATORVASTATIN'")
        ok, notices, err = run_file("forward.sql")
        assert ok, f"forward must succeed: {err}"
        assert any("Postflight PASSED: Consolidated 9 rows" in n for n in notices), notices

        ok, _notices, err = run_file("postflight_inspection.sql")
        assert ok, f"postflight must run cleanly after forward: {err}"
        assert query(
            "SELECT count(*) FROM case_drugs WHERE drug_normalized IN "
            "('LIPITOR','GLUCOPHAGE','GLUCOPHAGE XR','ADVIL','MOTRIN',"
            "'PRILOSEC','ZOLOFT')"
        ) == [(0,)], "zero targeted brand rows may remain"
        assert query("SELECT count(*) FROM case_drugs_before_image "
                     "WHERE migration_id = '20261015_focus_drug_aliases' ") == [(9,)]
        assert query("SELECT count(*) FROM case_drugs_before_image "
                     "WHERE migration_id = '20261015_focus_drug_aliases' "
                     "AND restored_at IS NOT NULL") == [(0,)]
        # drug_original preserved bit-for-bit; controls untouched
        assert query(
            "SELECT count(*) FROM case_drugs cd JOIN case_drugs_before_image b "
            "ON b.case_drug_id = cd.id "
            "WHERE b.migration_id = '20261015_focus_drug_aliases' "
            "AND cd.drug_original IS DISTINCT FROM b.drug_original"
        ) == [(0,)]
        assert query("SELECT drug_normalized FROM case_drugs WHERE primaryid IN "
                     "('RBK1008','RBK1009') ORDER BY primaryid") \
            == [("CRESTOR",), ("JANUVIA",)]
        assert query("SELECT drug_normalized FROM case_drugs WHERE primaryid = 'RBK1002'") \
            == [("METFORMIN",)]

        # ---- guard 1: double execution blocked -------------------------------
        ok, _notices, err = run_file("forward.sql")
        assert not ok and "already exists" in err

        # ---- §6: rollback restores every string exactly ----------------------
        ok, notices, err = run_file("rollback.sql")
        assert ok, f"rollback must succeed: {err}"
        assert any("Rollback Postflight PASSED: All 9 rows restored" in n
                   for n in notices), notices
        assert query(
            "SELECT drug_normalized FROM case_drugs "
            "WHERE primaryid LIKE 'RBK%' AND drug_normalized IN "
            "('LIPITOR','GLUCOPHAGE','GLUCOPHAGE XR','ADVIL','MOTRIN',"
            "'PRILOSEC','ZOLOFT') ORDER BY primaryid"
        ) == [("LIPITOR",), ("GLUCOPHAGE",), ("GLUCOPHAGE XR",), ("ADVIL",),
              ("MOTRIN",), ("PRILOSEC",), ("ZOLOFT",), ("LIPITOR",),
              ("LIPITOR",)]
        assert query("SELECT count(*) FROM case_drugs_before_image "
                     "WHERE migration_id = '20261015_focus_drug_aliases' "
                     "AND restored_at IS NOT NULL") == [(9,)]

        # ---- double rollback blocked, and forward stays blocked -------------
        ok, _notices, err = run_file("rollback.sql")
        assert not ok and "already restored" in err
        ok, _notices, err = run_file("forward.sql")
        assert not ok and "already exists" in err, (
            "§6 retry note must hold: forward stays blocked after a rollback"
        )
    finally:
        # Best-effort cleanup so a failed assertion does not strand the database;
        # errors here must not mask the assertion that is actually being tested.
        try:
            _with_admin(f'DROP DATABASE IF EXISTS "{dbname}"')
        except psycopg2.Error:
            pass


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
