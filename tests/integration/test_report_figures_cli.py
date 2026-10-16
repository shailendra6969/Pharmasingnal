"""Real-database CI guards for the migration 004 snapshot/diff tooling.

tests/unit/test_report_figures.py covers the rendering and diff logic without a
database; these tests exercise `scripts/report_figures.py` end-to-end against a
real PostgreSQL test database — the exact CLI path the operator runbook uses:

    --snapshot            writes the baseline                    (exit 0)
    --snapshot again      refuses to clobber an existing one     (exit 2, needs --force)
    --diff                passes while nothing moved             (exit 0, state=baseline)
    --diff after movement fails loudly                           (exit 1, background N moved)
    --diff after forward.sql (real §4 gate, scratch database)    (exit 0, state=post-execution)
    --diff after rollback.sql  (real §6 gate, scratch database)   (exit 0, state=baseline)

A deliberately tiny seeded dataset keeps the run fast: --snapshot/--diff skip
section 7.5's heavy case_events query by design. Only TEST_DATABASE_URL is ever
used; production is never touched.
"""
from __future__ import annotations

import json

import psycopg2
import pytest

from scripts.report_figures import SNAPSHOT_SCHEMA, main

# (generic, brand, label-canonical event) per focus target. One seeded case per
# (drug, event) role keeps every §7.1-§7.3 figure well-defined: each target has
# at least one generic case (render_72 needs a positive generic denominator)
# and each of the five event terms has a non-focus case (so c > 0 and every
# PRR/ROR/chi-square in render_73 is defined).
FOCUS_SEED = [
    ("ATORVASTATIN", "LIPITOR", "RHABDOMYOLYSIS"),
    ("METFORMIN", "GLUCOPHAGE", "LACTIC ACIDOSIS"),
    ("IBUPROFEN", "ADVIL", "GASTRIC ULCER"),
    ("OMEPRAZOLE", "PRILOSEC", "HYPOMAGNESAEMIA"),
    ("SERTRALINE", "ZOLOFT", "SEROTONIN SYNDROME"),
]
SEEDED_CASES = len(FOCUS_SEED) * 4


