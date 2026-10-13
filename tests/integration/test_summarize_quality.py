from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_summarize_quality_over_loaded_quarters(test_db_url, tmp_path, project_root,
                                                capsys):
    """Summary must be built from live DB state + DB-stored run reports only.

    Regression: on-disk report JSONs get overwritten by --fixtures test runs;
    the summary must never read them. All invariants (per-quarter sums, dedup
    flags, reconciliation) are asserted from DB-verified values.
    """
    from scripts.run_ingestion import main as ingest_main
    from scripts.summarize_quality import main as summary_main

    # Ingest fixtures so there is data + a stored run report
    rc = ingest_main(["--fixtures", "--database-url", test_db_url])
    assert rc == 0

    out_path = tmp_path / "summary.json"
    rc = summary_main(["--database-url", test_db_url, "--output", str(out_path)])
    assert rc == 0
    assert out_path.exists()

    summary = json.loads(out_path.read_text(encoding="utf-8"))

    # ---- scope / per-quarter from live DB ----
    quarters = [p["quarter"] for p in summary["per_quarter"]]
    assert quarters == ["2024q1", "2024q2"]

    # fixture DB-verified counts: 5 current + 2 stale = 7 rows
    dc = summary["database_counts"]
    assert dc["total"] == 7
    assert dc["current"] == 5
    assert dc["stale"] == 2
    assert dc["case_drugs"] == 8
    assert dc["case_events"] == 8

    # ---- invariants ----
    dedup = summary["dedup"]
    assert dedup["invariant_current_plus_stale_equals_total"] is True

    rec = summary["reconciliation"]
    assert rec["rows_sum_equals_db_total"] is True
    assert rec["current_sum_equals_db_current"] is True
    assert rec["per_quarter_rows_sum"] == 7
    assert rec["per_quarter_current_sum"] == 5

    # per_quarter rows come from the DB, so 2024q1=5 rows / 2024q2=2 rows
    by_q = {p["quarter"]: p for p in summary["per_quarter"]}
    assert by_q["2024q1"]["case_rows"] == 5
    assert by_q["2024q2"]["case_rows"] == 2
    assert by_q["2024q1"]["age_missing"] == 2   # 100002 missing + 100004 out-of-range

    # ---- stage totals come from DB-stored run report (9 demo raw rows) ----
    st = summary["pipeline_stage_totals"]
    assert st["demo_raw_rows"] == 9             # 7 + 2
    assert st["demo_rejected_rows"] == 2        # missing primaryid + caseid
    assert st["drug_raw_rows"] == 11
    assert st["reac_raw_rows"] == 11
    assert st["drug_rejected_rows"] == 1        # missing primaryid
    assert st["reac_rejected_rows"] == 1        # missing PT
    assert st["demo_rejected_missing_primaryid"] == 1
    assert st["reac_rejected_missing_pt"] == 1


def test_summarize_quality_quarter_filter(test_db_url, tmp_path):
    """--quarters restricts per-quarter stats and scope."""
    from scripts.run_ingestion import main as ingest_main
    from scripts.summarize_quality import main as summary_main

    rc = ingest_main(["--fixtures", "--database-url", test_db_url])
    assert rc == 0

    out_path = tmp_path / "summary_q1.json"
    rc = summary_main(["--database-url", test_db_url,
                       "--quarters", "2024q1", "--output", str(out_path)])
    assert rc == 0
    summary = json.loads(out_path.read_text(encoding="utf-8"))
    assert [p["quarter"] for p in summary["per_quarter"]] == ["2024q1"]
    assert summary["reconciliation"]["per_quarter_rows_sum"] == 5


def test_summarize_quality_no_quarters_fails(test_db_url, tmp_path):
    """Empty database is an error, not an empty report."""
    import psycopg2

    from scripts.summarize_quality import main as summary_main

    # The session-scoped test DB may already hold fixture data from earlier
    # tests; empty it for this check.
    conn = psycopg2.connect(test_db_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE cases RESTART IDENTITY CASCADE")
    conn.close()

    rc = summary_main(["--database-url", test_db_url,
                       "--output", str(tmp_path / "s.json")])
    assert rc == 1
    assert not (tmp_path / "s.json").exists()
