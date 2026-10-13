"""Phase 2 integration tests (spec §9, §11, §12).

Synthetic population (loaded into quarters 2099q1/q2 so the engine's
--quarters filter isolates it exactly):

    200 current cases total:
      40  SYNDRUGA + SYNEVENTX   (30 PS role, 10 SS role)
      60  SYNDRUGA only          (40 PS, 20 SS)
      10  SYNEVENTX only
      90  neither

Full-background table: a=40 b=60 c=10 d=90 -> hand-calculated in
tests/unit/test_disproportionality.py: PRR 4.0 [2.1195, 7.5488],
ROR 6.0 [2.7890, 12.9080], chi2 22.4267, both screens pass.
Primary-suspect-only table (exposed = PS role): a=30 b=40 c=20 d=110
(the 10 SS co-occurrence cases are unexposed there) -> PRR = (30/70)/(20/130).
"""
from __future__ import annotations

import psycopg2
import pytest
from psycopg2.extras import execute_values

from app.statistics.engine import compute_signals
from app.statistics.trend import pair_quarterly_series, quarterly_volume


def _exec(cur, sql, params=()):
    cur.execute(sql, params)


def _load_synthetic(cur) -> None:
    """200 cases; D+E split across 2099q1 (20) and 2099q2 (20) for persistence."""
    rows_cases = []
    rows_drugs = []
    rows_events = []
    idx = 0

    def add_case(quarter: str) -> str:
        nonlocal idx
        idx += 1
        pid = f"SYN{idx:05d}"
        rows_cases.append((pid, f"SYNCASE{idx:05d}", quarter))
        return pid

    # 40 D+E: 20 in each quarter; 30 PS / 10 SS overall
    for i in range(40):
        q = "2099q1" if i < 20 else "2099q2"
        pid = add_case(q)
        role = "PS" if i < 30 else "SS"
        rows_drugs.append((pid, role))
        rows_events.append(pid)
    # 60 D only: 40 PS / 20 SS
    for i in range(60):
        pid = add_case("2099q1")
        rows_drugs.append((pid, "PS" if i < 40 else "SS"))
    # 10 E only
    for _ in range(10):
        pid = add_case("2099q2")
        rows_events.append(pid)
    # 90 neither
    for _ in range(90):
        add_case("2099q1")

    execute_values(cur, """
        INSERT INTO cases (primaryid, case_id, source_quarter, is_current)
        VALUES %s ON CONFLICT (primaryid) DO NOTHING
    """, [(p, c, q, True) for p, c, q in rows_cases])
    execute_values(cur, """
        INSERT INTO case_drugs (primaryid, case_id, drug_normalized, role)
        VALUES %s
    """, [(p, f"SYNCASE{p[3:]}", "SYNDRUGA", r) for p, r in rows_drugs])
    execute_values(cur, """
        INSERT INTO case_events (primaryid, case_id, event_term)
        VALUES %s
    """, [(p, f"SYNCASE{p[3:]}", "SYNEVENTX") for p in rows_events])


def _cleanup(cur) -> None:
    cur.execute("DELETE FROM case_events WHERE primaryid LIKE 'SYN%'")
    cur.execute("DELETE FROM case_drugs WHERE primaryid LIKE 'SYN%'")
    cur.execute("DELETE FROM cases WHERE primaryid LIKE 'SYN%'")
    cur.execute("""
        DELETE FROM signal_scores
        WHERE drug LIKE 'SYN%' OR event LIKE 'SYN%'
    """)


@pytest.fixture()
def signals_conn(test_db_url):
    """Connection to the test DB with fixtures ingested and synthetic removed."""
    from scripts.run_ingestion import main as ingest_main

    ingest_main(["--fixtures", "--database-url", test_db_url])
    conn = psycopg2.connect(test_db_url)
    conn.autocommit = False
    cur = conn.cursor()
    _cleanup(cur)
    conn.commit()
    yield conn
    cur = conn.cursor()
    _cleanup(cur)
    conn.commit()
    conn.close()


