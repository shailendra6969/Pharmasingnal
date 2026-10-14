from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _q(url: str, sql: str, params: tuple = ()):
    import psycopg2

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return row[0] if row and len(row) == 1 else row
    finally:
        conn.close()


def _raw_root_with_fixtures(test_db_url, tmp_path, project_root) -> Path:
    """Build a raw root that satisfies cross-quarter dedup.

    Deduplication needs DEMO files for EVERY quarter loaded in the database
    (spec §7), so the synthetic raw root gets a fixture link per loaded
    quarter. Unknown quarters fail loudly rather than silently skipping dedup.
    """
    loaded = _q(test_db_url, "SELECT array_agg(DISTINCT source_quarter) "
                             "FROM cases") or []
    root = tmp_path / "FAERS"
    root.mkdir(parents=True, exist_ok=True)
    for q in sorted(set(loaded) | {"2024q1", "2024q2"}):
        fixture = project_root / "data" / "samples" / f"fixture_{q}"
        assert fixture.exists(), (
            f"loaded quarter {q} has no fixture; extend this test's raw-root "
            f"builder before relying on its dedup assumptions")
        link = root / q
        if not link.exists():
            link.symlink_to(fixture, target_is_directory=True)
    return root


def test_refresh_quarter_end_to_end(test_db_url, tmp_path, project_root,
                                    monkeypatch):
    """Download (stubbed) -> ingest -> recompute signals, fully verified."""
    from app.faers.download import DownloadResult
    import scripts.refresh_quarter as refresh

    raw_root = _raw_root_with_fixtures(test_db_url, tmp_path, project_root)
    download_calls: list[str] = []

    def fake_download(quarter, raw_root_, **kwargs):
        download_calls.append(quarter)
        directory = Path(raw_root_) / quarter
        assert directory.is_dir(), f"expected extracted dir {directory}"
        return DownloadResult(
            quarter=quarter,
            archive=Path(raw_root_) / "archives" / f"ASCII-{quarter}.zip",
            extracted_dir=directory,
            extracted=["DEMO24q1.txt", "DRUG24q1.txt", "REAC24q1.txt"],
            skipped_existing=True,
            archive_sha256="0" * 64,
        )

    monkeypatch.setattr(refresh, "download_quarter", fake_download)

    report_path = tmp_path / "refresh.json"
    rc = refresh.main([
        "--quarter", "2024Q1",          # case is normalized, not rejected
        "--min-cases", "1",             # fixture pairs are tiny; keep them evaluable
        "--skip-concordance",           # covered by its own test below
        "--database-url", test_db_url,
        "--raw-root", str(raw_root),
        "--report", str(report_path),
    ])
    assert rc == 0
    assert download_calls == ["2024q1"]

    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "succeeded"
    assert report["quarter"] == "2024q1"
    assert set(report["stages"]) == {
        "download", "ingest", "signals", "concordance", "regimes",
        "profiles", "confounding",
    }
    assert report["stages"]["concordance"]["status"] == "skipped"
    assert report["stages"]["download"]["archive_sha256"] == "0" * 64

    # ---- regime stage: ran on the refreshed screen, shape is fixed ----
    reg = report["stages"]["regimes"]
    assert reg["status"] == "succeeded"
    assert isinstance(reg["candidates_evaluated"], int)
    assert isinstance(reg["flags"], list)
    assert set(reg["flag_counts"]) == {"bulk_single_event", "notoriety_ramp"}
    if reg["candidates_evaluated"] == 0:
        # fixture screen passes no PRR/ROR screen -> nothing to audit
        assert reg["flags"] == [] and reg["evaluated_quarter"] is None
    else:
        for f in reg["flags"]:
            assert f["drug"] and f["event"] and f["quarter"]
            assert f["flag"] in reg["flag_counts"]
            assert f["reason"]

    # ---- profile verdicts stage ----
    prof = report["stages"]["profiles"]
    assert prof["status"] == "succeeded"
    assert isinstance(prof["signals_evaluated"], int)
    assert isinstance(prof["flagged"], list)
    assert isinstance(prof["verdict_counts"], dict)

    # ---- confounding probe stage ----
    conf = report["stages"]["confounding"]
    assert conf["status"] == "succeeded"
    assert isinstance(conf["summary"], dict)
    assert "probed" in conf["summary"]
    assert isinstance(conf["flagged"], list)

    # ---- ingest stage: data landed + dedup invariant held ----
    ing = report["stages"]["ingest"]
    assert ing["case_rows"] > 0
    assert ing["current_cases"] > 0
    assert _q(test_db_url,
              "SELECT count(*) FROM cases WHERE source_quarter = '2024q1'") > 0

    # ---- signals stage: persisted state matches what the engine reported ----
    sig = report["stages"]["signals"]["modes"]
    assert set(sig) == {"all", "primary_suspect"}
    for mode, ps_only in (("all", False), ("primary_suspect", True)):
        st = sig[mode]
        assert st["background_n"] == ing["background_current_cases"]
        assert st["score_rows"] > 0            # min-cases=1 keeps pairs evaluable
        assert _q(test_db_url,
                  "SELECT count(*) FROM signal_scores "
                  "WHERE primary_suspect_mode = %s", (ps_only,)) == st["score_rows"]
        assert _q(test_db_url,
                  "SELECT count(*) FROM signal_quarterly "
                  "WHERE primary_suspect_mode = %s", (ps_only,)) == st["quarterly_rows"]
        # every stored score row must carry the live background N
        assert _q(test_db_url,
                  "SELECT count(DISTINCT background_n) FROM signal_scores "
                  "WHERE primary_suspect_mode = %s", (ps_only,)) == 1


