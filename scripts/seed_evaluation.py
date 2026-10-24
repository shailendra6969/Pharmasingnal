#!/usr/bin/env python3
"""Seed the deterministic evaluation dataset (spec §35).

CI runs this against its ephemeral PostgreSQL service before
``scripts/run_evaluation.py``: the baseline cases read ``signal_scores`` and
``doc_chunks`` directly, so an unseeded database would score every case
against nothing. Seeding is idempotent — by default it first removes the rows
the evaluation owns, so re-running always yields the same dataset.

Usage:
    python scripts/seed_evaluation.py                        # DATABASE_URL
    python scripts/seed_evaluation.py --database-url postgresql://...
    python scripts/seed_evaluation.py --no-reset              # keep existing rows

Exit codes:
    0  dataset seeded
    2  configuration or database error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                                   # noqa: E402
from app.evaluation.seed import EVALUATION_CHUNKS, EVALUATION_SIGNALS, \
    seed_evaluation_data                                          # noqa: E402
from app.repositories import loader                               # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the evaluation dataset")
    parser.add_argument("--database-url", default=None,
                        help="Target DSN (default: DATABASE_URL)")
    parser.add_argument("--no-reset", action="store_true",
                        help="Keep rows already present instead of replacing them")
    args = parser.parse_args(argv)

    try:
        conn = loader.connect(args.database_url or settings.database_url())
    except Exception as exc:
        print(f"[error] cannot connect to database: {exc}", file=sys.stderr)
        return 2

    try:
        seed_evaluation_data(conn, reset=not args.no_reset)
    except Exception as exc:
        conn.rollback()
        print(f"[error] seeding failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(
        f"[seed] evaluation dataset ready: "
        f"{len(EVALUATION_SIGNALS)} signals, {len(EVALUATION_CHUNKS)} label chunks "
        f"(reset={'no' if args.no_reset else 'yes'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())