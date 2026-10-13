#!/usr/bin/env python3
"""Phase 1 ingestion orchestrator (spec §7):

    download (optional) -> parse -> validate -> dedupe decisions -> normalize
    -> load PostgreSQL -> dedup flagging -> data quality report

Usage:
    python scripts/run_ingestion.py --fixtures            # sample data (tests/dev)
    python scripts/run_ingestion.py --quarters 2024q1,2024q2
    python scripts/run_ingestion.py --quarter-dirs 2024q1=/path/to/dir
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.faers import normalize as nz                # noqa: E402
from app.faers.dedupe import VersionRecord           # noqa: E402
from app.faers.parse import normalize_quarter, parse_quarter  # noqa: E402
from app.faers.quality import QualityReport          # noqa: E402
from app.faers.transform import transform_quarter    # noqa: E402
from app.repositories import loader                  # noqa: E402

DATASETS = ("demo", "drug", "reac")

# Advisory-lock id for the single-writer ingestion pipeline (spec §7).
INGEST_LOCK_ID = 8115201


def _version_records_from_demo(parsed_demo) -> list[VersionRecord]:
    fm = parsed_demo.field_map
    df = parsed_demo.frame
    pid = df[fm["primaryid"]].tolist()
    cid = df[fm["caseid"]].tolist() if fm.get("caseid") else pid
    fda = df[fm["fda_dt"]].tolist() if fm.get("fda_dt") else [None] * len(df)
    ver = df[fm["fda_version"]].tolist() if fm.get("fda_version") else [None] * len(df)
    records = []
    for i in range(len(df)):
        records.append(VersionRecord(
            case_id=str(cid[i]).strip(),
            primaryid=str(pid[i]).strip(),
            fda_dt=str(fda[i]).strip() if fda[i] else None,
            fda_version=str(ver[i]).strip() if ver[i] else None,
        ))
    return records


def resolve_quarter_dirs(args) -> list[tuple[str, Path]]:
    """Return [(quarter, dir)] from --fixtures, --quarter-dirs, or --quarters."""
    pairs: list[tuple[str, Path]] = []
    if args.fixtures:
        root = Path(args.samples_root)
        for d in sorted(root.glob("fixture_*")):
            q = d.name.replace("fixture_", "")
            pairs.append((normalize_quarter(q), d))
        if not pairs:
            raise SystemExit(f"No fixture_* directories found under {root}")
        return pairs
    if args.quarter_dirs:
        for spec in args.quarter_dirs.split(","):
            q, _, path = spec.partition("=")
            pairs.append((normalize_quarter(q), Path(path)))
        return pairs
    root = Path(args.raw_root)
    for q in [x.strip() for x in args.quarters.split(",") if x.strip()]:
        q = normalize_quarter(q)
        pairs.append((q, root / q))
    return pairs


def _loaded_quarter_dirs(conn, pairs, raw_root) -> dict[str, Path]:
    """Quarter -> directory for EVERY quarter loaded in the database.

    Deduplication must see all versions of all cases (spec §7); a partial
    run must never demote cases from quarters outside its own scope.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT source_quarter FROM cases ORDER BY 1")
        loaded = [r[0] for r in cur.fetchall()]
    known = {q: d for q, d in pairs if Path(d).exists()}
    out: dict[str, Path] = {}
    missing: list[str] = []
    for q in loaded:
        d = known.get(q) or Path(raw_root) / q
        if d.exists():
            out[q] = d
        else:
            missing.append(f"{q} (expected {d})")
    if missing:
        raise RuntimeError(
            "Deduplication needs DEMO files for every loaded quarter; missing: "
            + ", ".join(missing)
            + ". Download them first, or run with --skip-dedup to leave "
              "is_current flags unchanged."
        )
    return out


def _acquire_ingest_lock(conn) -> bool:
    """Take the single-writer pipeline lock; False if another run is active."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (INGEST_LOCK_ID,))
        got = cur.fetchone()[0]
    conn.commit()
    return got


def _recover_orphaned_runs(conn) -> int:
    """Mark runs left 'running' by a killed client as failed (spec §39).

    Only called while holding INGEST_LOCK_ID, so any 'running' row is
    provably orphaned — a live concurrent run cannot exist.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET status = 'failed', finished_at = now(), "
            "error = %s WHERE status = 'running'",
            ("orphaned: client process died before completion "
             "(recovered on next start)",),
        )
        recovered = cur.rowcount
    conn.commit()
    return recovered


