#!/usr/bin/env python3
"""Ingest official FDA Structured Product Labeling (SPL) XML into doc_chunks (Phase 4).

Downloads, parses, and loads section-aware plain-text label chunks into PostgreSQL:

    python scripts/ingest_labels.py                       # all focus drugs
    python scripts/ingest_labels.py --drug ATORVASTATIN   # single curated drug
    python scripts/ingest_labels.py --drug ASPIRIN        # any drug: set_id auto-discovered
    python scripts/ingest_labels.py --drug A --drug B     # repeatable: a whole batch
    python scripts/ingest_labels.py --file path/to/label.xml --drug ATORVASTATIN
    python scripts/ingest_labels.py --dry-run             # inspect chunks without saving

Exit codes:
    0  every requested drug ingested
    2  at least one drug could not be resolved, downloaded or parsed (the
       rest of the batch still ingests — a partial run reports itself red)

Safety & Invariants:
- Strictly plain text (no pgvector / embedding dependencies).
- Idempotent: re-running replaces prior chunks for the same label without duplicates.
- Raw downloaded files are cached in data/raw/dailymed/ (gitignored).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.rag.dailymed_client import DEFAULT_FOCUS_DRUG_SPLS, DailyMedClient
from app.rag.spl_parser import parse_spl_xml
from app.repositories import doc_repository, loader


def ingest_label(
    conn,
    drug: str,
    xml_content: str,
    source_url: str | None = None,
    dry_run: bool = False,
) -> tuple[int, dict[str, int]]:
    """Parse XML and load chunks into doc_chunks if not dry_run."""
    parsed = parse_spl_xml(xml_content, drug=drug, source_url=source_url)
    section_counts: dict[str, int] = {}
    for c in parsed.chunks:
        section_counts[c.section] = section_counts.get(c.section, 0) + 1

    print(
        f"  [parsed] set_id={parsed.set_id} v{parsed.version_number} "
        f"date={parsed.effective_time or 'N/A'} chunks={len(parsed.chunks)} "
        f"sponsor={parsed.sponsor or 'N/A'}"
    )

    if not dry_run and conn is not None:
        inserted = doc_repository.replace_label_chunks(
            conn, drug=drug, set_id=parsed.set_id, chunks=parsed.chunks
        )
        print(f"  [loaded] {inserted} chunks stored in doc_chunks for {drug}")
    elif dry_run:
        print(f"  [dry-run] skipped database write for {len(parsed.chunks)} chunks")

    return len(parsed.chunks), section_counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest FDA DailyMed SPL labels into doc_chunks")
    parser.add_argument(
        "--drug",
        action="append",
        default=None,
        help=(
            "Generic drug name, repeatable for a batch (default: all configured "
            "focus drugs). Any name works: drugs without a curated set_id are "
            "looked up on DailyMed."
        ),
    )
    parser.add_argument(
        "--set-id",
        default=None,
        help="Explicit DailyMed set_id (overrides default mapping)",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Local SPL XML file (offline mode)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if XML is already cached locally",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse XML and show stats without inserting into database",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override target PostgreSQL database URL",
    )
    args = parser.parse_args(argv)

    client = DailyMedClient()
    db_url = args.database_url or settings.database_url()
    conn = None
    if not args.dry_run:
        conn = loader.connect(db_url)

    target_drugs = args.drug or list(DEFAULT_FOCUS_DRUG_SPLS.keys())
    total_chunks = 0
    failures: list[str] = []

    try:
        if args.file:
            drug = (args.drug[0] if args.drug else "UNKNOWN") or "UNKNOWN"
            print(f"Ingesting local SPL file {args.file} for drug={drug}...")
            content = args.file.read_text(encoding="utf-8")
            n, _ = ingest_label(conn, drug, content, source_url=str(args.file), dry_run=args.dry_run)
            total_chunks += n
        else:
            print(f"Ingesting DailyMed SPL labels for {len(target_drugs)} drug(s)...")
            for drug in target_drugs:
                drug = drug.strip().upper()
                config = DEFAULT_FOCUS_DRUG_SPLS.get(drug, {})
                set_id = args.set_id or config.get("set_id")
                brand = config.get("brand_name", "")

                if not set_id:
                    # Not curated: discover a representative SPL by generic name.
                    print(f"\nProcessing {drug} (resolving set_id from DailyMed)...")
                    try:
                        resolved = client.resolve_set_id(drug)
                    except Exception as exc:
                        print(f"  [error] DailyMed search failed for {drug}: {exc}", file=sys.stderr)
                        failures.append(drug)
                        continue
                    if resolved is None:
                        print(f"  [error] no usable single-ingredient SPL for {drug}", file=sys.stderr)
                        failures.append(drug)
                        continue
                    set_id, title = resolved
                    brand = brand or title
                    print(f"  [discover] set_id={set_id} title={title}")
                else:
                    print(f"\nProcessing {drug} (brand: {brand}, set_id: {set_id})...")

                try:
                    _, xml_content = client.download_spl(drug=drug, set_id=set_id, force=args.force)
                except Exception as exc:
                    print(f"  [error] download failed for {drug} (set_id={set_id}): {exc}", file=sys.stderr)
                    failures.append(drug)
                    continue

                source_url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{set_id}.xml"
                n, _ = ingest_label(conn, drug, xml_content, source_url=source_url, dry_run=args.dry_run)
                total_chunks += n

        print(f"\nIngestion complete: {total_chunks} total chunks processed across {len(target_drugs)} drug(s).")
        if failures:
            print(f"[error] failed to ingest: {', '.join(failures)}", file=sys.stderr)
        if not args.dry_run and conn is not None:
            counts = doc_repository.count_chunks_by_drug(conn)
            print(f"Current doc_chunks totals in database: {counts}")
    finally:
        if conn is not None:
            conn.close()

    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
