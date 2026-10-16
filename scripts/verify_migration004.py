#!/usr/bin/env python3
"""Post-recompute acceptance check for migration 004 (runbook §5).

Replaces the hand-run SQL in §5. Run it after `forward.sql` and
`compute_signals.py --mode both` have completed, against the live database:

    1. each of the five §7.3 *Projected* pairs carries exactly the projected
       2x2 cells in `signal_scores` (period='all', non-primary-suspect mode);
    2. `brand_scores = 0` — no `signal_scores` row of any period or mode still
       references one of the seven retired brand names;
    3. those five rows agree with cells recomputed from the live tables, so a
       row left stale by a partial recompute cannot pass.

Expectations come from the baseline snapshot recorded in runbook §3 — the same
artifact `report_figures.py --diff` uses — so no figure is hand-copied into
this script and none can drift from the report.

Usage:
    python scripts/verify_migration004.py
    python scripts/verify_migration004.py --snapshot-path path/to/baseline.json
    python scripts/verify_migration004.py --database-url postgresql://...

Exit codes (same contract as report_figures.py): 0 every assertion holds,
1 an assertion fails (details on stderr), 2 execution error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.repositories import loader                  # noqa: E402
from scripts.report_figures import (                 # noqa: E402
    ALL_BRANDS,
    DEFAULT_SNAPSHOT,
    PAIR_ROWS,
    PROJECTED_SRC,
    ReportError,
    load_snapshot,
)

# The five projected (drug, event) pairs, derived from PAIR_ROWS so the report
# stays the single source of truth for which pairs *are* the projection.
PROJECTED_PAIRS = tuple(
    (drugs[0], event)
    for _label, source, drugs, event in PAIR_ROWS
    if source == PROJECTED_SRC
)


def expected_projected_cells(snapshot: dict) -> dict[tuple[str, str], dict]:
    """(drug, event) -> projected 2x2 cells recorded in the baseline snapshot."""
    by_label = {p["label"]: p for p in snapshot.get("pairs", [])}
    expected: dict[tuple[str, str], dict] = {}
    for label, source, drugs, event in PAIR_ROWS:
        if source != PROJECTED_SRC:
            continue
        row = by_label.get(label)
        if row is None:
            raise ReportError(
                f"snapshot has no row {label!r} — it predates the §7.3 "
                f"projection; re-take it with --force before the window"
            )
        expected[(drugs[0], event)] = {
            "label": label,
            "cells": (row["a"], row["b"], row["c"], row["d"]),
        }
    if len(expected) != len(PROJECTED_PAIRS):
        raise ReportError(
            f"expected {len(PROJECTED_PAIRS)} projected pairs, "
            f"found {len(expected)} in the snapshot"
        )
    return expected


def fetch_signal_rows(conn, pairs: list[tuple[str, str]]) -> dict:
    """(drug, event) -> stored signal_scores row for the five pairs."""
    if not pairs:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT drug, event, a, b, c, d, case_count
            FROM signal_scores
            WHERE period = 'all' AND primary_suspect_mode = FALSE
              AND (drug, event) IN %s
            """,
            (tuple(pairs),),
        )
        rows = cur.fetchall()
    return {(drug, event):
            {"cells": (int(a), int(b), int(c), int(dcell)),
             "case_count": int(case_count)}
            for drug, event, a, b, c, dcell, case_count in rows}


def live_cells(conn, drug: str, event: str) -> tuple[int, int, int, int]:
    """Recompute the 2x2 from the live tables (engine arithmetic, spec §9)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH c AS (SELECT primaryid FROM cases WHERE is_current),
                 d AS (SELECT DISTINCT cd.primaryid FROM case_drugs cd
                       JOIN c USING (primaryid) WHERE cd.drug_normalized = %s),
                 e AS (SELECT DISTINCT ev.primaryid FROM case_events ev
                       JOIN c USING (primaryid) WHERE ev.event_term = %s)
            SELECT (SELECT count(*) FROM cases WHERE is_current),
                   (SELECT count(*) FROM d),
                   (SELECT count(*) FROM d JOIN e USING (primaryid)),
                   (SELECT count(*) FROM e)
            """,
            (drug, event),
        )
        n, drug_n, a, marginal = (int(v) for v in cur.fetchone())
    b = drug_n - a
    c = marginal - a
    d = n - drug_n - marginal + a
    return a, b, c, d


def count_brand_scores(conn, brands) -> int:
    """signal_scores rows still naming a retired brand (any period or mode)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM signal_scores WHERE drug = ANY(%s)",
            (list(brands),),
        )
        return int(cur.fetchone()[0])


def check_results(expected: dict, signal_rows: dict, live: dict,
                  brand_scores: int) -> list[str]:
    """Pure comparison: every problem string, empty when the window can close.

    expected   (drug, event) -> {"label": str, "cells": (a, b, c, d)}
    signal_rows (drug, event) -> {"cells": ..., "case_count": ...}  (stored rows)
    live       (drug, event) -> (a, b, c, d) recomputed from the tables
    """
    problems: list[str] = []
    for key in sorted(expected):
        label = expected[key]["label"]
        projected = expected[key]["cells"]
        row = signal_rows.get(key)
        if row is None:
            problems.append(
                f"{label}: no signal_scores row (period='all', "
                f"non-primary-suspect) — recomputation did not run, or the "
                f"pair fell below the case floor"
            )
            continue
        stored = row["cells"]
        if stored != projected:
            problems.append(
                f"{label}: stored cells {stored} do not equal the projected "
                f"{projected}"
            )
        recomputed = live.get(key)
        if recomputed is not None and recomputed != stored:
            problems.append(
                f"{label}: stored cells {stored} are stale — the live tables "
                f"recompute to {recomputed}; run compute_signals.py again"
            )
    if brand_scores:
        problems.append(
            f"brand_scores = {brand_scores}: signal_scores still references "
            f"retired brand names (expected 0)"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify migration 004 recomputation (runbook §5)")
    parser.add_argument("--snapshot-path", default=str(DEFAULT_SNAPSHOT),
                        help="baseline snapshot recorded in runbook §3")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    try:
        snapshot = load_snapshot(Path(args.snapshot_path))
        expected = expected_projected_cells(snapshot)
    except ReportError as exc:
        print(f"[verify-004] {exc}", file=sys.stderr)
        return 2

    try:
        conn = loader.connect(args.database_url or settings.database_url())
    except Exception as exc:                      # noqa: BLE001
        print(f"[verify-004] cannot connect: {exc}", file=sys.stderr)
        return 2
    try:
        pairs = list(expected)
        signal_rows = fetch_signal_rows(conn, pairs)
        live = {key: live_cells(conn, *key) for key in pairs}
        brand_scores = count_brand_scores(conn, ALL_BRANDS)
    except Exception as exc:                      # noqa: BLE001
        print(f"[verify-004] query failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    for key in sorted(expected):
        label = expected[key]["label"]
        stored = signal_rows.get(key)
        cells = stored["cells"] if stored else None
        print(f"[verify-004] {label}: stored={cells} "
              f"projected={expected[key]['cells']} live={live.get(key)}")

    problems = check_results(expected, signal_rows, live, brand_scores)
    if problems:
        print(f"[verify-004] FAIL: {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"[verify-004] PASS: {len(expected)}/{len(expected)} projected pairs "
          f"match, brand_scores = 0, no stale rows — §5 acceptance met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
