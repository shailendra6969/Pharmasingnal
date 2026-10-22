#!/usr/bin/env python3
"""Run a full signal investigation end to end (spec §23–32).

Usage:
    python scripts/run_investigation.py --drug ATORVASTATIN --event RHABDOMYOLYSIS
    python scripts/run_investigation.py --drug SERTRALINE --event "SEROTONIN SYNDROME" \\
        --mode primary_suspect --investigation-type numeric_only
    python scripts/run_investigation.py --drug METFORMIN --event NAUSEA --json report.json

Exit codes:
    0  status VERIFIED (numeric claims, label evidence, citations and causal guard all pass)
    1  status FLAGGED_WITH_WARNINGS (at least one verification gate failed)
    2  configuration or database error
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.investigation import InvestigationWorkflow   # noqa: E402
from app.config import settings                               # noqa: E402
from app.repositories import loader                           # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a pharmacovigilance signal investigation and verify it"
    )
    parser.add_argument("--drug", required=True, help="Canonical drug name (e.g. ATORVASTATIN)")
    parser.add_argument("--event", required=True, help="MedDRA adverse event term (e.g. RHABDOMYOLYSIS)")
    parser.add_argument("--mode", default="all", choices=["all", "primary_suspect"],
                        help="Screening mode (default: all)")
    parser.add_argument("--investigation-type", default="comprehensive",
                        choices=["comprehensive", "numeric_only"],
                        help="comprehensive requires label evidence (default)")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json", dest="json_path", default=None,
                        help="Write the full report JSON to this path")
    args = parser.parse_args(argv)

    try:
        conn = loader.connect(args.database_url or settings.database_url())
    except Exception as exc:  # connection/config failures -> exit 2
        print(f"[error] cannot connect to database: {exc}", file=sys.stderr)
        return 2

    try:
        workflow = InvestigationWorkflow(conn=conn)
        report = workflow.run(
            drug=args.drug,
            event=args.event,
            mode=args.mode,
            investigation_type=args.investigation_type,
        )
    except Exception as exc:
        print(f"[error] investigation failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if not report:
        print("[error] investigation produced no report", file=sys.stderr)
        return 2

    v = report.get("verification") or {}
    evidence = report.get("evidence", {})
    metrics = report.get("metrics", {})

    print(f"status: {report['status']}  ({report['drug']} / {report['event']}, "
          f"mode={report['mode']}, type={report['investigation_type']})")
    print(f"metrics: cases={metrics.get('case_count')} prr={metrics.get('prr')} "
          f"ror={metrics.get('ror')} screened={metrics.get('screened')}")
    print(f"evidence: chunks={evidence.get('retrieved_chunks_count')} "
          f"citations={evidence.get('citations_count')} "
          f"sections={evidence.get('sections_searched')}")
    print(f"verification: passed={v.get('passed')} "
          f"claims={v.get('claims', {}).get('passed')}/{v.get('claims', {}).get('total')} "
          f"evidence={v.get('evidence', {}).get('passed')} "
          f"citations={v.get('citations', {}).get('passed')} "
          f"causal_guard={v.get('causal_guard', {}).get('passed')}")

    for cit in report.get("citations", []):
        print(f"  citation: {cit['chunk_id']} :: {cit['quoted_span'][:90]}")

    for err in (v.get("evidence", {}).get("errors") or []):
        print(f"  evidence error: {err}")
    for err in (v.get("citations", {}).get("errors") or []):
        print(f"  citation error: {err}")
    for viol in (v.get("causal_guard", {}).get("violations") or []):
        print(f"  causal violation: {viol}")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        print(f"[saved] report -> {args.json_path}")

    return 0 if report.get("status") == "VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
