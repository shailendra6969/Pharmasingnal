"""Tests for scripts/validate_concordance.py (label-concordance validation).

Two layers:
  * pure parsing tests for load_reference / load_pairs_file (no DB);
  * end-to-end tests against the test DB with synthetic signal_scores rows
    whose screen outcomes are hand-set, so every expected number below is
    derived by hand, not measured from a previous run.

Hand-computed end-to-end fixture (all mode, period='all'):

    reference pairs (--pairs TSV):
        (VALDRUGA, VALEVENT1)   drug+event in DB vocab, pair scored + screened
        (VALDRUGA, VALEVENT2)   drug+event in DB vocab, pair scored, NOT screened
        (VALDRUGB, VALEVENT3)   drug+event in DB vocab, pair NEVER scored
        (VALDRC,   VALEVENT1)   drug not in DB vocab at all
        (VALDRUGA, VALEVX)      event not in DB vocab at all

    -> label_pairs_total=5, both_sides_in_db_vocab=3,
       pairs_drug_not_in_db=1, pairs_event_not_in_db=1,
       evaluable=2 (rows present in signal_scores), prr_screened=1
       recall_prr=0.5, recall_ror=0.5, in_vocab_not_scored=3-2=1

    top-N over prr_screen rows by case_count DESC:
        (VALDRUGA, VALEVENT1) 100 cases  labeled match
        (VALDRUGB, VALEVENT2)  80 cases  indication (--indications TSV)
        (VALDRUGB, OFF LABEL USE) 70     utilization term -> non-adverse
        (VALDRUGA, VALEVENT3)  60 cases  judgable, unlabeled
        (VALDRUGC, CONDITION AGGRAVATED) 5  disease-flare term, drug NOT in
                                            the reference (SIDER blind spot)
    n=4: label_matches=1 judgable=4 concordance_judgable=0.25
         indication_events=1 non_adverse_event_signals=1 disease_flare=0
    n=5: disease_flare_signals=1 (flag fires without any reference coverage)
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.validate_concordance as vc  # noqa: E402


# ---------------------------------------------------------------------------
# pure parsing tests
# ---------------------------------------------------------------------------

def test_load_reference_keeps_pt_only_and_uppercases(tmp_path):
    """SIDER parser: PT terms kept, LLT dropped, names uppercased."""
    (tmp_path / "drug_names.tsv").write_text(
        "CID1\taspirin\nCID2\tibuprofen\n", encoding="utf-8")
    # meddra_all_se.tsv: [cid, umls, x, kind(LLT/PT), code, side_effect]
    with gzip.open(tmp_path / "meddra_all_se.tsv.gz", "wt",
                   encoding="utf-8") as f:
        f.write("CID1\tU1\tx\tPT\t1\tHeadache\n")
        f.write("CID1\tU1\tx\tLLT\t2\tHead ache\n")      # dropped: LLT
        f.write("CID2\tU2\tx\tPT\t3\tnausea\n")
        f.write("CIDX\tU3\tx\tPT\t4\tUnknown Effect\n")  # dropped: no drug name
    # meddra_all_indications.tsv: [cid, umls, x, y, kind, code, indication]
    with gzip.open(tmp_path / "meddra_all_indications.tsv.gz", "wt",
                   encoding="utf-8") as f:
        f.write("CID1\tU1\tx\ty\tPT\t5\tPain\n")
        f.write("CID1\tU1\tx\ty\tLLT\t6\tDropping pain\n")  # dropped: LLT

    labels, indications = vc.load_reference(tmp_path)
    assert labels == {("ASPIRIN", "HEADACHE"), ("IBUPROFEN", "NAUSEA")}
    assert indications == {("ASPIRIN", "PAIN")}


def test_load_reference_missing_files_lists_download_commands(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        vc.load_reference(tmp_path)
    msg = str(exc.value)
    assert "meddra_all_se.tsv.gz" in msg
    assert "curl -sL" in msg              # actionable, not just "missing"
    assert "sideeffects.embl.de" in msg


def test_load_pairs_file_skips_comments_and_blanks(tmp_path):
    pairs_tsv = tmp_path / "pairs.tsv"
    pairs_tsv.write_text(
        "# comment\n"
        "\n"
        "drugA\tevent one\n"
        "  drugB \t event two \tignored-third-column\n",
        encoding="utf-8")
    assert vc.load_pairs_file(pairs_tsv) == {
        ("DRUGA", "EVENT ONE"), ("DRUGB", "EVENT TWO")}


def test_main_with_empty_reference_returns_error(tmp_path, capsys):
    """An empty reference is an error, not a vacuous all-zero report."""
    pairs_tsv = tmp_path / "empty.tsv"
    pairs_tsv.write_text("# only comments\n", encoding="utf-8")
    rc = vc.main(["--pairs", str(pairs_tsv),
                  "--output", str(tmp_path / "out.json")])
    assert rc == 1
    assert not (tmp_path / "out.json").exists()
    assert "reference is empty" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# end-to-end concordance math
# ---------------------------------------------------------------------------

VAL_PRIMARYIDS = ["VALCONC01", "VALCONC02", "VALCONC03", "VALCONC04"]


def _seed_vocab(cur) -> None:
    """DB vocabulary seen by the script: drugs VALDRUGA/VALDRUGB/VALDRUGC
    and events VALEVENT1/2/3 + CONDITION AGGRAVATED. VALDRC (drug) and
    VALEVX (event) intentionally stay outside the vocabulary. VALDRUGC is
    also absent from the reference files — the SIDER blind-spot case."""
    execute_values(cur, """
        INSERT INTO cases (primaryid, case_id, source_quarter, is_current)
        VALUES %s
    """, [(p, f"VCASE{p[-2:]}", "2099q1", True) for p in VAL_PRIMARYIDS])
    execute_values(cur, """
        INSERT INTO case_drugs (primaryid, case_id, drug_normalized, role)
        VALUES %s
    """, [("VALCONC01", "VCASE01", "VALDRUGA", "PS"),
          ("VALCONC02", "VCASE02", "VALDRUGB", "PS"),
          ("VALCONC03", "VCASE03", "VALDRUGA", "PS"),
          ("VALCONC04", "VCASE04", "VALDRUGC", "PS")])
    execute_values(cur, """
        INSERT INTO case_events (primaryid, case_id, event_term)
        VALUES %s
    """, [("VALCONC01", "VCASE01", "VALEVENT1"),
          ("VALCONC02", "VCASE02", "VALEVENT2"),
          ("VALCONC03", "VCASE03", "VALEVENT3"),
          ("VALCONC04", "VCASE04", "CONDITION AGGRAVATED")])


_SCORE_COLS = ("drug", "event", "period", "a", "b", "c", "d", "case_count",
               "prr", "chi_square", "primary_suspect_mode",
               "prr_screen", "ror_screen")


def _seed_scores(cur) -> None:
    rows = [
        # all-mode rows (period 'all', primary_suspect_mode=False)
        ("VALDRUGA", "VALEVENT1", "all", 50, 10, 5, 935, 50, 4.0, 22.0,
         False, True, True),
        ("VALDRUGA", "VALEVENT2", "all", 40, 10, 5, 945, 40, 3.5, 18.0,
         False, False, False),
        ("VALDRUGB", "VALEVENT2", "all", 30, 10, 5, 955, 30, 3.0, 12.0,
         False, True, True),
        ("VALDRUGB", "OFF LABEL USE", "all", 20, 10, 5, 965, 20, 2.5, 8.0,
         False, True, True),
        ("VALDRUGA", "VALEVENT3", "all", 10, 10, 5, 975, 10, 2.2, 6.0,
         False, True, True),
        # disease-flare row: drug absent from the reference entirely, so the
        # SIDER-based indication flag can never see it; the flare flag must
        # (case_count 5 keeps it below the n=4 head above)
        ("VALDRUGC", "CONDITION AGGRAVATED", "all", 5, 10, 5, 985, 5, 2.1, 5.0,
         False, True, True),
        # primary-suspect-mode row: labeled pair screened there too
        ("VALDRUGA", "VALEVENT1", "all", 50, 10, 5, 935, 50, 4.0, 22.0,
         True, True, True),
    ]
    execute_values(cur, f"""
        INSERT INTO signal_scores ({', '.join(_SCORE_COLS)})
        VALUES %s
    """, rows)


def _seed_reference(tmp_path: Path) -> tuple[Path, Path]:
    pairs = tmp_path / "ref_pairs.tsv"
    pairs.write_text(
        "VALDRUGA\tVALEVENT1\n"
        "VALDRUGA\tVALEVENT2\n"
        "VALDRUGB\tVALEVENT3\n"
        "VALDRC\tVALEVENT1\n"
        "VALDRUGA\tVALEVX\n",
        encoding="utf-8")
    indications = tmp_path / "ref_indications.tsv"
    indications.write_text("VALDRUGB\tVALEVENT2\n", encoding="utf-8")
    return pairs, indications


def _cleanup(conn) -> None:
    """Remove this test's rows.

    signal_scores is emptied outright (not just VAL*): the top-N ranked query
    scans every prr_screen row, so any scores another test left behind would
    shift the exact top-N counts asserted here. No test persists scores it
    doesn't recompute, so emptying is safe and makes this order-independent.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM cases WHERE primaryid LIKE 'VALCONC%'")
    cur.execute("DELETE FROM signal_scores")
    conn.commit()
    cur.close()


