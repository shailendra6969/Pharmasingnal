#!/usr/bin/env python3
"""Create the dev and test databases if they do not exist (local dev only)."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


def ensure_database(url: str) -> None:
    parsed = urlparse(url)
    dbname = parsed.path.lstrip("/")
    admin = urlunparse(parsed._replace(path="/postgres"))
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone():
                print(f"[db] exists: {dbname}")
                return
            cur.execute(f'CREATE DATABASE "{dbname}"')
            print(f"[db] created: {dbname}")
    finally:
        conn.close()


def main() -> int:
    ensure_database(settings.database_url())
    ensure_database(settings.test_database_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