def _persist_run_report(conn, run_id, report) -> None:
    """(Re)write this run's quality-report row; exactly one row per run_id.

    Called after EVERY quarter so a client killed mid-run can never lose
    the stats of quarters that already loaded (regression: 2024q3's report
    was lost to a client disconnect even though its data loaded fine).
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM data_quality_reports WHERE run_id = %s",
                    (run_id,))
        cur.execute(
            "INSERT INTO data_quality_reports (run_id, quarters, report) "
            "VALUES (%s, %s, %s)",
            (run_id, list(report.quarters), json.dumps(report.as_dict())),
        )
    conn.commit()


def _dedup_pass(conn, pairs, raw_root) -> loader.LoadStats:
    dirs = _loaded_quarter_dirs(conn, pairs, raw_root)
    all_records: list[VersionRecord] = []
    for q in sorted(dirs):
        parsed = parse_quarter(dirs[q], q, datasets=("demo",))
        all_records.extend(_version_records_from_demo(parsed["demo"]))
    return loader.apply_dedup(conn, all_records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run FAERS ingestion pipeline")
    parser.add_argument("--quarters", default=",".join(settings.faers_quarters()))
    parser.add_argument("--quarter-dirs", default=None,
                        help="Explicit QUARTER=DIR pairs, comma separated")
    parser.add_argument("--fixtures", action="store_true",
                        help="Ingest data/samples/fixture_* directories")
    parser.add_argument("--samples-root", default=str(Path(__file__).resolve().parents[1] / "data" / "samples"))
    parser.add_argument("--raw-root", default=str(settings.faers_data_path()))
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--skip-dedup", action="store_true")
    parser.add_argument("--dedup-only", action="store_true",
                        help="Skip parsing/loading; recompute is_current flags "
                             "from DEMO files of all loaded quarters")
    args = parser.parse_args(argv)

    database_url = args.database_url or settings.database_url()
    pairs = resolve_quarter_dirs(args)

    conn = loader.connect(database_url)
    if args.dedup_only:
        try:
            if not _acquire_ingest_lock(conn):
                raise SystemExit("Another ingestion run is active (advisory "
                                 "lock held); refusing to run dedup concurrently.")
            stats = _dedup_pass(conn, pairs, args.raw_root)
            print(f"[dedup] current={stats.marked_current} stale={stats.marked_stale}")
            return 0
        finally:
            conn.close()

    if not _acquire_ingest_lock(conn):
        conn.close()
        raise SystemExit("Another ingestion run is active (advisory lock "
                         "held); refusing to start.")
    recovered = _recover_orphaned_runs(conn)
    if recovered:
        print(f"[ingest] recovered {recovered} orphaned run(s) left by a "
              f"killed client")

    report = QualityReport()
    started = time.time()
    run_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingest_runs (quarters, status) VALUES (%s, 'running') RETURNING run_id",
                ([q for q, _ in pairs],),
            )
            run_id = cur.fetchone()[0]
        conn.commit()

        total_load = loader.LoadStats()
        for quarter, directory in pairs:
            print(f"[ingest] {quarter}: parsing {directory}")
            parsed = parse_quarter(directory, quarter, datasets=DATASETS)
            rows, stats = transform_quarter(parsed)
            ls = loader.load_quarter(conn, quarter, rows)
            for attr in ("cases_inserted", "cases_skipped", "drugs_inserted",
                         "drugs_skipped", "events_inserted", "events_skipped"):
                setattr(total_load, attr, getattr(total_load, attr) + getattr(ls, attr))
            report.add_stats(stats)
            print(f"[ingest] {quarter}: cases={len(rows['cases'])} "
                  f"drugs={len(rows['case_drugs'])} events={len(rows['case_events'])} "
                  f"(inserted cases {ls.cases_inserted}, skipped {ls.cases_skipped})")
            report.status = "partial"
            _persist_run_report(conn, run_id, report)
            print(f"[ingest] {quarter}: quality stats persisted (partial)")

        if not args.skip_dedup:
            print("[ingest] computing current versions (dedup) across all "
                  "loaded quarters")
            dedup_stats = _dedup_pass(conn, pairs, args.raw_root)
            print(f"[ingest] current={dedup_stats.marked_current} "
                  f"stale={dedup_stats.marked_stale}")

        counts = loader.database_counts(conn)
        report.set_database_counts(counts)
        report.notes.append(
            "Dedup flags is_current per case_id; all versions retained (never deleted)."
        )
        report.status = "complete"
        out_path = report.write(Path(settings.processed_data_path()))
        _persist_run_report(conn, run_id, report)
        with conn.cursor() as cur:
            duration = time.time() - started
            cur.execute(
                "UPDATE ingest_runs SET finished_at = now(), status = 'succeeded', "
                "duration_seconds = %s WHERE run_id = %s",
                (duration, run_id),
            )
        conn.commit()
        print(f"[ingest] quality report: {out_path}")
        print(f"[ingest] done in {duration:.1f}s; db counts: {counts}")
        return 0
    except BaseException as exc:
        # BaseException: KeyboardInterrupt must also mark the run failed;
        # SIGKILL leaves 'running' rows that the next start recovers.
        conn.rollback()
        if run_id is not None:
            try:
                if report.quarters:
                    _persist_run_report(conn, run_id, report)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ingest_runs SET finished_at = now(), status = 'failed', "
                        "error = %s WHERE run_id = %s",
                        (str(exc)[:2000], run_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
        print(f"[ingest] FAILED: {exc}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
