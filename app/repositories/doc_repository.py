"""Document chunks repository for FDA SPL label evidence (spec §18, Phase 4).

Manages storage, retrieval, and atomic idempotent updates for `doc_chunks`.

Idempotency Invariant:
- Re-ingesting a document with the same set_id atomically replaces all previous
  chunks for that label, preventing chunk duplication, stale subsections,
  or version conflicts.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from app.rag.spl_parser import DocChunk
from app.repositories.loader import transaction


def replace_label_chunks(
    conn,
    drug: str,
    set_id: str,
    chunks: list[DocChunk],
) -> int:
    """Atomically replace all chunks for a given drug and set_id.

    Args:
        conn: psycopg2 connection.
        drug: Canonical drug name (e.g. 'ATORVASTATIN').
        set_id: DailyMed SPL set_id.
        chunks: List of parsed DocChunk instances.

    Returns:
        Number of chunks inserted/updated.
    """
    drug_clean = drug.strip().upper()
    prefix = f"{set_id}_%"

    with transaction(conn):
        with conn.cursor() as cur:
            # Delete any prior chunks for this set_id to clean up removed or re-indexed subsections
            cur.execute(
                """
                DELETE FROM doc_chunks
                WHERE drug = %s
                  AND (metadata->>'set_id' = %s OR chunk_id LIKE %s);
                """,
                (drug_clean, set_id, prefix),
            )

            if not chunks:
                return 0

            # Insert new chunks
            insert_sql = """
                INSERT INTO doc_chunks (
                    chunk_id, drug, doc_type, section, subsection,
                    text, source_url, document_date, chunk_index,
                    chunking_method, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (chunk_id) DO UPDATE SET
                    text = EXCLUDED.text,
                    section = EXCLUDED.section,
                    subsection = EXCLUDED.subsection,
                    source_url = EXCLUDED.source_url,
                    document_date = EXCLUDED.document_date,
                    metadata = EXCLUDED.metadata;
            """
            for chunk in chunks:
                cur.execute(
                    insert_sql,
                    (
                        chunk.chunk_id,
                        chunk.drug,
                        chunk.doc_type,
                        chunk.section,
                        chunk.subsection,
                        chunk.text,
                        chunk.source_url,
                        chunk.document_date,
                        chunk.chunk_index,
                        chunk.chunking_method,
                        json.dumps(chunk.metadata),
                    ),
                )
    return len(chunks)


def get_chunks_by_drug(
    conn,
    drug: str,
    section: str | None = None,
) -> list[DocChunk]:
    """Retrieve all chunks for a drug, optionally filtered by section."""
    drug_clean = drug.strip().upper()
    with conn.cursor() as cur:
        if section:
            cur.execute(
                """
                SELECT chunk_id, drug, doc_type, section, subsection,
                       text, source_url, document_date, chunk_index,
                       chunking_method, metadata
                FROM doc_chunks
                WHERE drug = %s AND section = %s
                ORDER BY chunk_index ASC;
                """,
                (drug_clean, section),
            )
        else:
            cur.execute(
                """
                SELECT chunk_id, drug, doc_type, section, subsection,
                       text, source_url, document_date, chunk_index,
                       chunking_method, metadata
                FROM doc_chunks
                WHERE drug = %s
                ORDER BY chunk_index ASC;
                """,
                (drug_clean,),
            )
        rows = cur.fetchall()

    chunks: list[DocChunk] = []
    for r in rows:
        chunks.append(
            DocChunk(
                chunk_id=r[0],
                drug=r[1],
                doc_type=r[2],
                section=r[3],
                subsection=r[4],
                text=r[5],
                source_url=r[6],
                document_date=r[7],
                chunk_index=r[8],
                chunking_method=r[9],
                metadata=r[10] if isinstance(r[10], dict) else json.loads(r[10] or "{}"),
            )
        )
    return chunks


def count_chunks_by_drug(conn) -> dict[str, int]:
    """Count stored document chunks grouped by canonical drug name."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT drug, count(*)
            FROM doc_chunks
            GROUP BY drug
            ORDER BY count(*) DESC;
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def count_chunks_by_section(conn, drug: str | None = None) -> dict[str, int]:
    """Count stored chunks grouped by section."""
    with conn.cursor() as cur:
        if drug:
            cur.execute(
                """
                SELECT section, count(*)
                FROM doc_chunks
                WHERE drug = %s
                GROUP BY section
                ORDER BY count(*) DESC;
                """,
                (drug.strip().upper(),),
            )
        else:
            cur.execute(
                """
                SELECT section, count(*)
                FROM doc_chunks
                GROUP BY section
                ORDER BY count(*) DESC;
                """
            )
        return {row[0]: row[1] for row in cur.fetchall()}