def test_refresh_quarter_invalid_quarter_rejected(test_db_url, tmp_path,
                                                  monkeypatch):
    """Malformed quarters exit 2 before any stage runs."""
    import scripts.refresh_quarter as refresh

    monkeypatch.setattr(
        refresh, "download_quarter",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))

    rc = refresh.main(["--quarter", "2024-q1",
                       "--database-url", test_db_url,
                       "--report", str(tmp_path / "r.json")])
    assert rc == 2
    assert not (tmp_path / "r.json").exists()


def test_refresh_quarter_download_failure_aborts(test_db_url, tmp_path,
                                                 monkeypatch):
    """A failed download stops the refresh before ingest or signals."""
    import scripts.refresh_quarter as refresh

    def boom(*args, **kwargs):
        raise RuntimeError("HTTP 404 from all URL patterns (not published yet?)")

    monkeypatch.setattr(refresh, "download_quarter", boom)

    rc = refresh.main(["--quarter", "2030q1",
                       "--database-url", test_db_url,
                       "--report", str(tmp_path / "r.json")])
    assert rc == 1
    assert not (tmp_path / "r.json").exists()
    assert _q(test_db_url,
              "SELECT count(*) FROM cases WHERE source_quarter = '2030q1'") == 0


def test_refresh_quarter_skip_download_missing_dir_fails(test_db_url, tmp_path):
    """--skip-download on an absent directory fails loudly, not silently."""
    import scripts.refresh_quarter as refresh

    rc = refresh.main(["--quarter", "2030q1", "--skip-download",
                       "--database-url", test_db_url,
                       "--raw-root", str(tmp_path / "nowhere"),
                       "--report", str(tmp_path / "r.json")])
    assert rc == 1
    assert not (tmp_path / "r.json").exists()


def test_refresh_quarter_quarter_dir_records_skipped_download(
        test_db_url, tmp_path, project_root):
    """--quarter-dir with an existing directory records the download stage
    as skipped instead of crashing with KeyError 'download' (regression:
    the report dict was only populated by the download branch)."""
    import scripts.refresh_quarter as refresh

    raw_root = _raw_root_with_fixtures(test_db_url, tmp_path, project_root)
    fixture = project_root / "data" / "samples" / "fixture_2024q1"
    report_path = tmp_path / "refresh.json"
    rc = refresh.main([
        "--quarter", "2024q1", "--min-cases", "1",
        "--quarter-dir", str(fixture),
        "--skip-concordance",           # covered by its own tests
        "--database-url", test_db_url,
        "--raw-root", str(raw_root),
        "--report", str(report_path),
    ])
    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "succeeded"
    dl = report["stages"]["download"]
    assert dl["status"] == "skipped"
    assert dl["reason"] == "--quarter-dir given"
    assert dl["extract_dir"] == str(fixture)
    assert dl["files"]                  # fixture dir was validated, not ignored
    # the data still landed through the quarter-dir ingest path
    assert _q(test_db_url,
              "SELECT count(*) FROM cases WHERE source_quarter = '2024q1'") > 0


