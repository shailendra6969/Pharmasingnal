"""Unit tests for quarterly PRR profiles (app/statistics/qprofile.py).

Tests hand-calculated quarter_prr values, continuity correction,
and all profile_verdict outcomes: elevation_is_new, consistent, mixed, insufficient.
"""
from __future__ import annotations

import pytest

from app.statistics.qprofile import (
    DEFAULT_THRESHOLDS,
    profile_verdict,
    quarter_prr,
    quarterly_profiles,
)


# ============================================================================
# quarter_prr hand-calculated tests
# ============================================================================

def test_quarter_prr_hand_calculated():
    """Hand-calculated test for continuity-corrected quarterly PRR.

    Given:
      a = 10 (co-occurrences in quarter)
      drug_cases = 100
      event_cases = 50
      population = 1000

    Haldane-Anscombe 0.5 correction:
      a2 = a + 0.5 = 10.5
      b2 = (drug_cases - a) + 0.5 = 90.5
      a2 + b2 = 101.0

      c2 = (event_cases - a) + 0.5 = 40.5
      d2 = (population - drug_cases - event_cases + a) + 0.5 = 860.5
      c2 + d2 = (1000 - 100) + 1.0 = 901.0

      qPRR = (10.5 / 101.0) / (40.5 / 901.0)
           = 0.103960396 / 0.044950055
           = 2.312799... -> rounded to 4 decimals = 2.3128
    """
    res = quarter_prr(a=10, drug_cases=100, event_cases=50, population=1000)
    assert res == 2.3128


def test_quarter_prr_hand_calculated_small_table():
    """Second hand-calculated fixture:
    a = 4, drug_cases = 20, event_cases = 20, population = 200.

      a2 = 4.5, b2 = 16.5 -> a2 + b2 = 21.0
      c2 = 16.5, d2 = (200 - 20 - 20 + 4) + 0.5 = 164.5 -> c2 + d2 = 181.0
      qPRR = (4.5 / 21.0) / (16.5 / 181.0)
           = (0.2142857) / (0.0911602) = 2.35065... -> 2.3507
    """
    assert quarter_prr(a=4, drug_cases=20, event_cases=20, population=200) == 2.3506


def test_quarter_prr_zero_co_occurrences():
    """When a=0, Haldane-Anscombe gives a non-zero, well-defined small PRR."""
    # a=0, drug_cases=100, event_cases=50, population=1000
    # a2=0.5, b2=100.5 -> 101
    # c2=50.5, d2=850.5 -> 901
    # qPRR = (0.5 / 101) / (50.5 / 901) = 0.0883
    res = quarter_prr(a=0, drug_cases=100, event_cases=50, population=1000)
    assert res == 0.0883


def test_quarter_prr_event_only_in_drug():
    """When event_cases == a (c=0), event never appears outside drug.
    Haldane-Anscombe prevents division by zero and produces a large finite number."""
    # a=10, drug_cases=100, event_cases=10, population=1000
    # a2=10.5, b2=90.5 -> 101
    # c2=0.5, d2=900.5 -> 901
    # qPRR = (10.5 / 101) / (0.5 / 901) = 187.3366
    res = quarter_prr(a=10, drug_cases=100, event_cases=10, population=1000)
    assert res == 187.3366


def test_quarter_prr_invalid_inputs_return_none():
    """Negative drug cases, zero drug cases, or population <= drug cases return None."""
    assert quarter_prr(a=1, drug_cases=0, event_cases=10, population=100) is None
    assert quarter_prr(a=1, drug_cases=-5, event_cases=10, population=100) is None
    assert quarter_prr(a=5, drug_cases=100, event_cases=10, population=100) is None
    assert quarter_prr(a=5, drug_cases=150, event_cases=10, population=100) is None


# ============================================================================
# profile_verdict tests
# ============================================================================

