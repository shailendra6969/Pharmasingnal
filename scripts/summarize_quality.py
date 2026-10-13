#!/usr/bin/env python3
"""Consolidated data-quality summary over all loaded FAERS quarters (spec §7, §38).

Authoritative sources (no on-disk JSON files; those can be overwritten by
--fixtures test runs):
  * parse-stage stats: per-run reports stored in `data_quality_reports`
  * row counts / dedup invariants: live database state

Usage:
    python scripts/summarize_quality.py                          # all loaded quarters
    python scripts/summarize_quality.py --quarters 2024q1,2024q2
    python scripts/summarize_quality.py --output data/processed/data_quality_summary_8q.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.repositories import loader                  # noqa: E402


def _per_quarter_db_stats(conn, quarters: list[str] | None) -> list[dict]:
    """Live per-quarter row counts + age/sex missingness from `cases`."""
    where = ""
    params: tuple = ()
    if quarters:
        where = "WHERE source_quarter IN %s"
        params = (tuple(quarters),)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT source_quarter,
                   count(*)                                       AS case_rows,
                   count(*) FILTER (WHERE is_current)             AS current_versions,
                   count(*) FILTER (WHERE age_years IS NULL)      AS age_missing,
                   count(*) FILTER (WHERE sex IS NULL)            AS sex_missing
            FROM cases {where}
            GROUP BY source_quarter
            ORDER BY source_quarter
        """, params)
        rows = cur.fetchall()
    out = []
    for q, case_rows, current, age_miss, sex_miss in rows:
        out.append({
            "quarter": q,
            "case_rows": case_rows,
            "current_versions": current,
            "age_missing": age_miss,
            "sex_missing": sex_miss,
            "age_missing_pct": round(100 * age_miss / case_rows, 1) if case_rows else 0.0,
            "sex_missing_pct": round(100 * sex_miss / case_rows, 1) if case_rows else 0.0,
        })
    return out


def _stage_totals_from_reports(conn, quarters: list[str] | None) -> tuple[dict, list[str]]:
    """Sum parse-stage stats across per-run reports in the database.

    One report per run; a quarter appearing in several runs (re-ingests)
    is counted once — the report of its latest successful run.
    Returns (stage_totals, notes) where notes flag quarters without a
    stored report (data may still be complete — only stats are missing).
    """
    notes: list[str] = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT r.report, r.generated_at
            FROM data_quality_reports r
            JOIN ingest_runs u ON u.run_id = r.run_id
            WHERE u.status IN ('succeeded', 'running')
            ORDER BY r.generated_at
        """)
        rows = cur.fetchall()

    by_quarter: dict[str, dict] = {}
    for report_json, _generated in rows:
        report = report_json if isinstance(report_json, dict) else json.loads(report_json)
        for pq in report.get("per_quarter", []):
            q = pq.get("quarter")
            if q:
                by_quarter[q] = pq  # later run wins

    totals: dict[str, int] = {}
    for q, pq in sorted(by_quarter.items()):
        if quarters and q not in quarters:
            continue
        for key, val in pq.items():
            if isinstance(val, bool):
                continue
            if isinstance(val, int):
                totals[key] = totals.get(key, 0) + val
            elif isinstance(val, dict) and key in ("demo", "drug", "reac"):
                # per-dataset stage stats: raw/accepted/rejected + reasons
                for k in ("raw_rows", "accepted_rows", "rejected_rows"):
                    if isinstance(val.get(k), int):
                        skey = f"{key}_{k}"
                        totals[skey] = totals.get(skey, 0) + val[k]
                for reason, cnt in (val.get("rejection_reasons") or {}).items():
                    rkey = f"{key}_rejected_{reason}"
                    totals[rkey] = totals.get(rkey, 0) + cnt
    return totals, notes


def build_summary(conn, quarters: list[str] | None = None) -> dict:
    counts = loader.database_counts(conn)
    per_quarter = _per_quarter_db_stats(conn, quarters)
    stage_totals, report_notes = _stage_totals_from_reports(conn, quarters)

    loaded_quarters = [row["quarter"] for row in per_quarter]
    covered = set()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT unnest(r.quarters)
            FROM data_quality_reports r
            JOIN ingest_runs u ON u.run_id = r.run_id
            WHERE u.status IN ('succeeded', 'running')
        """)
        covered = {r[0] for r in cur.fetchall()}
    missing_reports = [q for q in loaded_quarters if q not in covered]

    total_rows = sum(r["case_rows"] for r in per_quarter)
    current_sum = sum(r["current_versions"] for r in per_quarter)

    notes = [
        "is_current marks the latest FDA version per case_id; superseded "
        "versions retained (never deleted).",
        "Age/sex missingness reflects FAERS reporting, not pipeline loss.",
    ]
    notes.extend(report_notes)
    if missing_reports:
        notes.append(
            f"No stored parse-stage report for: {', '.join(missing_reports)}; "
            f"their data is DB-verified but stage totals exclude them."
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": (f"FAERS ASCII quarterly archives {loaded_quarters[0]}-"
                  f"{loaded_quarters[-1]} ({len(loaded_quarters)} quarters)"
                  if loaded_quarters else "no quarters loaded"),
        "database_counts": {
            "total": counts["cases"],
            "current": counts["cases_current"],
            "stale": counts["cases_stale"],
            "case_drugs": counts["case_drugs"],
            "case_events": counts["case_events"],
            "distinct_drugs": counts["distinct_drugs"],
            "distinct_events": counts["distinct_events"],
            "source_quarters": counts["source_quarters"],
        },
        "dedup": {
            "current": counts["cases_current"],
            "stale_superseded_versions": counts["cases_stale"],
            "invariant_current_plus_stale_equals_total": (
                counts["cases_current"] + counts["cases_stale"] == counts["cases"]
            ),
        },
        "per_quarter": per_quarter,
        "pipeline_stage_totals": stage_totals,
        "notes": notes,
        "reconciliation": {
            "per_quarter_rows_sum": total_rows,
            "db_total_case_rows": counts["cases"],
            "per_quarter_current_sum": current_sum,
            "db_current": counts["cases_current"],
            "rows_sum_equals_db_total": total_rows == counts["cases"],
            "current_sum_equals_db_current": current_sum == counts["cases_current"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consolidated data-quality summary")
    parser.add_argument("--quarters", default=None,
                        help="Restrict to these quarters (default: all loaded)")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--output", default=None,
                        help="Output JSON path (default: data/processed/"
                             "data_quality_summary_latest.json)")
    args = parser.parse_args(argv)

    quarters = ([q.strip() for q in args.quarters.split(",") if q.strip()]
                if args.quarters else None)
    conn = loader.connect(args.database_url or settings.database_url())
    try:
        summary = build_summary(conn, quarters)
    finally:
        conn.close()

    if not summary["per_quarter"]:
        print("[summary] no quarters loaded; nothing to summarize", file=sys.stderr)
        return 1

    out_dir = Path(settings.processed_data_path())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output) if args.output else out_dir / "data_quality_summary_latest.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[summary] wrote {out_path}")
    rec = summary["reconciliation"]
    print(f"[summary] scope={summary['scope']}")
    print(f"[summary] rows_sum==db_total: {rec['rows_sum_equals_db_total']}, "
          f"current_sum==db_current: {rec['current_sum_equals_db_current']}, "
          f"dedup invariant: {summary['dedup']['invariant_current_plus_stale_equals_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
