from __future__ import annotations

import json
from pathlib import Path

import psycopg2
import pytest


def _q(url: str, sql: str, params: tuple = ()):
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
            return None
    finally:
        conn.close()


def test_fixture_ingestion_end_to_end(test_db_url, monkeypatch, project_root):
    """spec §50 integration: FAERS files -> validation -> database."""
    import sys
    sys.path.insert(0, str(project_root))
    from scripts.run_ingestion import main

    rc = main(["--fixtures", "--database-url", test_db_url])
    assert rc == 0

    # ---- cases: 5 (q1 accepted) + 2 (q2) = 7 version rows ----
    (total,) = _q(test_db_url, "SELECT count(*) FROM cases")[0]
    assert total == 7

    # ---- dedup: current = one per case_id; 90002's newer q2 version wins ----
    (current,) = _q(test_db_url, "SELECT count(*) FROM cases WHERE is_current")[0]
    (stale,) = _q(test_db_url, "SELECT count(*) FROM cases WHERE NOT is_current")[0]
    assert current == 5
    assert stale == 2
    winner = _q(test_db_url,
                "SELECT primaryid FROM cases WHERE case_id = '90002' AND is_current")[0][0]
    assert winner == "200001"          # cross-quarter version handling
    winner2 = _q(test_db_url,
                 "SELECT primaryid FROM cases WHERE case_id = '90001' AND is_current")[0][0]
    assert winner2 == "100050"         # within-quarter version handling

    # ---- normalization ----
    (unknown_drugs,) = _q(test_db_url,
        "SELECT count(*) FROM case_drugs WHERE drug_normalized = '(UNKNOWN)'")[0]
    assert unknown_drugs == 1
    (metformin,) = _q(test_db_url,
        "SELECT count(*) FROM case_drugs WHERE drug_normalized = 'METFORMIN'")[0]
    assert metformin >= 1              # alias applied (METFORMIN HYDROCHLORIDE)
    (ages,) = _q(test_db_url, "SELECT count(*) FROM cases WHERE age_years IS NULL")[0]
    assert ages == 2                   # 100002 missing + 100004 out-of-range

    # ---- orphans / duplicates never loaded ----
    (orphan_drug,) = _q(test_db_url,
        "SELECT count(*) FROM case_drugs WHERE primaryid = '999999'")[0]
    assert orphan_drug == 0
    (orphan_event,) = _q(test_db_url,
        "SELECT count(*) FROM case_events WHERE primaryid = '888888'")[0]
    assert orphan_event == 0

    # ---- drug rows: 6 (q1) + 2 (q2) = 8 ----
    (drugs,) = _q(test_db_url, "SELECT count(*) FROM case_drugs")[0]
    assert drugs == 8
    (events,) = _q(test_db_url, "SELECT count(*) FROM case_events")[0]
    assert events == 8

    # ---- ingest bookkeeping ----
    (runs,) = _q(test_db_url,
        "SELECT count(*) FROM ingest_runs WHERE status = 'succeeded'")[0]
    assert runs >= 1
    (reports,) = _q(test_db_url, "SELECT count(*) FROM data_quality_reports")[0]
    assert reports >= 1
    report_json = _q(test_db_url,
        "SELECT report FROM data_quality_reports ORDER BY report_id DESC LIMIT 1")[0][0]
    report = report_json if isinstance(report_json, dict) else json.loads(report_json)
    assert report["stage_totals"]["demo_raw_rows"] == 9   # 7 + 2
    assert report["database_counts"]["cases_current"] == 5

    latest = project_root / "data" / "processed" / "data_quality_report_latest.json"
    assert latest.exists()


def test_partial_run_does_not_demote_other_quarters(test_db_url, tmp_path):
    """Regression: a run limited to one quarter must NOT mark cases from
    other loaded quarters stale (dedup must always see all quarters).

    Fixture copies live at tmp/2024q1 and tmp/2024q2 so directory
    resolution exercises the real raw-root fallback.
    """
    import shutil
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.run_ingestion import main

    root = Path(__file__).resolve().parents[2]
    for q in ("2024q1", "2024q2"):
        dst = tmp_path / q
        shutil.copytree(root / "data" / "samples" / f"fixture_{q}", dst)

    # Full load of both quarters from the temp raw root
    rc = main(["--quarters", "2024q1,2024q2", "--raw-root", str(tmp_path),
               "--database-url", test_db_url])
    assert rc == 0
    before = _q(test_db_url,
                "SELECT count(*) FROM cases WHERE is_current")[0][0]
    assert before == 5
    winner_q2 = _q(test_db_url,
                   "SELECT primaryid FROM cases WHERE case_id = '90002' "
                   "AND is_current")[0][0]
    assert winner_q2 == "200001"

    # Partial run: ONLY 2024q1 in scope — before the fix this marked all
    # 2024q2 cases stale (current dropped from 5 to 3).
    rc = main(["--quarters", "2024q1", "--raw-root", str(tmp_path),
               "--database-url", test_db_url])
    assert rc == 0
    after = _q(test_db_url,
               "SELECT count(*) FROM cases WHERE is_current")[0][0]
    assert after == 5                      # q2 cases NOT demoted
    winner_q2_after = _q(test_db_url,
                         "SELECT primaryid FROM cases WHERE case_id = '90002' "
                         "AND is_current")[0][0]
    assert winner_q2_after == "200001"

    # And the q1 winner is still correct after the partial re-run
    winner_q1 = _q(test_db_url,
                   "SELECT primaryid FROM cases WHERE case_id = '90001' "
                   "AND is_current")[0][0]
    assert winner_q1 == "100050"