def test_verdict_elevation_is_new():
    """elevation_is_new: pooled PRR is elevated (>= 2.0), baseline median PRR
    is below background (< 1.0), and the latest quarter is elevated (>= 2.0)."""
    series = [
        {"quarter": "2024q1", "a": 1, "drug_cases": 20, "event_cases": 30,
         "population_cases": 1000, "prr": 0.5},
        {"quarter": "2024q2", "a": 2, "drug_cases": 25, "event_cases": 35,
         "population_cases": 1000, "prr": 0.8},
        {"quarter": "2024q3", "a": 2, "drug_cases": 30, "event_cases": 40,
         "population_cases": 1000, "prr": 0.7},
        {"quarter": "2024q4", "a": 15, "drug_cases": 35, "event_cases": 45,
         "population_cases": 1000, "prr": 4.0},
    ]
    # Baseline PRRs: [0.5, 0.7, 0.8] -> median = 0.7 (< 1.0)
    # Latest PRR = 4.0 (>= 2.0)
    # Pooled PRR = 2.5 (>= 2.0)
    v = profile_verdict(series, pooled_prr=2.5)
    assert v["verdict"] == "elevation_is_new"
    assert v["flagged"] is True
    assert v["quarters_evaluable"] == 4
    assert v["quarters_elevated"] == 1
    assert v["quarters_below_background"] == 3
    assert v["baseline_median_prr"] == 0.7
    assert v["latest_prr"] == 4.0
    assert v["pooled_prr"] == 2.5
    assert "pooled elevation is a new-quarter reporting regime" in v["reason"]


def test_verdict_consistent():
    """consistent: quarterly PRR is elevated (>= 2.0) in EVERY evaluable quarter."""
    series = [
        {"quarter": "2024q1", "a": 10, "drug_cases": 20, "event_cases": 30,
         "population_cases": 1000, "prr": 2.5},
        {"quarter": "2024q2", "a": 12, "drug_cases": 25, "event_cases": 35,
         "population_cases": 1000, "prr": 3.2},
        {"quarter": "2024q3", "a": 11, "drug_cases": 30, "event_cases": 40,
         "population_cases": 1000, "prr": 2.8},
        {"quarter": "2024q4", "a": 15, "drug_cases": 35, "event_cases": 45,
         "population_cases": 1000, "prr": 3.5},
    ]
    # All 4 quarters elevated
    v = profile_verdict(series, pooled_prr=3.0)
    assert v["verdict"] == "consistent"
    assert v["flagged"] is False
    assert v["quarters_evaluable"] == 4
    assert v["quarters_elevated"] == 4
    assert v["quarters_below_background"] == 0
    assert v["baseline_median_prr"] == 2.8
    assert v["latest_prr"] == 3.5
    assert "historically supported" in v["reason"]


def test_verdict_mixed():
    """mixed: has sufficient evaluable baseline history, but neither all quarters
    are elevated nor does it meet the elevation_is_new condition."""
    series = [
        {"quarter": "2024q1", "a": 5, "drug_cases": 20, "event_cases": 30,
         "population_cases": 1000, "prr": 1.5},
        {"quarter": "2024q2", "a": 10, "drug_cases": 25, "event_cases": 35,
         "population_cases": 1000, "prr": 2.5},
        {"quarter": "2024q3", "a": 7, "drug_cases": 30, "event_cases": 40,
         "population_cases": 1000, "prr": 1.8},
        {"quarter": "2024q4", "a": 8, "drug_cases": 35, "event_cases": 45,
         "population_cases": 1000, "prr": 1.9},
    ]
    # Baseline PRRs: [1.5, 1.8, 2.5] -> median = 1.8 (not < 1.0)
    # Latest PRR = 1.9 (not >= 2.0)
    # Evaluated = 4, elevated = 1 (not all 4)
    v = profile_verdict(series, pooled_prr=2.1)
    assert v["verdict"] == "mixed"
    assert v["flagged"] is False
    assert v["quarters_evaluable"] == 4
    assert v["quarters_elevated"] == 1
    assert v["baseline_median_prr"] == 1.8
    assert v["latest_prr"] == 1.9
    assert "mixed quarterly history" in v["reason"]


def test_verdict_insufficient_empty_series():
    """Empty series returns insufficient."""
    v = profile_verdict([], pooled_prr=2.5)
    assert v["verdict"] == "insufficient"
    assert v["flagged"] is False
    assert v["quarters_evaluable"] == 0
    assert "no quarterly data" in v["reason"]