@pytest.fixture()
def concordance_db(test_db_url):
    """Test DB seeded with the hand-designed synthetic screen."""
    conn = psycopg2.connect(test_db_url)
    conn.autocommit = False
    _cleanup(conn)  # idempotent start regardless of test order
    cur = conn.cursor()
    _seed_vocab(cur)
    _seed_scores(cur)
    conn.commit()
    cur.close()
    yield conn
    conn.close()
    _cleanup(psycopg2.connect(test_db_url))


def test_concordance_end_to_end(concordance_db, test_db_url, tmp_path):
    pairs, indications = _seed_reference(tmp_path)
    out = tmp_path / "concordance.json"
    rc = vc.main([
        "--database-url", test_db_url,
        "--pairs", str(pairs),
        "--indications", str(indications),
        "--top", "1,2,4,5",
        "--baseline-table", "",
        "--output", str(out),
    ])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))

    all_mode = report["modes"]["all"]
    assert all_mode["label_pairs_total"] == 5
    assert all_mode["pairs_both_sides_in_db_vocab"] == 3
    assert all_mode["pairs_drug_not_in_db"] == 1
    assert all_mode["pairs_event_not_in_db"] == 1
    assert all_mode["evaluable_pairs"] == 2
    assert all_mode["in_vocab_not_scored"] == 1
    assert all_mode["prr_screened"] == 1
    assert all_mode["recall_prr"] == 0.5
    assert all_mode["ror_screened"] == 1
    assert all_mode["recall_ror"] == 0.5

    by_n = {b["n"]: b for b in all_mode["top_signals"]}
    assert by_n[1]["label_matches"] == 1
    assert by_n[1]["concordance_judgable"] == 1.0
    assert by_n[1]["indication_events"] == 0
    assert by_n[1]["non_adverse_event_signals"] == 0

    assert by_n[2]["label_matches"] == 1
    assert by_n[2]["judgable_signals"] == 2
    assert by_n[2]["concordance_judgable"] == 0.5
    assert by_n[2]["indication_events"] == 1
    assert by_n[2]["non_adverse_event_signals"] == 0

    assert by_n[4]["signals_returned"] == 4
    assert by_n[4]["label_matches"] == 1
    assert by_n[4]["judgable_signals"] == 4
    assert by_n[4]["concordance_judgable"] == 0.25
    assert by_n[4]["concordance_all"] == 0.25
    assert by_n[4]["indication_events"] == 1
    assert by_n[4]["indication_share"] == 0.25
    assert by_n[4]["non_adverse_event_signals"] == 1
    assert by_n[4]["non_adverse_event_share"] == 0.25
    assert by_n[4]["disease_flare_signals"] == 0
    assert by_n[4]["disease_flare_share"] == 0.0

    # blind spot closed: flare term flagged although its drug (VALDRUGC) is
    # in NO reference file, so neither label matching nor the SIDER-based
    # indication flag could have caught it
    assert by_n[5]["signals_returned"] == 5
    assert by_n[5]["disease_flare_signals"] == 1
    assert by_n[5]["disease_flare_share"] == 0.2

    # primary-suspect mode: only the one seeded PS row, which is screened
    ps = report["modes"]["primary_suspect"]
    assert ps["evaluable_pairs"] == 1
    assert ps["recall_prr"] == 1.0
    assert ps["top_signals"][0]["label_matches"] == 1

    # --baseline-table '' -> comparison disabled, not attempted
    assert report["baseline"]["table"] == "(disabled)"
    assert "modes" not in report["baseline"] or not report["baseline"]["modes"]

    # audit examples: reader can see WHICH signals matched / didn't
    ex = all_mode["examples"]
    assert [(r["drug"], r["event"]) for r in ex["matched_label"]] \
        == [("VALDRUGA", "VALEVENT1")]
    assert ex["matched_label"][0]["case_count"] == 50
    unmatched = {(r["drug"], r["event"]) for r in ex["judgable_unmatched"]}
    assert unmatched == {
        ("VALDRUGB", "VALEVENT2"),
        ("VALDRUGB", "OFF LABEL USE"),
        ("VALDRUGA", "VALEVENT3"),
    }
    assert [(r["drug"], r["event"]) for r in ex["indication_events"]] \
        == [("VALDRUGB", "VALEVENT2")]
    # flare examples surface signals that matched no other category
    assert [(r["drug"], r["event"]) for r in ex["disease_flare"]] \
        == [("VALDRUGC", "CONDITION AGGRAVATED")]
    assert ex["disease_flare"][0]["case_count"] == 5