@pytest.fixture()
def seeded_db(test_db_url):
    """Migrated test database with one round of synthetic focus-drug data.

    Yields (url, pre-seed current-case count): other integration tests share
    this database and some leave rows behind, so every assertion here is a
    delta against the measured pre-seed state, never an absolute population.
    """
    conn = psycopg2.connect(test_db_url)
    try:
        with conn.cursor() as cur:
            # Same predicate report_figures uses for background N.
            cur.execute("SELECT count(*) FROM cases WHERE is_current")
            baseline_cases = int(cur.fetchone()[0])
            for i, (generic, brand, event) in enumerate(FOCUS_SEED):
                rows = [(f"RPTG{i}", generic, event),      # generic + event
                        (f"RPTB{i}", brand, event),        # brand + event
                        (f"RPTE{i}", None, event),         # event only (c cell)
                        (f"RPTX{i}", None, f"FILLEREVT{i}")]  # filler (d cell)
                for primaryid, drug, evt in rows:
                    cur.execute(
                        "INSERT INTO cases (primaryid, case_id, source_quarter) "
                        "VALUES (%s, %s, '2099q1')",
                        (primaryid, f"RPTC{i}"),
                    )
                    cur.execute(
                        "INSERT INTO case_events (primaryid, case_id, event_term) "
                        "VALUES (%s, %s, %s)",
                        (primaryid, f"RPTC{i}", evt),
                    )
                    if drug:
                        cur.execute(
                            "INSERT INTO case_drugs (primaryid, case_id, "
                            "drug_normalized, role) VALUES (%s, %s, %s, 'PS')",
                            (primaryid, f"RPTC{i}", drug),
                        )
        conn.commit()
    finally:
        conn.close()
    yield test_db_url, baseline_cases
    # Cascade removes the seeded case_events / case_drugs rows too.
    conn = psycopg2.connect(test_db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cases WHERE primaryid LIKE 'RPT%'")
    finally:
        conn.close()


def test_snapshot_roundtrip_and_overwrite_refusal(seeded_db, tmp_path):
    seeded_db_url, baseline_cases = seeded_db
    snap = tmp_path / "baseline.json"

    rc = main(["--snapshot", "--force", "--snapshot-path", str(snap),
               "--database-url", seeded_db_url])
    assert rc == 0, "fresh --snapshot must succeed"
    payload = json.loads(snap.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SNAPSHOT_SCHEMA
    assert payload["background_n"] == baseline_cases + SEEDED_CASES

    # Runbook §3: without --force an existing baseline is protected, exit 2,
    # and the file must be left byte-identical.
    before = snap.read_text(encoding="utf-8")
    rc = main(["--snapshot", "--snapshot-path", str(snap),
               "--database-url", seeded_db_url])
    assert rc == 2, "re-running --snapshot without --force must refuse"
    assert snap.read_text(encoding="utf-8") == before


def test_diff_passes_at_baseline_then_detects_injected_movement(
        seeded_db, tmp_path, capsys):
    seeded_db_url, _baseline_cases = seeded_db
    snap = tmp_path / "baseline.json"
    assert main(["--snapshot", "--force", "--snapshot-path", str(snap),
                 "--database-url", seeded_db_url]) == 0
    capsys.readouterr()

    rc = main(["--diff", "--snapshot-path", str(snap),
               "--database-url", seeded_db_url])
    out = capsys.readouterr()
    assert rc == 0, f"diff at baseline must pass: {out.err}"
    assert "state=baseline" in out.out
    assert "PASS" in out.out

    # Inject an unrelated change — a new current case moves background N, the
    # exact failure mode §4's proof exists to catch (ingestion mid-window).
    conn = psycopg2.connect(seeded_db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cases (primaryid, case_id, source_quarter) "
                "VALUES ('RPTDRIFT', 'RPTCX', '2099q1')"
            )
    finally:
        conn.close()

    rc = main(["--diff", "--snapshot-path", str(snap),
               "--database-url", seeded_db_url])
    out = capsys.readouterr()
    assert rc == 1, "diff must fail once background N moves"
    assert "background N moved" in out.err
    assert "FAIL" in out.err


def test_diff_after_forward_and_rollback_lifecycle(tmp_path, capsys):
    """Runbook §4/§6 through the real CLI on a scratch database.

    The runbook's load-bearing post-execution gate is `--diff` AFTER
    forward.sql — a state the shared-database tests never exercise. There the
    §7.3 baseline brand rows (e.g. LIPITOR) legitimately have zero drug cases
    and their statistics are undefined, so --diff must consume raw figures
    without rendering §7 tables.

    Regression: rendering ran unconditionally in main(), so --diff exited 2
    ("statistics undefined") on exactly the post-execution state §4 exists to
    verify. The lifecycle mirrors runbook §4 and §6 step-for-step: snapshot,
    forward, --diff must pass with state=post-execution; rollback, --diff must
    return to state=baseline.
    """
    from pathlib import Path
    from urllib.parse import urlparse, urlunparse

    from app.config import settings
    from scripts.create_databases import ensure_database
    from scripts.migrate import apply_migrations

    parsed = urlparse(settings.test_database_url())
    dbname = "pharmasignal_mig004_cli_diff"
    url = urlunparse(parsed._replace(path=f"/{dbname}"))
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    mig_dir = (Path(__file__).resolve().parents[2]
               / "docs" / "migrations" / "004_focus_drug_brand_aliases")

    def _with_admin(sql: str) -> None:
        conn = psycopg2.connect(admin_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.close()

    def run_file(filename: str) -> None:
        """Execute a package SQL file the way `psql -f` does (autocommit)."""
        sql = (mig_dir / filename).read_text(encoding="utf-8")
        conn = psycopg2.connect(url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.close()

    try:
        _with_admin(f'DROP DATABASE IF EXISTS "{dbname}"')   # stale-run leftovers
        ensure_database(url)
        apply_migrations(url)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL not available for CLI lifecycle test: {exc}")

    try:
        # Seed one round of focus-drug data: for every target a generic case, a
        # brand case, an event-only case (c cell) and a filler (d cell), so all
        # 15 §7.3 rows and every target figure are well-defined in both states.
        conn = psycopg2.connect(url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for i, (generic, brand, event) in enumerate(FOCUS_SEED):
                    rows = [(f"CLIG{i}", generic, event),
                            (f"CLIB{i}", brand, event),
                            (f"CLIE{i}", None, event),
                            (f"CLIX{i}", None, f"CLIFILLER{i}")]
                    if generic == "METFORMIN":
                        rows.append((f"CLIXR{i}", "GLUCOPHAGE XR", event))
                    for primaryid, drug, evt in rows:
                        cur.execute(
                            "INSERT INTO cases (primaryid, case_id, "
                            "source_quarter) VALUES (%s, %s, '2099q1')",
                            (primaryid, f"CLIC{i}"),
                        )
                        cur.execute(
                            "INSERT INTO case_events (primaryid, case_id, "
                            "event_term) VALUES (%s, %s, %s)",
                            (primaryid, f"CLIC{i}", evt),
                        )
                        if drug:
                            cur.execute(
                                "INSERT INTO case_drugs (primaryid, case_id, "
                                "drug_normalized, role) VALUES (%s, %s, %s, 'PS')",
                                (primaryid, f"CLIC{i}", drug),
                            )
        finally:
            conn.close()

        snap = tmp_path / "cli_diff_baseline.json"
        rc = main(["--snapshot", "--force", "--snapshot-path", str(snap),
                   "--database-url", url])
        assert rc == 0, "--snapshot must succeed on the seeded scratch database"
        capsys.readouterr()

        # ---- §4: forward, then the post-execution gate ----------------------
        run_file("forward.sql")
        rc = main(["--diff", "--snapshot-path", str(snap),
                   "--database-url", url])
        out = capsys.readouterr()
        assert rc == 0, (
            "--diff must PASS after forward.sql (runbook §4 gate); "
            f"exited {rc}: {out.err}"
        )
        assert "state=post-execution" in out.out
        assert "PASS" in out.out

        # ---- §6: rollback, then the return-to-baseline gate -----------------
        run_file("rollback.sql")
        rc = main(["--diff", "--snapshot-path", str(snap),
                   "--database-url", url])
        out = capsys.readouterr()
        assert rc == 0, (
            "--diff must PASS after rollback.sql (runbook §6 gate); "
            f"exited {rc}: {out.err}"
        )
        assert "state=baseline" in out.out
        assert "PASS" in out.out
    finally:
        # Drop the scratch database even on assertion failure so a failed run
        # can never leave the before-image guard in the way of the next one.
        _with_admin(f'DROP DATABASE IF EXISTS "{dbname}"')
