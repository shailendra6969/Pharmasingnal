#!/usr/bin/env python3
"""Apply SQL migrations in lexical order (see docs/plans/04).

Idempotent: applied files are recorded in schema_migrations and skipped.
Each file is applied in its own transaction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def apply_migrations(database_url: str | None = None, migrations_dir: Path | None = None) -> list[str]:
    url = database_url or settings.database_url()
    mdir = migrations_dir or MIGRATIONS_DIR
    files = sorted(p for p in mdir.glob("*.sql") if not p.name.endswith("_down.sql"))
    applied_now: list[str] = []

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            done = {row[0] for row in cur.fetchall()}

        for path in files:
            if path.name in done:
                continue
            print(f"[migrate] applying {path.name}")
            try:
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)",
                        (path.name,),
                    )
                conn.commit()
                applied_now.append(path.name)
            except Exception as exc:
                conn.rollback()
                print(f"[migrate] FAILED {path.name}: {exc}", file=sys.stderr)
                raise
    finally:
        conn.close()
    return applied_now


def main(argv: list[str] | None = None) -> int:
    url = None
    if argv and "--test" in argv:
        url = settings.test_database_url()
    applied = apply_migrations(url)
    if applied:
        print(f"[migrate] applied {len(applied)} migration(s): {applied}")
    else:
        print("[migrate] nothing to apply (up to date)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