def test_verdict_insufficient_fewer_than_two_baseline_quarters():
    """Only 1 or 2 total quarters -> baseline has < 2 quarters -> insufficient."""
    # 1 quarter
    s1 = [{"quarter": "2024q1", "a": 5, "drug_cases": 20, "prr": 2.5}]
    v1 = profile_verdict(s1, pooled_prr=2.5)
    assert v1["verdict"] == "insufficient"
    assert "0 evaluable baseline quarters (< 2)" in v1["reason"]

    # 2 quarters -> baseline has 1 quarter
    s2 = [
        {"quarter": "2024q1", "a": 5, "drug_cases": 20, "prr": 2.5},
        {"quarter": "2024q2", "a": 6, "drug_cases": 20, "prr": 2.5},
    ]
    v2 = profile_verdict(s2, pooled_prr=2.5)
    assert v2["verdict"] == "insufficient"
    assert "1 evaluable baseline quarters (< 2)" in v2["reason"]


def test_verdict_insufficient_drug_cases_below_threshold():
    """Quarters with drug_cases < min_drug_cases (default 10) are not evaluable."""
    # 3 total quarters, but Q1 has only 5 drug cases -> baseline only has 1 evaluable quarter
    series = [
        {"quarter": "2024q1", "a": 2, "drug_cases": 5, "prr": 2.0},   # unevaluable (<10)
        {"quarter": "2024q2", "a": 5, "drug_cases": 20, "prr": 2.0},
        {"quarter": "2024q3", "a": 5, "drug_cases": 20, "prr": 2.0},
    ]
    v = profile_verdict(series, pooled_prr=2.0)
    assert v["verdict"] == "insufficient"
    assert "1 evaluable baseline quarters (< 2)" in v["reason"]


def test_verdict_insufficient_unevaluable_latest_quarter():
    """If latest quarter has drug_cases < 10 or prr is None, verdict is insufficient."""
    series = [
        {"quarter": "2024q1", "a": 5, "drug_cases": 20, "prr": 2.0},
        {"quarter": "2024q2", "a": 5, "drug_cases": 20, "prr": 2.0},
        {"quarter": "2024q3", "a": 1, "drug_cases": 5, "prr": 2.0},   # unevaluable latest
    ]
    v = profile_verdict(series, pooled_prr=2.0)
    assert v["verdict"] == "insufficient"
    assert "unevaluable latest quarter" in v["reason"]

    # Latest quarter prr is None
    series_none = [
        {"quarter": "2024q1", "a": 5, "drug_cases": 20, "prr": 2.0},
        {"quarter": "2024q2", "a": 5, "drug_cases": 20, "prr": 2.0},
        {"quarter": "2024q3", "a": 1, "drug_cases": 20, "prr": None},
    ]
    v_none = profile_verdict(series_none, pooled_prr=2.0)
    assert v_none["verdict"] == "insufficient"
    assert "unevaluable latest quarter" in v_none["reason"]


def test_verdict_custom_thresholds():
    """Custom thresholds can adjust baseline quarter requirement."""
    # 2 total quarters, but min_baseline_quarters overridden to 1
    series = [
        {"quarter": "2024q1", "a": 5, "drug_cases": 20, "prr": 3.0},
        {"quarter": "2024q2", "a": 6, "drug_cases": 20, "prr": 3.0},
    ]
    v = profile_verdict(series, pooled_prr=3.0,
                        thresholds={"min_baseline_quarters": 1})
    assert v["verdict"] == "consistent"
    assert v["quarters_evaluable"] == 2
    assert v["baseline_median_prr"] == 3.0


class _DummyCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _DummyConn:
    def cursor(self):
        return _DummyCursor([])

    def rollback(self):
        pass


def test_quarterly_profiles_empty_db():
    """quarterly_profiles returns a valid empty report when signal_scores is empty."""
    conn = _DummyConn()
    report = quarterly_profiles(conn)
    assert report["status"] == "ok"
    assert report["signals_evaluated"] == 0
    assert report["flagged"] == []
    assert report["verdict_counts"] == {}
    assert report["evaluated_quarter"] is None
