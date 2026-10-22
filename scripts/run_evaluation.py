#!/usr/bin/env python3
"""Run the baseline evaluation set against the investigation pipeline (spec §35).

Usage:
    python scripts/run_evaluation.py                       # all baseline cases
    python scripts/run_evaluation.py --case eval_incorrect_citation_atorvastatin
    python scripts/run_evaluation.py --json eval_report.json
    python scripts/run_evaluation.py --no-persist          # score without writing eval_results

Exit codes:
    0  every executed case matched its expected outcome (skips allowed)
    1  at least one case mismatched its expected outcome
    2  configuration or database error
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                                   # noqa: E402
from app.evaluation.cases import BASELINE_CASES                   # noqa: E402
from app.evaluation.runner import run_evaluation                  # noqa: E402
from app.repositories import loader                               # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run evaluation cases and score verification outcomes"
    )
    parser.add_argument("--case", action="append", default=None, dest="cases",
                        help="Run only this question_id (repeatable)")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--no-persist", action="store_true",
                        help="Do not write eval_questions / eval_results rows")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="Write the scored evaluation report to this path")
    args = parser.parse_args(argv)

    cases = BASELINE_CASES
    if args.cases:
        known = {c.question_id: c for c in BASELINE_CASES}
        unknown = [cid for cid in args.cases if cid not in known]
        if unknown:
            print(f"[error] unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"known: {', '.join(known)}", file=sys.stderr)
            return 2
        cases = [known[cid] for cid in args.cases]

    try:
        conn = loader.connect(args.database_url or settings.database_url())
    except Exception as exc:
        print(f"[error] cannot connect to database: {exc}", file=sys.stderr)
        return 2

    try:
        report = run_evaluation(conn, cases, persist=not args.no_persist)
    except Exception as exc:
        print(f"[error] evaluation failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(f"{'case':46} {'category':20} {'expected':24} {'actual':24} outcome")
    for r in report.results:
        print(
            f"{r.case.question_id:46} {r.case.category:20} "
            f"{r.case.expected_status:24} {r.status:24} {r.outcome}"
        )
        if r.skip_reason:
            print(f"    skip: {r.skip_reason}")
        if r.outcome == "FAIL":
            errs = (
                (r.verification.get("evidence", {}) or {}).get("errors", [])
                + (r.verification.get("citations", {}) or {}).get("errors", [])
                + (r.verification.get("causal_guard", {}) or {}).get("violations", [])
            )
            for err in errs:
                print(f"    gate: {err}")
            for detail in r.claims:
                if not detail.get("passed"):
                    print(f"    claim: {detail.get('reason', detail)}")

    print(
        f"\nsummary: passed={report.passed} failed={report.failed} "
        f"skipped={report.skipped} (persist={'no' if args.no_persist else 'yes'})"
    )

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report.summary(), indent=2, default=str), encoding="utf-8"
        )
        print(f"[saved] report -> {args.json_path}")

    return 0 if report.all_matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