def _score(cur, drug, event, mode, period="all"):
    cur.execute("""
        SELECT a, b, c, d, case_count, prr, prr_lower, prr_upper,
               ror, ror_lower, ror_upper, chi_square,
               prr_screen, ror_screen, insufficient,
               quarters_observed, quarters_screening, background_n
        FROM signal_scores
        WHERE drug = %s AND event = %s AND period = %s
          AND primary_suspect_mode = %s
    """, (drug, event, period, mode))
    row = cur.fetchone()
    assert row is not None, f"no score row for {drug}/{event}/{period}/{mode}"
    keys = ("a", "b", "c", "d", "case_count", "prr", "prr_lower", "prr_upper",
            "ror", "ror_lower", "ror_upper", "chi_square",
            "prr_screen", "ror_screen", "insufficient",
            "quarters_observed", "quarters_screening", "background_n")
    return dict(zip(keys, row))


def test_engine_hand_calculated_full_background(signals_conn):
    cur = signals_conn.cursor()
    _load_synthetic(cur)
    signals_conn.commit()

    res = compute_signals(signals_conn, primary_suspect_only=False,
                          quarters=["2099q1", "2099q2"], min_cases=3)
    assert res.background_n == 200

    s = _score(cur, "SYNDRUGA", "SYNEVENTX", False)
    assert (s["a"], s["b"], s["c"], s["d"]) == (40, 60, 10, 90)
    assert s["case_count"] == 40
    assert s["background_n"] == 200
    # hand-calculated values (same as unit test)
    from pytest import approx
    assert s["prr"] == approx(4.0)
    assert s["prr_lower"] == approx(2.1195, abs=1e-3)
    assert s["prr_upper"] == approx(7.5488, abs=1e-3)
    assert s["ror"] == approx(6.0)
    assert s["ror_lower"] == approx(2.7890, abs=1e-3)
    assert s["ror_upper"] == approx(12.9080, abs=1e-3)
    assert s["chi_square"] == approx(22.4267, abs=1e-3)
    assert s["prr_screen"] is True
    assert s["ror_screen"] is True
    assert s["insufficient"] is False
    # persistence: 20 + 20 co-occurrences in 2 quarters, both above floor
    assert s["quarters_observed"] == 2
    assert s["quarters_screening"] == 2


def test_quarterly_series_rows_written(signals_conn):
    cur = signals_conn.cursor()
    _load_synthetic(cur)
    signals_conn.commit()
    compute_signals(signals_conn, primary_suspect_only=False,
                    quarters=["2099q1", "2099q2"], min_cases=3)

    cur.execute("""
        SELECT quarter, a, background_n FROM signal_quarterly
        WHERE drug = 'SYNDRUGA' AND event = 'SYNEVENTX'
          AND NOT primary_suspect_mode
        ORDER BY quarter
    """)
    rows = cur.fetchall()
    assert rows == [("2099q1", 20, 200), ("2099q2", 20, 200)]
    # volume rows live in signal_quarterly, not signal_scores (spec §12)
    cur.execute("""
        SELECT count(*) FROM signal_scores
        WHERE drug = 'SYNDRUGA' AND event = 'SYNEVENTX' AND period <> 'all'
    """)
    assert cur.fetchone()[0] == 0


def test_primary_suspect_sensitivity(signals_conn):
    """spec §11: sensitivity comparison, not a causality determination."""
    cur = signals_conn.cursor()
    _load_synthetic(cur)
    signals_conn.commit()

    compute_signals(signals_conn, primary_suspect_only=False,
                    quarters=["2099q1", "2099q2"], min_cases=3)
    compute_signals(signals_conn, primary_suspect_only=True,
                    quarters=["2099q1", "2099q2"], min_cases=3)

    all_mode = _score(cur, "SYNDRUGA", "SYNEVENTX", False)
    ps_mode = _score(cur, "SYNDRUGA", "SYNEVENTX", True)
    from pytest import approx
    # full background of suspect drugs: 40/60
    assert (all_mode["a"], all_mode["b"]) == (40, 60)
    # primary suspect only: a=30 (PS rows among D+E), b=40 (PS among D only)
    # the 10 SS-role co-occurrence cases are unexposed here -> c=50-30=20
    assert (ps_mode["a"], ps_mode["b"]) == (30, 40)
    assert (ps_mode["c"], ps_mode["d"]) == (20, 110)
    assert ps_mode["prr"] == approx((30 / 70) / (20 / 130))
    # both rows coexist for comparison
    cur.execute("""
        SELECT count(*) FROM signal_scores
        WHERE drug = 'SYNDRUGA' AND event = 'SYNEVENTX' AND period = 'all'
    """)
    assert cur.fetchone()[0] == 2


