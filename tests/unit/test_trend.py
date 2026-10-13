from __future__ import annotations

import pytest

from app.statistics.trend import rolling_z


def test_window_must_be_at_least_two():
    with pytest.raises(ValueError):
        rolling_z([1.0, 2.0, 3.0], window=1)


def test_incomplete_window_returns_none():
    out = rolling_z([10.0, 11.0, 12.0, 13.0, 14.0], window=3)
    assert out[:3] == [None, None, None]
    assert all(z is not None for z in out[3:])


def test_zero_variance_returns_none():
    out = rolling_z([5.0, 5.0, 5.0, 9.0, 99.0], window=3)
    assert out[3] is None          # window [5,5,5] -> SD 0 -> None, not 0.0
    assert out[4] is not None      # window [5,5,9] has variance -> spike flagged


def test_no_future_leakage():
    # Changing a FUTURE value must not change earlier z-scores
    a = rolling_z([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], window=3)
    b = rolling_z([1.0, 2.0, 3.0, 4.0, 5.0, 600.0], window=3)
    assert a[:5] == b[:5]


def test_known_value():
    # prev window [1,2,3]: mean 2, sample SD 1 -> z = (4-2)/1 = 2.0
    out = rolling_z([1.0, 2.0, 3.0, 4.0], window=3)
    assert out[3] == pytest.approx(2.0)


def test_deterministic():
    vals = [100.0, 110.0, 90.0, 105.0, 120.0, 80.0, 95.0]
    assert rolling_z(vals, 4) == rolling_z(vals, 4)
