"""End-to-end rehearsal of runbook §5 on a scratch database.

scripts/verify_migration004.py replaces the hand-run acceptance SQL, so it is
exercised here through the same sequence an operator runs, on an isolated
database (never production):

    baseline snapshot -> forward.sql -> compute_signals.py -> verifier PASS

then each failure mode is provoked in isolation and must be reported:

    a surviving brand-named signal row  -> exit 1, brand_scores problem
    tampered pair cells                 -> exit 1, mismatch + staleness

The scratch database is dropped in the fixture teardown; nothing is left behind.
"""
from __future__ import annotations

from pathlib import Path

import psycopg2
import pytest

from scripts.compute_signals import main as compute_main
from scripts.report_figures import main as figures_main
from scripts.verify_migration004 import main as verify_main
from tests.integration.test_report_figures_cli import FOCUS_SEED

MIGRATION_DIR = (
    Path(__file__).resolve().parents[2]
    / "docs" / "migrations" / "004_focus_drug_brand_aliases"
)


def _rehearsal_urls() -> tuple[str, str]:
    """(db_name, url) of an isolated scratch database."""
    from urllib.parse import urlparse, urlunparse
    from app.config import settings

    parsed = urlparse(settings.test_database_url())
    name = "pharmasignal_mig004_verify"
    return name, urlunparse(parsed._replace(path=f"/{name}"))


@pytest.fixture()
def post_migration_db(tmp_path):
    """Scratch database taken through snapshot -> forward -> recompute."""
    from scripts.create_databases import ensure_database
    from scripts.migrate import apply_migrations

    dbname, url = _rehearsal_urls()
    admin_url = url.rsplit("/", 1)[0] + "/postgres"

    def admin(sql: str) -> None:
        conn = psycopg2.connect(admin_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.close()

    try:
        admin(f'DROP DATABASE IF EXISTS "{dbname}"')     # stale-run leftovers
        ensure_database(url)
        apply_migrations(url)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL not available for §5 rehearsal: {exc}")

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for i, (generic, brand, event) in enumerate(FOCUS_SEED):
                for primaryid, drug, evt in (
                    (f"VRFG{i}", generic, event),
                    (f"VRFB{i}", brand, event),
                    (f"VRFE{i}", None, event),
                    (f"VRFX{i}", None, f"VRFILL{i}"),
                ):
                    cur.execute(
                        "INSERT INTO cases (primaryid, case_id, source_quarter) "
                        "VALUES (%s, %s, '2099q1')",
                        (primaryid, f"VRFC{i}"),
                    )
                    cur.execute(
                        "INSERT INTO case_events (primaryid, case_id, event_term) "
                        "VALUES (%s, %s, %s)",
                        (primaryid, f"VRFC{i}", evt),
                    )
                    if drug:
                        cur.execute(
                            "INSERT INTO case_drugs (primaryid, case_id, "
                            "drug_normalized, role) VALUES (%s, %s, %s, 'PS')",
                            (primaryid, f"VRFC{i}", drug),
                        )
        conn.commit()
    finally:
        conn.close()

    # §3: baseline snapshot on the pre-migration state.
    snap = tmp_path / "baseline.json"
    assert figures_main(["--snapshot", "--force", "--snapshot-path", str(snap),
                         "--database-url", url]) == 0

    # §4: the forward run itself.
    forward = (MIGRATION_DIR / "forward.sql").read_text(encoding="utf-8")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(forward)
    finally:
        conn.close()

    # §5: recompute both modes with the tiny dataset's floor.
    assert compute_main(["--mode", "both", "--min-cases", "1",
                         "--database-url", url]) == 0

    yield url, snap

    admin(f'DROP DATABASE IF EXISTS "{dbname}"')


def test_verifier_passes_after_a_correct_recomputation(
        post_migration_db, capsys):
    url, snap = post_migration_db
    rc = verify_main(["--snapshot-path", str(snap), "--database-url", url])
    out = capsys.readouterr()
    assert rc == 0, f"§5 acceptance must pass after a clean run: {out.err}"
    assert "PASS" in out.out
    assert "brand_scores = 0" in out.out


def test_verifier_fails_on_a_surviving_brand_signal(post_migration_db, capsys):
    url, snap = post_migration_db
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO signal_scores (drug, event, period, a, b, c, d, "
                "case_count, prr, chi_square, primary_suspect_mode, prr_screen, "
                "ror_screen) VALUES "
                "('ZOLOFT', 'SEROTONIN SYNDROME', 'all', 5, 5, 10, 100, 5, "
                "2.0, 9.0, FALSE, TRUE, TRUE)"
            )
    finally:
        conn.close()

    rc = verify_main(["--snapshot-path", str(snap), "--database-url", url])
    out = capsys.readouterr()
    assert rc == 1, "a brand-named signal row must fail §5 acceptance"
    assert "brand_scores = 1" in out.err


def test_verifier_fails_on_tampered_pair_cells(post_migration_db, capsys):
    url, snap = post_migration_db
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE signal_scores SET a = a + 1 "
                "WHERE drug = 'SERTRALINE' AND event = 'SEROTONIN SYNDROME' "
                "AND period = 'all' AND NOT primary_suspect_mode"
            )
    finally:
        conn.close()

    rc = verify_main(["--snapshot-path", str(snap), "--database-url", url])
    out = capsys.readouterr()
    assert rc == 1, "cells that drifted from the projection must fail §5"
    assert "do not equal the projected" in out.err
    assert "stale" in out.err
