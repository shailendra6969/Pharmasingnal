"""Tests for the indication-bias probe (app/statistics/confounding.py).

Hand-computed fixture (all current cases; probe denominators = current
versions only). SIDER indications cover VALBIASA only, via --reference:
(VALBIASA, VALEVENTX). All other drugs use GENERIC_DISEASE_TERMS fallback.

    VALBIASA / AEVENT1 (SIDER indication VALEVENTX):
        pair cases A02-A05 (A02-A04 also VALEVENTX)  -> pair 3/4  = 0.75
        drug cases A01-A08 (A01-A04 VALEVENTX)        -> base  4/8 = 0.50
        enrichment 1.5 == threshold -> FLAGGED (co-event)
    VALBIASB / BEVENT1 (not in SIDER; B05 carries VALEVENTX which is NOT
        B's indication term — terms are drug-keyed):
        pair 0/4, base 0/5 -> enrichment None -> not flagged
    VALBIASC / CONDITION AGGRAVATED (self-exclusion):
        every case self-carries the term; pair hits must be 0 -> not flagged
    VALBIASD / DEAVENT1 (treatment-failure):
        pair D01-D03 carry a failure/util term -> 3/4 = 0.75
        drug D01-D09                            -> 3/9 = 0.3333
        enrichment 2.25 -> FLAGGED (treatment-failure)
    VALBIASE / EAVENT1 (SIDER-missing drug, fallback term):
        pair E01-E03 carry DERMATITIS ATOPIC -> 3/3 = 1.0
        drug E01-E08                          -> 3/8 = 0.375
        enrichment 2.667 -> FLAGGED (co-event)
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

import scripts.probe_confounding as pc  # noqa: E402
from app.statistics.confounding import probe_signals  # noqa: E402

SIGNALS = [
    ("VALBIASA", "AEVENT1", 100, 3.0),
    ("VALBIASB", "BEVENT1", 80, 2.5),
    ("VALBIASC", "CONDITION AGGRAVATED", 60, 2.0),
    ("VALBIASD", "DEAEVENT1", 40, 2.0),
    ("VALBIASE", "EAVENT1", 30, 2.0),
]
INDICATION_PAIRS = {("VALBIASA", "VALEVENTX")}

_CASES = {
    "A01": ("VALBIASA", ["VALEVENTX"]),
    "A02": ("VALBIASA", ["AEVENT1", "VALEVENTX"]),
    "A03": ("VALBIASA", ["AEVENT1", "VALEVENTX"]),
    "A04": ("VALBIASA", ["AEVENT1", "VALEVENTX"]),
    "A05": ("VALBIASA", ["AEVENT1"]),
    "A06": ("VALBIASA", ["OTHERTERM"]),
    "A07": ("VALBIASA", ["OTHERTERM"]),
    "A08": ("VALBIASA", ["OTHERTERM"]),
    "B01": ("VALBIASB", ["BEVENT1"]),
    "B02": ("VALBIASB", ["BEVENT1"]),
    "B03": ("VALBIASB", ["BEVENT1"]),
    "B04": ("VALBIASB", ["BEVENT1", "SOMETERM"]),
    "B05": ("VALBIASB", ["VALEVENTX"]),
    "C01": ("VALBIASC", ["CONDITION AGGRAVATED"]),
    "C02": ("VALBIASC", ["CONDITION AGGRAVATED"]),
    "C03": ("VALBIASC", ["CONDITION AGGRAVATED"]),
    "D01": ("VALBIASD", ["DEAEVENT1", "DRUG INEFFECTIVE"]),
    "D02": ("VALBIASD", ["DEAEVENT1", "DRUG INEFFECTIVE"]),
    "D03": ("VALBIASD", ["DEAEVENT1", "OFF LABEL USE"]),
    "D04": ("VALBIASD", ["DEAEVENT1"]),
    "D05": ("VALBIASD", ["OTHERTERM"]),
    "D06": ("VALBIASD", ["OTHERTERM"]),
    "D07": ("VALBIASD", ["OTHERTERM"]),
    "D08": ("VALBIASD", ["OTHERTERM"]),
    "D09": ("VALBIASD", ["OTHERTERM"]),
    "E01": ("VALBIASE", ["EAVENT1", "DERMATITIS ATOPIC"]),
    "E02": ("VALBIASE", ["EAVENT1", "DERMATITIS ATOPIC"]),
    "E03": ("VALBIASE", ["EAVENT1", "DERMATITIS ATOPIC"]),
    "E04": ("VALBIASE", ["OTHERTERM"]),
    "E05": ("VALBIASE", ["OTHERTERM"]),
    "E06": ("VALBIASE", ["OTHERTERM"]),
    "E07": ("VALBIASE", ["OTHERTERM"]),
    "E08": ("VALBIASE", ["OTHERTERM"]),
}

_SCORE_COLS = ("drug", "event", "period", "a", "b", "c", "d", "case_count",
               "prr", "chi_square", "primary_suspect_mode",
               "prr_screen", "ror_screen")


def _seed(conn) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM signal_scores")  # ranked query scans every row
    execute_values(cur, """
        INSERT INTO cases (primaryid, case_id, source_quarter, is_current)
        VALUES %s
    """, [(f"VALCONF{k}", f"VCASE{k}", "2099q2", True) for k in _CASES])
    execute_values(cur, """
        INSERT INTO case_drugs (primaryid, case_id, drug_normalized, role)
        VALUES %s
    """, [(f"VALCONF{k}", f"VCASE{k}", drug, "PS")
          for k, (drug, _) in _CASES.items()])
    execute_values(cur, """
        INSERT INTO case_events (primaryid, case_id, event_term)
        VALUES %s
    """, [(f"VALCONF{k}", f"VCASE{k}", ev)
          for k, (_, evs) in _CASES.items() for ev in evs])
    execute_values(cur, f"""
        INSERT INTO signal_scores ({', '.join(_SCORE_COLS)})
        VALUES %s
    """, [(d, e, "all", c, 10, 5, 1000, c, p, 10.0, False, True, True)
          for d, e, c, p in SIGNALS])
    conn.commit()
    cur.close()


def _cleanup(conn) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM cases WHERE primaryid LIKE 'VALCONF%'")
    cur.execute("DELETE FROM signal_scores")
    conn.commit()
    cur.close()


@pytest.fixture()
def probe_db(test_db_url):
    conn = psycopg2.connect(test_db_url)
    conn.autocommit = False
    _cleanup(conn)   # order-independent start
    _seed(conn)
    yield conn
    conn.close()
    _cleanup(psycopg2.connect(test_db_url))


def _sider_reference_dir(tmp_path: Path) -> Path:
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "drug_names.tsv").write_text("CID1\tvalbiasa\n", encoding="utf-8")
    with gzip.open(ref / "meddra_all_se.tsv.gz", "wt", encoding="utf-8") as f:
        f.write("CID1\tU1\tx\tPT\t1\tHeadache\n")
    with gzip.open(ref / "meddra_all_indications.tsv.gz", "wt",
                   encoding="utf-8") as f:
        f.write("CID1\tU1\tx\ty\tPT\t2\tValeventx\n")
    return ref


def test_probe_hand_computed_enrichments(probe_db):
    """Both dimensions, thresholds, self-exclusion, drug-keyed terms."""
    scores = probe_signals(probe_db, SIGNALS, indication_pairs=INDICATION_PAIRS)
    by = {(s.drug, s.event): s for s in scores}
    assert len(by) == 5

    a = by[("VALBIASA", "AEVENT1")]
    assert (a.pair_cases, a.drug_cases) == (4, 8)
    assert (a.coevent.pair_hits, a.coevent.base_hits) == (3, 4)
    assert a.coevent.share_pair == 0.75 and a.coevent.share_base == 0.5
    assert a.coevent.enrichment == 1.5          # exactly at the threshold
    assert a.flagged and "co-event" in a.reasons[0]
    assert not any("treatment-failure" in r for r in a.reasons)

    b = by[("VALBIASB", "BEVENT1")]
    # B05 carries VALEVENTX but it is not B's indication: terms are drug-keyed
    assert b.coevent.pair_hits == 0 and b.coevent.base_hits == 0
    assert b.coevent.enrichment is None and not b.flagged

    c = by[("VALBIASC", "CONDITION AGGRAVATED")]
    # 100% of cases self-carry the term; self must never count
    assert c.coevent.pair_hits == 0 and c.coevent.share_pair == 0.0
    assert not c.flagged

    d = by[("VALBIASD", "DEAEVENT1")]
    assert (d.treatment_failure.pair_hits, d.treatment_failure.base_hits) \
        == (3, 3)
    assert d.treatment_failure.share_pair == 0.75
    assert d.treatment_failure.share_base == 0.3333
    assert d.treatment_failure.enrichment == 2.25
    assert d.flagged and all("treatment-failure" in r for r in d.reasons)
    assert not d.flagged or "co-event" not in d.reasons[0]
    coevents = {x["event"]: x["cases"] for x in d.top_coevents}
    assert coevents.get("DRUG INEFFECTIVE") == 2
    assert coevents.get("OFF LABEL USE") == 1

    e = by[("VALBIASE", "EAVENT1")]
    # SIDER misses this drug entirely; GENERIC_DISEASE_TERMS still fires
    assert e.coevent.enrichment == 2.667
    assert e.flagged and "co-event" in e.reasons[0]


def test_probe_cli_report(probe_db, test_db_url, tmp_path):
    """CLI: ranked signals -> scored report with summary and flagged list."""
    ref = _sider_reference_dir(tmp_path)
    out = tmp_path / "confounding.json"
    rc = pc.main([
        "--database-url", test_db_url,
        "--reference-dir", str(ref),
        "--top", "10",
        "--output", str(out),
    ])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))

    assert report["summary"]["probed"] == 5
    assert report["summary"]["flagged"] == 3
    flagged = {(f["drug"], f["event"]) for f in report["flagged"]}
    assert flagged == {("VALBIASA", "AEVENT1"), ("VALBIASD", "DEAEVENT1"),
                       ("VALBIASE", "EAVENT1")}
    assert len(report["signals"]) == 5
    assert "SIDER indications" in report["reference"]["indication_terms"]
    assert "DERMATITIS ATOPIC" in report["terms"]["generic_disease_terms"]
    assert "DRUG INEFFECTIVE" in report["terms"]["treatment_failure_terms"]
    # flagged rows carry the audit co-events
    dupix = [f for f in report["flagged"] if f["drug"] == "VALBIASD"][0]
    assert dupix["top_coevents"]


def test_probe_cli_no_screened_signals_fails(probe_db, test_db_url, tmp_path):
    """An empty screen is an error, not a vacuous all-clear report."""
    cur = probe_db.cursor()
    cur.execute("UPDATE signal_scores SET prr_screen = FALSE")
    probe_db.commit()
    cur.close()
    rc = pc.main(["--database-url", test_db_url,
                  "--reference-dir", str(tmp_path / "missing"),
                  "--output", str(tmp_path / "r.json")])
    assert rc == 1
    assert not (tmp_path / "r.json").exists()