def test_refresh_quarter_skip_download_records_stage(test_db_url, tmp_path,
                                                     project_root):
    """--skip-download on an existing directory also records a skipped
    download stage (same KeyError class as --quarter-dir)."""
    import scripts.refresh_quarter as refresh

    raw_root = _raw_root_with_fixtures(test_db_url, tmp_path, project_root)
    report_path = tmp_path / "refresh.json"
    rc = refresh.main([
        "--quarter", "2024q2", "--min-cases", "1",
        "--skip-download",
        "--skip-concordance",
        "--database-url", test_db_url,
        "--raw-root", str(raw_root),
        "--report", str(report_path),
    ])
    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dl = report["stages"]["download"]
    assert dl["status"] == "skipped"
    assert dl["reason"] == "--skip-download"
    assert dl["extract_dir"] == str(raw_root / "2024q2")
    assert _q(test_db_url,
              "SELECT count(*) FROM cases WHERE source_quarter = '2024q2'") > 0


def _reference_dir(tmp_path: Path) -> Path:
    """Tiny SIDER-style reference covering both fixture drugs."""
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "drug_names.tsv").write_text(
        "CID1\tmetformin\nCID2\tibuprofen\n", encoding="utf-8")
    with gzip.open(ref / "meddra_all_se.tsv.gz", "wt", encoding="utf-8") as f:
        f.write("CID1\tU1\tx\tPT\t1\tNausea\n")   # fixture event exists
        f.write("CID2\tU2\tx\tPT\t2\tRash\n")
    with gzip.open(ref / "meddra_all_indications.tsv.gz", "wt",
                   encoding="utf-8") as f:
        f.write("CID1\tU1\tx\ty\tPT\t3\tType 2 diabetes mellitus\n")
    return ref


def test_refresh_quarter_concordance_delta(test_db_url, tmp_path, project_root,
                                            monkeypatch):
    """Stage 4 reruns concordance and records the numeric delta.

    First run has no previous report -> delta is None; second run diffs
    against the first run's stored summary (identical screen -> zero deltas).
    """
    from app.faers.download import DownloadResult
    import scripts.refresh_quarter as refresh

    raw_root = _raw_root_with_fixtures(test_db_url, tmp_path, project_root)

    def fake_download(quarter, raw_root_, **kwargs):
        return DownloadResult(
            quarter=quarter,
            archive=Path(raw_root_) / "archives" / f"ASCII-{quarter}.zip",
            extracted_dir=Path(raw_root_) / quarter,
            extracted=["DEMO24q1.txt", "DRUG24q1.txt", "REAC24q1.txt"],
            skipped_existing=True, archive_sha256="0" * 64)

    monkeypatch.setattr(refresh, "download_quarter", fake_download)
    ref = _reference_dir(tmp_path)
    report_path = tmp_path / "refresh.json"
    argv = ["--quarter", "2024q1", "--min-cases", "1",
            "--database-url", test_db_url,
            "--raw-root", str(raw_root),
            "--reference-dir", str(ref),
            "--concordance-top", "10,20",
            "--report", str(report_path)]

    # ---- first run: no previous report -> delta is None ----
    assert refresh.main(argv) == 0
    st1 = json.loads(report_path.read_text(encoding="utf-8")) \
        ["stages"]["concordance"]
    assert st1["status"] == "succeeded"
    assert st1["reference"]["label_pairs"] == 2
    assert st1["delta_vs_previous"]["changes"] is None
    # fixture pairs co-occur in the data (min-cases=1) -> evaluable, unscreened
    allm = st1["modes"]["all"]
    assert allm["evaluable_pairs"] == 2
    assert allm["recall_prr"] == 0.0
    assert allm["top"] is None       # no pair passes the PRR screen

    # ---- second run: delta computed against the first run ----
    assert refresh.main(argv) == 0
    st2 = json.loads(report_path.read_text(encoding="utf-8")) \
        ["stages"]["concordance"]
    delta = st2["delta_vs_previous"]
    assert delta["previous"] and "previous refresh report" in delta["previous"]
    ch = delta["changes"]
    assert ch["all"]["evaluable_pairs"] == {"previous": 2, "current": 2,
                                             "delta": 0}
    assert ch["all"]["recall_prr"]["delta"] == 0.0
    assert ch["primary_suspect"]["evaluable_pairs"]["delta"] == 0