def test_baseline_snapshot_common_pairs(concordance_db, test_db_url,
                                        tmp_path):
    """Baseline snapshot: recall delta restricted to common pairs."""
    cur = concordance_db.cursor()
    # The common-pairs JOIN needs the pair in BOTH baseline and signal_scores.
    # VALEVENT3 is the labeled pair the fixture deliberately leaves unscored;
    # score it here (screened) so it becomes the "newly screened" case.
    cur.execute("""
        INSERT INTO signal_scores
            (drug, event, period, a, b, c, d, case_count, prr, chi_square,
             primary_suspect_mode, prr_screen)
        VALUES ('VALDRUGB', 'VALEVENT3', 'all', 15, 10, 5, 970, 15, 2.5, 9.0,
                false, true)
    """)
    # baseline rows for the SAME labeled pairs: ONE kept, TWO was screened
    # in the baseline but is not anymore -> dropped; THREE is newly screened
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_scores_conc_base (
            drug TEXT, event TEXT, period TEXT,
            primary_suspect_mode BOOLEAN NOT NULL DEFAULT FALSE,
            a INT, b INT, c INT, d INT, case_count INT,
            prr DOUBLE PRECISION, chi_square DOUBLE PRECISION,
            prr_screen BOOLEAN NOT NULL DEFAULT FALSE,
            ror_screen BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    cur.execute("TRUNCATE signal_scores_conc_base")
    execute_values(cur, """
        INSERT INTO signal_scores_conc_base
            (drug, event, period, case_count, prr, chi_square, prr_screen)
        VALUES %s
    """, [("VALDRUGA", "VALEVENT1", "all", 50, 4.0, 22.0, True),   # kept
          ("VALDRUGA", "VALEVENT2", "all", 40, 3.5, 18.0, True),   # dropped
          ("VALDRUGB", "VALEVENT3", "all", 15, 2.5, 9.0, False)])  # new
    concordance_db.commit()

    try:
        pairs, indications = _seed_reference(tmp_path)
        out = tmp_path / "concordance_baseline.json"
        rc = vc.main([
            "--database-url", test_db_url,
            "--pairs", str(pairs),
            "--indications", str(indications),
            "--baseline-table", "signal_scores_conc_base",
            "--output", str(out),
        ])
        assert rc == 0
        report = json.loads(out.read_text(encoding="utf-8"))
        common = report["baseline"]["common_pairs_all_mode"]
        # all three baseline rows join signal_scores on the same pairs
        # (VALEVENT2 and VALEVENT3 pairs exist as scored rows, VALEVENT1 too)
        assert common["common_evaluable_pairs"] == 3
        assert common["baseline_prr_screened"] == 2
        assert common["current_prr_screened"] == 2  # ONE + VALEVENT3
        assert common["baseline_recall"] == pytest.approx(0.6667, abs=1e-3)
        assert common["current_recall"] == pytest.approx(0.6667, abs=1e-3)
        assert common["kept_screened"] == 1     # VALEVENT1 screened in both
        assert common["dropped_screened"] == 1  # VALEVENT2 screened only in baseline
        assert common["newly_screened"] == 1    # VALEVENT3 screened only now
    finally:
        cur.execute("DROP TABLE IF EXISTS signal_scores_conc_base")
        concordance_db.commit()


def test_baseline_missing_table_is_skipped(concordance_db, test_db_url,
                                           tmp_path):
    pairs, indications = _seed_reference(tmp_path)
    out = tmp_path / "concordance_nobase.json"
    rc = vc.main([
        "--database-url", test_db_url,
        "--pairs", str(pairs),
        "--indications", str(indications),
        "--baseline-table", "signal_scores_does_not_exist",
        "--output", str(out),
    ])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "skipped" in report["baseline"]
    assert "signal_scores_does_not_exist" in report["baseline"]["skipped"]
    assert any("Baseline comparison unavailable" in n
               for n in report["notes"])
