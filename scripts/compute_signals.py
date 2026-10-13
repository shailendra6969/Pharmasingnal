#!/usr/bin/env python3
"""Compute full-background signal scores (spec §9, §11).

Usage:
    python scripts/compute_signals.py                       # both modes
    python scripts/compute_signals.py --mode all
    python scripts/compute_signals.py --mode primary-suspect --min-cases 3
    python scripts/compute_signals.py --quarters 2024q1,2024q2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.repositories import loader                  # noqa: E402
from app.statistics.engine import compute_signals     # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute PRR/ROR signal scores")
    parser.add_argument("--mode", choices=["all", "primary-suspect", "both"],
                        default="both")
    parser.add_argument("--min-cases", type=int, default=3)
    parser.add_argument("--quarters", default=None,
                        help="Restrict background to these quarters (default: all loaded)")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--no-quarter-series", action="store_true")
    args = parser.parse_args(argv)

    quarters = ([q.strip() for q in args.quarters.split(",") if q.strip()]
                if args.quarters else None)
    modes = {"all": [False], "primary-suspect": [True],
             "both": [False, True]}[args.mode]

    conn = loader.connect(args.database_url or settings.database_url())
    try:
        for ps_only in modes:
            res = compute_signals(
                conn,
                primary_suspect_only=ps_only,
                min_cases=args.min_cases,
                quarters=quarters,
                include_quarter_series=not args.no_quarter_series,
                progress=True,
            )
            print(
                f"[signals] mode={res.mode}: N={res.background_n}, "
                f"pairs>=min_cases={res.pairs_considered}, "
                f"PRR-screened={res.pairs_screened_prr}, "
                f"ROR-screened={res.pairs_screened_ror}, "
                f"rows_written={res.rows_written}, "
                f"{res.duration_seconds:.1f}s"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