def test_refresh_quarter_missing_reference_skips_concordance(
        test_db_url, tmp_path, project_root, monkeypatch):
    """Absent SIDER files (not committed) skip stage 4; refresh still
    succeeds and the skip is recorded with a reason."""
    from app.faers.download import DownloadResult
    import scripts.refresh_quarter as refresh

    raw_root = _raw_root_with_fixtures(test_db_url, tmp_path, project_root)

    def fake_download(quarter, raw_root_, **kwargs):
        return DownloadResult(
            quarter=quarter,
            archive=Path(raw_root_) / "archives" / f"ASCII-{quarter}.zip",
            extracted_dir=Path(raw_root_) / quarter,
            extracted=["DEMO24q1.txt", "DRUG24q1.txt", "REAC24q1.txt"],
            skipped_existing=True, archive_sha256="0" * 64)

    monkeypatch.setattr(refresh, "download_quarter", fake_download)
    report_path = tmp_path / "refresh.json"
    rc = refresh.main([
        "--quarter", "2024q1", "--min-cases", "1",
        "--database-url", test_db_url,
        "--raw-root", str(raw_root),
        "--reference-dir", str(tmp_path / "no_sider_here"),
        "--report", str(report_path),
    ])
    assert rc == 0
    st = json.loads(report_path.read_text(encoding="utf-8")) \
        ["stages"]["concordance"]
    assert st["status"] == "skipped"
    assert "missing" in st["reason"] or "Reference files missing" in st["reason"]


def test_refresh_quarter_skip_flags(test_db_url, tmp_path, project_root, monkeypatch):
    """--skip-regimes, --skip-profiles, --skip-confounding record skipped status."""
    from app.faers.download import DownloadResult
    import scripts.refresh_quarter as refresh

    raw_root = _raw_root_with_fixtures(test_db_url, tmp_path, project_root)

    def fake_download(quarter, raw_root_, **kwargs):
        return DownloadResult(
            quarter=quarter,
            archive=Path(raw_root_) / "archives" / f"ASCII-{quarter}.zip",
            extracted_dir=Path(raw_root_) / quarter,
            extracted=["DEMO24q1.txt", "DRUG24q1.txt", "REAC24q1.txt"],
            skipped_existing=True,
            archive_sha256="0" * 64,
        )

    monkeypatch.setattr(refresh, "download_quarter", fake_download)

    report_path = tmp_path / "refresh_skips.json"
    rc = refresh.main([
        "--quarter", "2024q1",
        "--min-cases", "1",
        "--skip-concordance",
        "--skip-regimes",
        "--skip-profiles",
        "--skip-confounding",
        "--database-url", test_db_url,
        "--raw-root", str(raw_root),
        "--report", str(report_path),
    ])
    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "succeeded"
    stages = report["stages"]
    assert stages["concordance"]["status"] == "skipped"
    assert stages["concordance"]["reason"] == "--skip-concordance"
    assert stages["regimes"]["status"] == "skipped"
    assert stages["regimes"]["reason"] == "--skip-regimes"
    assert stages["profiles"]["status"] == "skipped"
    assert stages["profiles"]["reason"] == "--skip-profiles"
    assert stages["confounding"]["status"] == "skipped"
    assert stages["confounding"]["reason"] == "--skip-confounding"

