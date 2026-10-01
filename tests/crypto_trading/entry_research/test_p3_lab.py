"""P3 lab integrity: bracket conventions, RSI, z-score."""

import pytest

from crypto_trading.entry_research.p3_lab import FEE, STOP_SLIP, TAKER_SLIP, _rsi, _z, bracket


def _bars(rows):
    o, h, lo, c = (list(x) for x in zip(*rows, strict=True))
    return o, h, lo, c


def test_stop_is_checked_before_target_inside_one_bar():
    o, h, lo, c = _bars([(100, 106, 95, 100)] * 80)
    ret, reason, _, _ = bracket(o, h, lo, c, 0, +1, [], [], 0)
    assert reason == "SL"
    assert ret == pytest.approx((95.7 * (1 - STOP_SLIP) / 100 - 1) - 2 * FEE - TAKER_SLIP)


def test_short_target_and_cost():
    o, h, lo, c = _bars([(100, 100.5, 94, 95)] + [(95, 95, 95, 95)] * 80)
    ret, reason, mfe, _ = bracket(o, h, lo, c, 0, -1, [], [], 0)
    assert reason == "TP"
    assert ret == pytest.approx(0.05 - 2 * FEE - TAKER_SLIP)
    assert mfe == pytest.approx(0.06)


def test_longs_pay_positive_funding_shorts_receive_it():
    o, h, lo, c = _bars([(100, 100.1, 99.9, 100)] * 80)
    fts, frs = [600], [0.001]
    long_ret = bracket(o, h, lo, c, 0, +1, fts, frs, 0)[0]
    short_ret = bracket(o, h, lo, c, 0, -1, fts, frs, 0)[0]
    assert long_ret < short_ret
    assert short_ret - long_ret == pytest.approx(0.002, abs=1e-3)


def test_time_exit_needs_the_whole_window():
    o, h, lo, c = _bars([(100, 100.1, 99.9, 100)] * 10)
    assert bracket(o, h, lo, c, 0, +1, [], [], 0) is None


def test_rsi_and_z():
    assert _rsi([1.0] * 15) == 50.0
    assert _rsi([float(i) for i in range(15)]) == 100.0
    assert _z(10.0, [1.0, 2.0] * 10) > 10
    assert _z(1.0, [1.0]) is None
