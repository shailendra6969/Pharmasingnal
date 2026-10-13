"""Hand-calculated tests for PRR/ROR/CIs/chi-square (spec §10).

Expected values in this file were computed by hand from the formulas in
docs (Evans et al. PRR screening; Woolf log-CIs; Yates-corrected Pearson
chi-square). The same input must always produce the same output.
"""
from __future__ import annotations

import math

import pytest

from app.statistics.disproportionality import (
    Table2x2,
    chi_square,
    compute,
    prr_ci,
    prr_point,
    ror_ci,
    ror_point,
)


def test_table_validation():
    assert Table2x2(1, 2, 3, 4).total == 10
    assert Table2x2(1, 2, 3, 4).drug_cases == 3
    assert Table2x2(1, 2, 3, 4).event_cases == 4
    with pytest.raises(ValueError):
        Table2x2(-1, 2, 3, 4)
    with pytest.raises(ValueError):
        Table2x2(1.5, 2, 3, 4)  # type: ignore[arg-type]


def test_balanced_table_prr_ror_exactly_one():
    t = Table2x2(100, 100, 100, 100)
    assert prr_point(t) == pytest.approx(1.0)
    assert ror_point(t) == pytest.approx(1.0)
    # SE(ln PRR) = sqrt(1/100 - 1/200 + 1/100 - 1/200) = 0.1
    # 95% CI = exp(+-1.959964*0.1) = [0.8220, 1.2165]
    _, lo, hi = prr_ci(t)
    assert lo == pytest.approx(0.8220, abs=1e-3)
    assert hi == pytest.approx(1.2165, abs=1e-3)
    # SE(ln ROR) = sqrt(4 * 1/100) = 0.2 -> [0.6757, 1.4799]
    _, rlo, rhi, corrected = ror_ci(t)
    assert corrected is False
    assert rlo == pytest.approx(0.6757, abs=1e-3)
    assert rhi == pytest.approx(1.4799, abs=1e-3)


def test_screening_table_hand_calculated():
    # a=40 b=60 c=10 d=90
    t = Table2x2(40, 60, 10, 90)
    # PRR = (40/100) / (10/100) = 4.0
    assert prr_point(t) == pytest.approx(4.0)
    # SE(ln PRR) = sqrt(1/40 - 1/100 + 1/10 - 1/100) = sqrt(0.105)
    _, lo, hi = prr_ci(t)
    assert lo == pytest.approx(2.1195, abs=1e-3)
    assert hi == pytest.approx(7.5488, abs=1e-3)
    # ROR = (40*90)/(60*10) = 6.0
    assert ror_point(t) == pytest.approx(6.0)
    # SE(ln ROR) = sqrt(1/40+1/60+1/10+1/90) = sqrt(0.152777...)
    _, rlo, rhi, corrected = ror_ci(t)
    assert corrected is False
    assert rlo == pytest.approx(2.7890, abs=1e-3)
    assert rhi == pytest.approx(12.9080, abs=1e-3)
    # Yates chi-square = 200*(|40*90-60*10| - 100)^2 / (100*100*50*150)
    #                  = 200 * 2900^2 / 75_000_000 = 22.4267
    assert chi_square(t) == pytest.approx(22.4267, abs=1e-3)


def test_screening_flags_pass():
    stat = compute(Table2x2(40, 60, 10, 90))
    assert stat.prr_screen is True     # PRR 4 >= 2, chi2 22.4 >= 4, a 40 >= 3
    assert stat.ror_screen is True     # lower bound 2.789 > 1
    assert stat.insufficient is False
    assert stat.continuity_corrected is False


def test_screening_flags_fail_on_weak_signal():
    # PRR < 2 (practically 1) although counts are large
    stat = compute(Table2x2(11, 989, 10, 990))
    assert prr_point(Table2x2(11, 989, 10, 990)) == pytest.approx(1.1)
    assert stat.prr_screen is False
    assert stat.ror_screen is False