def test_missing_demo_files_for_loaded_quarter_fails_loudly(test_db_url, tmp_path,
                                                            fixture_q1):
    """Dedup must never silently proceed with an incomplete record set."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.run_ingestion import main

    # In-scope quarter has files; the OTHER loaded quarter (2024q2) does not
    # exist under this raw root -> dedup must refuse loudly.
    with pytest.raises(RuntimeError, match="Deduplication needs DEMO files"):
        main(["--quarter-dirs", f"2024q1={fixture_q1}",
              "--raw-root", str(tmp_path),
              "--database-url", test_db_url])


def test_ingestion_is_idempotent(test_db_url):
    """Re-running ingestion must not duplicate data (spec §7 measurable, not lossy)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.run_ingestion import main

    before = {
        "cases": _q(test_db_url, "SELECT count(*) FROM cases")[0][0],
        "drugs": _q(test_db_url, "SELECT count(*) FROM case_drugs")[0][0],
        "events": _q(test_db_url, "SELECT count(*) FROM case_events")[0][0],
    }
    rc = main(["--fixtures", "--database-url", test_db_url])
    assert rc == 0
    after = {
        "cases": _q(test_db_url, "SELECT count(*) FROM cases")[0][0],
        "drugs": _q(test_db_url, "SELECT count(*) FROM case_drugs")[0][0],
        "events": _q(test_db_url, "SELECT count(*) FROM case_events")[0][0],
    }
    assert before == after


def test_crash_mid_run_keeps_completed_quarters_report(test_db_url, fixture_q1,
                                                       tmp_path):
    """Regression: a client killed between quarters used to lose the quality
    report entirely even though loaded quarters' data stayed in the database.

    Now the report is persisted after EVERY quarter (status='partial') and the
    run is marked failed; the completed quarter's stats survive.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.run_ingestion import main

    # 2024q2's directory doesn't exist -> the run crashes AFTER 2024q1 loaded
    with pytest.raises(FileNotFoundError):
        main(["--quarter-dirs", f"2024q1={fixture_q1},2024q2={tmp_path / 'nope'}",
              "--skip-dedup", "--database-url", test_db_url])

    # data of the completed quarter is in the database
    (cases,) = _q(test_db_url,
                  "SELECT count(*) FROM cases WHERE source_quarter = '2024q1'")[0]
    assert cases == 5

    # the run is marked failed with an error
    run_id, status, error = _q(
        test_db_url,
        "SELECT run_id, status, error FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
    )[0]
    assert status == "failed"
    assert "nope" in (error or "")

    # and the completed quarter's quality stats were NOT lost
    report = _q(test_db_url,
                "SELECT report FROM data_quality_reports WHERE run_id = %s",
                (run_id,))[0][0]
    if not isinstance(report, dict):
        report = json.loads(report)
    assert report["status"] == "partial"
    assert report["quarters"] == ["2024q1"]
    assert report["stage_totals"]["demo_raw_rows"] == 7


def test_orphaned_running_run_recovered_on_next_start(test_db_url):
    """A 'running' row left by SIGKILL must be marked failed on next start
    (advisory lock is held, so no live run can exist)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.run_ingestion import main

    conn = psycopg2.connect(test_db_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (quarters, status) "
            "VALUES (%s, 'running') RETURNING run_id",
            (["2024q1"],),
        )
        zombie_id = cur.fetchone()[0]
    conn.close()

    rc = main(["--fixtures", "--database-url", test_db_url])
    assert rc == 0

    status, error = _q(
        test_db_url,
        "SELECT status, error FROM ingest_runs WHERE run_id = %s",
        (zombie_id,),
    )[0]
    assert status == "failed"
    assert "orphaned" in error


def test_concurrent_ingestion_refused_while_lock_held(test_db_url, fixture_q1):
    """Single-writer pipeline (spec §7): a second run must refuse, not race."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.run_ingestion import INGEST_LOCK_ID, main

    holder = psycopg2.connect(test_db_url)
    holder.autocommit = True
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK_ID,))
    try:
        before = _q(test_db_url, "SELECT count(*) FROM ingest_runs")[0][0]
        with pytest.raises(SystemExit, match="Another ingestion run is active"):
            main(["--quarter-dirs", f"2024q1={fixture_q1}",
                  "--skip-dedup", "--database-url", test_db_url])
        # nothing ran: no new ingest_runs row was created
        after = _q(test_db_url, "SELECT count(*) FROM ingest_runs")[0][0]
        assert after == before
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK_ID,))
        holder.close()
