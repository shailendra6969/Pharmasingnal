"""Integration tests for DailyMed label ingestion into doc_chunks (Phase 4).

Validates:
1. Conformance with PostgreSQL `doc_chunks` schema and indices.
2. Idempotent ingestion: repeated ingestion produces identical row counts (zero duplicates).
3. Version replacement: updating from v1 to v2 replaces old chunks cleanly without orphans.
4. Section-level querying and counts via doc_repository.
5. Ingestion CLI execution with --dry-run.

All tests run strictly against settings.test_database_url(), never touching production data.
"""
from __future__ import annotations

from pathlib import Path
import psycopg2
import pytest

from app.config import settings
from app.rag.spl_parser import parse_spl_xml
from app.repositories import doc_repository, loader
from scripts.ingest_labels import ingest_label, main as ingest_main

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spl"


@pytest.fixture
def test_db_conn():
    test_url = settings.test_database_url()
    try:
        conn = psycopg2.connect(test_url)
    except psycopg2.OperationalError:
        pytest.skip("Test database not reachable; skipping label ingestion integration tests")

    # Clean doc_chunks before and after test
    with conn.cursor() as cur:
        cur.execute("DELETE FROM doc_chunks;")
    conn.commit()

    yield conn

    with conn.cursor() as cur:
        cur.execute("DELETE FROM doc_chunks;")
    conn.commit()
    conn.close()


def test_doc_chunks_schema_conformance(test_db_conn):
    """Verify doc_chunks has expected columns and GIN metadata index."""
    with test_db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'doc_chunks'
            ORDER BY ordinal_position;
            """
        )
        cols = {row[0]: row[1] for row in cur.fetchall()}

    expected_cols = [
        "chunk_id", "drug", "doc_type", "section", "subsection",
        "text", "source_url", "document_date", "chunk_index",
        "chunking_method", "metadata", "created_at"
    ]
    for col in expected_cols:
        assert col in cols, f"Missing column {col} in doc_chunks"


def test_idempotent_ingestion_on_test_db(test_db_conn):
    """Re-ingesting the same label must be 100% idempotent (0 duplicates)."""
    xml = (FIXTURE_DIR / "complete_spl.xml").read_text(encoding="utf-8")
    parsed = parse_spl_xml(xml, drug="ATORVASTATIN")
    initial_chunk_count = len(parsed.chunks)
    assert initial_chunk_count > 0

    # First ingestion
    inserted_1 = doc_repository.replace_label_chunks(
        test_db_conn, drug="ATORVASTATIN", set_id=parsed.set_id, chunks=parsed.chunks
    )
    assert inserted_1 == initial_chunk_count

    with test_db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_chunks WHERE drug = 'ATORVASTATIN';")
        count_after_1 = cur.fetchone()[0]
    assert count_after_1 == initial_chunk_count

    # Second ingestion (idempotency check)
    inserted_2 = doc_repository.replace_label_chunks(
        test_db_conn, drug="ATORVASTATIN", set_id=parsed.set_id, chunks=parsed.chunks
    )
    assert inserted_2 == initial_chunk_count

    with test_db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_chunks WHERE drug = 'ATORVASTATIN';")
        count_after_2 = cur.fetchone()[0]
    assert count_after_2 == initial_chunk_count, "Re-ingestion must not create duplicate chunks"


def test_version_replacement_on_test_db(test_db_conn):
    """Ingesting v2 replaces v1 chunks cleanly without orphan chunks."""
    v1_xml = (FIXTURE_DIR / "spl_v1.xml").read_text(encoding="utf-8")
    v2_xml = (FIXTURE_DIR / "spl_v2.xml").read_text(encoding="utf-8")

    doc_v1 = parse_spl_xml(v1_xml, drug="SERTRALINE")
    doc_v2 = parse_spl_xml(v2_xml, drug="SERTRALINE")
    assert doc_v1.set_id == doc_v2.set_id

    # Ingest v1 (2 chunks)
    doc_repository.replace_label_chunks(
        test_db_conn, drug="SERTRALINE", set_id=doc_v1.set_id, chunks=doc_v1.chunks
    )
    chunks_v1 = doc_repository.get_chunks_by_drug(test_db_conn, "SERTRALINE")
    assert len(chunks_v1) == 2
    assert any("Original warnings text in Version 1" in c.text for c in chunks_v1)

    # Ingest v2 (3 chunks with updated warnings text)
    doc_repository.replace_label_chunks(
        test_db_conn, drug="SERTRALINE", set_id=doc_v2.set_id, chunks=doc_v2.chunks
    )
    chunks_v2 = doc_repository.get_chunks_by_drug(test_db_conn, "SERTRALINE")
    assert len(chunks_v2) == 3
    # Check that v1 text is gone and v2 text is present
    assert not any("Original warnings text in Version 1" in c.text for c in chunks_v2)
    assert any("Revised and expanded warnings text in Version 2" in c.text for c in chunks_v2)
    assert any(c.section == "ADVERSE_REACTIONS" for c in chunks_v2)


def test_get_chunks_and_filter_by_section(test_db_conn):
    """Retrieve chunks filtered by section heading."""
    xml = (FIXTURE_DIR / "complete_spl.xml").read_text(encoding="utf-8")
    parsed = parse_spl_xml(xml, drug="ATORVASTATIN")
    doc_repository.replace_label_chunks(
        test_db_conn, drug="ATORVASTATIN", set_id=parsed.set_id, chunks=parsed.chunks
    )

    warning_chunks = doc_repository.get_chunks_by_drug(
        test_db_conn, "ATORVASTATIN", section="WARNINGS_AND_PRECAUTIONS"
    )
    assert len(warning_chunks) >= 2
    for c in warning_chunks:
        assert c.section == "WARNINGS_AND_PRECAUTIONS"
        assert c.drug == "ATORVASTATIN"

    counts_by_drug = doc_repository.count_chunks_by_drug(test_db_conn)
    assert counts_by_drug.get("ATORVASTATIN") == len(parsed.chunks)

    counts_by_sec = doc_repository.count_chunks_by_section(test_db_conn, "ATORVASTATIN")
    assert "WARNINGS_AND_PRECAUTIONS" in counts_by_sec
    assert "INDICATIONS_AND_USAGE" in counts_by_sec


def test_cli_ingest_file_and_dry_run(test_db_conn):
    """CLI script correctly parses files and honors --dry-run flag."""
    test_file = FIXTURE_DIR / "complete_spl.xml"

    # 1. Dry run should NOT write to DB
    ret = ingest_main([
        "--file", str(test_file),
        "--drug", "ATORVASTATIN",
        "--dry-run",
        "--database-url", settings.test_database_url(),
    ])
    assert ret == 0
    with test_db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_chunks;")
        assert cur.fetchone()[0] == 0

    # 2. Live write should write to DB
    ret = ingest_main([
        "--file", str(test_file),
        "--drug", "ATORVASTATIN",
        "--database-url", settings.test_database_url(),
    ])
    assert ret == 0
    with test_db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM doc_chunks WHERE drug = 'ATORVASTATIN';")
        assert cur.fetchone()[0] > 0