def test_min_cases_floor():
    # a=2 below the >=3 case floor -> insufficient, no screens
    stat = compute(Table2x2(2, 8, 4, 986), min_cases=3)
    assert stat.insufficient is True
    assert stat.prr_screen is False
    assert stat.ror_screen is False
    # point estimates still returned (they are facts, not verdicts)
    assert stat.prr == pytest.approx((2 / 10) / (4 / 990))


def test_zero_cell_continuity_correction():
    t = Table2x2(0, 10, 5, 100)
    assert t.has_zero
    # raw PRR defined (=0) but its CI is not (a=0)
    point, lo, hi = prr_ci(t)
    assert point == 0.0
    assert lo is None and hi is None
    # ROR CI uses Haldane-Anscombe 0.5 correction, flagged
    rpoint, rlo, rhi, corrected = ror_ci(t)
    assert corrected is True
    assert rpoint == pytest.approx((0.5 * 100.5) / (10.5 * 5.5))
    assert rlo is not None and rhi is not None and rlo < rhi


def test_zero_in_b_makes_raw_ror_undefined():
    t = Table2x2(3, 0, 5, 100)
    assert ror_point(t) is None        # raw undefined
    rpoint, rlo, rhi, corrected = ror_ci(t)
    assert corrected is True
    assert rpoint == pytest.approx((3.5 * 100.5) / (0.5 * 5.5))
    assert rhi > rpoint > rlo


def test_c_zero_prr_uses_continuity_correction():
    """Regression: c=0 made PRR's raw denominator 0.0 -> ZeroDivisionError
    on real 2024q1 data. Raw PRR is None; corrected estimate is returned."""
    t = Table2x2(40, 60, 0, 90)
    assert prr_point(t) is None        # raw ratio undefined (infinite)
    point, lo, hi = prr_ci(t)
    # Haldane-Anscombe on all cells: (40.5/101.0) / (0.5/91.0)
    assert point == pytest.approx((40.5 / (40.5 + 60.5)) / (0.5 / (0.5 + 90.5)))
    assert lo is not None and hi is not None and lo < point < hi
    stat = compute(t)
    assert stat.prr == point           # corrected estimate is what we store
    assert stat.continuity_corrected is True
    assert math.isfinite(stat.prr_upper)


def test_prr_none_when_drug_in_every_case():
    # c+d = 0: no unexposed background at all -> PRR undefined, no crash
    t = Table2x2(5, 5, 0, 0)
    assert prr_point(t) is None
    point, lo, hi = prr_ci(t)
    assert point is None and lo is None and hi is None
    stat = compute(t)
    assert stat.prr is None
    assert stat.prr_screen is False
    assert stat.insufficient is True


def test_degenerate_chi_square_is_none():
    assert chi_square(Table2x2(0, 0, 5, 100)) is None   # no drug cases
    assert chi_square(Table2x2(0, 0, 0, 0)) is None      # empty
    assert chi_square(Table2x2(10, 0, 0, 100)) == pytest.approx(98.2328, abs=1e-3)  # defined: marginals non-zero
    assert chi_square(Table2x2(0, 10, 0, 100)) is None   # event column empty


def test_large_counts_no_overflow():
    t = Table2x2(50_000, 150_000, 20_000, 930_000)
    stat = compute(t)
    assert stat.prr == pytest.approx((50_000 / 200_000) / (20_000 / 950_000))
    assert math.isfinite(stat.prr_upper)
    assert stat.ror_upper > stat.ror_lower > 0


def test_reproducibility():
    t = Table2x2(40, 60, 10, 90)
    runs = [compute(t) for _ in range(10)]
    assert all(r == runs[0] for r in runs)


def test_as_dict_roundtrip_keys():
    d = compute(Table2x2(40, 60, 10, 90)).as_dict()
    for key in ("a", "b", "c", "d", "prr", "prr_lower", "prr_upper",
                "ror", "ror_lower", "ror_upper", "chi_square",
                "prr_screen", "ror_screen", "insufficient",
                "continuity_corrected"):
        assert key in d