def test_reproducible_results(signals_conn):
    cur = signals_conn.cursor()
    _load_synthetic(cur)
    signals_conn.commit()
    kwargs = dict(primary_suspect_only=False,
                  quarters=["2099q1", "2099q2"], min_cases=3)
    r1 = compute_signals(signals_conn, **kwargs)
    r2 = compute_signals(signals_conn, **kwargs)  # replace=True re-run
    assert r1.pairs_considered == r2.pairs_considered
    assert r1.rows_written == r2.rows_written
    s1 = {(s["drug"], s["event"]): s for s in r1.scores}
    s2 = {(s["drug"], s["event"]): s for s in r2.scores}
    assert s1 == s2
    cur.execute("SELECT count(*) FROM signal_scores WHERE period = 'all'")
    count_after_rerun = cur.fetchone()[0]
    assert count_after_rerun == r2.pairs_considered  # no duplicates


def test_fixture_pairs_below_floor_are_excluded(signals_conn):
    """Fixture pairs have a=1: full background considered, none pass floor 3."""
    res = compute_signals(signals_conn, primary_suspect_only=False, min_cases=3)
    assert res.pairs_considered == 0
    assert res.background_n == 5


def test_same_drug_listed_twice_counts_one_case(signals_conn):
    """Regression (found on real 2024q1 data): a case with two drug_seq rows
    for the SAME normalized drug must count once, or cells go negative."""
    cur = signals_conn.cursor()
    execute_values(cur, """
        INSERT INTO cases (primaryid, case_id, source_quarter, is_current)
        VALUES %s
    """, [("SYNDUP01", "SYNDUPCASE", "2099q1", True)])
    execute_values(cur, """
        INSERT INTO case_drugs (primaryid, case_id, drug_seq,
                                drug_normalized, role)
        VALUES %s
    """, [("SYNDUP01", "SYNDUPCASE", "1", "SYNDRUGA", "PS"),
           ("SYNDUP01", "SYNDUPCASE", "2", "SYNDRUGA", "CON")])
    execute_values(cur, """
        INSERT INTO case_events (primaryid, case_id, event_term)
        VALUES %s
    """, [("SYNDUP01", "SYNDUPCASE", "SYNEVENTX")])
    signals_conn.commit()
    try:
        res = compute_signals(signals_conn, primary_suspect_only=False,
                              quarters=["2099q1"], min_cases=1)
        score = next(s for s in res.scores
                     if s["drug"] == "SYNDRUGA" and s["event"] == "SYNEVENTX")
        # 1 case with the pair; drug_n counts that case once despite 2 rows
        assert score["a"] == 1
        assert score["b"] == 0
        assert score["c"] == 0
        assert score["d"] >= 0
        assert score["a"] + score["b"] + score["c"] + score["d"] \
            == res.background_n
    finally:
        _cleanup(cur)
        signals_conn.commit()


def test_trend_modules_on_fixtures(signals_conn):
    cur = signals_conn.cursor()
    volume = quarterly_volume(signals_conn)
    by_q = {p.quarter: p.reports for p in volume}
    # current versions only: 3 in 2024q1 (2 stale) + 2 in 2024q2
    assert by_q == {"2024q1": 3, "2024q2": 2}
    # too few quarters for a rolling z -> no flags invented
    assert all(p.z_score is None for p in volume)
    assert all(not p.volume_flag for p in volume)

    series = pair_quarterly_series(signals_conn, "METFORMIN", "NAUSEA")
    by_q2 = {p["quarter"]: p["a"] for p in series}
    assert by_q2["2024q1"] == 1     # case 100050
    assert by_q2["2024q2"] == 0
