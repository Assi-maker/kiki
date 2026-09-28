"""P3-P6 shadow evaluation (2026-09-28): pure parts.

Everything here only COMPUTES what a rule would have done. Nothing can block,
open, change or close a trade."""
from datetime import UTC, datetime, timedelta

import pytest

from crypto_trading.shadow.evaluation import (
    Bar,
    decision_features,
    simulate_trade,
    veto_flags,
)

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _bars(prices, start=T0, step=1):
    """prices: list of (open, high, low, close) per minute."""
    return [Bar(start + timedelta(minutes=i * step), *p) for i, p in enumerate(prices)]


# --- simulate_trade -------------------------------------------------------

def test_a_plain_bracket_hits_the_target():
    bars = _bars([(100, 101, 99.5, 100.5), (100.5, 110.5, 100, 110)])
    result = simulate_trade(bars, entry=100, stop=95, target=110, fee_rt=0.001, stop_slip=0.0015)
    assert result.reason == "TP"
    assert result.exit_price == 110
    assert result.r == pytest.approx((10 - 0.1) / 5)
    assert result.mfe_pct == pytest.approx(10.5)


def test_the_stop_fills_below_its_price_and_fees_are_charged():
    bars = _bars([(100, 100.5, 94, 95)])
    result = simulate_trade(bars, entry=100, stop=95, target=110, fee_rt=0.001, stop_slip=0.0015)
    assert result.reason == "SL"
    assert result.exit_price == pytest.approx(95 * 0.9985)
    assert result.r == pytest.approx((95 * 0.9985 - 100 - 0.1) / 5)


def test_same_bar_stop_and_target_counts_the_stop_first():
    bars = _bars([(100, 111, 94, 100)])
    assert simulate_trade(bars, 100, 95, 110, 0.001, 0.0015).reason == "SL"


def test_time_exit_at_the_horizon_close():
    bars = _bars([(100, 101, 99, 100.5)] * 10)
    result = simulate_trade(bars, 100, 95, 110, 0.001, 0.0015, horizon_minutes=5)
    assert result.reason == "TIME"
    assert result.exit_price == 100.5


def test_break_even_covers_costs_and_only_moves_after_the_trigger_bar():
    bars = _bars([(100, 101.2, 99.9, 101), (101, 101, 99, 99.5)])
    result = simulate_trade(bars, 100, 95, 110, 0.001, 0.0015, be_trigger_pct=1.0, be_offset_pct=0.25)
    assert result.reason == "BE"
    assert result.exit_price == pytest.approx(100.25 * 0.9985)


def test_break_even_is_not_armed_by_the_same_bar_that_would_stop_out():
    """No intrabar lookahead: the stop is moved only AFTER the bar that
    reached the trigger has closed."""
    bars = _bars([(100, 101.5, 94, 96)])
    result = simulate_trade(bars, 100, 95, 110, 0.001, 0.0015, be_trigger_pct=1.0, be_offset_pct=0.25)
    assert result.reason == "SL"


def test_trailing_locks_a_fraction_of_the_favourable_excursion():
    bars = _bars([(100, 104, 100, 103.5), (103.5, 103.5, 101, 101.5)])
    result = simulate_trade(bars, 100, 95, 110, 0.001, 0.0015, trail_start_pct=1.0, trail_fraction=0.5)
    assert result.reason == "TRAIL"
    assert result.exit_price == pytest.approx(102 * 0.9985)


def test_a_trade_with_no_bars_is_none():
    assert simulate_trade([], 100, 95, 110, 0.001, 0.0015) is None


# --- decision_features: no lookahead ---------------------------------------

def _series(start, n, price=100.0, drift=0.0):
    out, p = [], price
    for i in range(n):
        out.append(Bar(start + timedelta(minutes=i), p, p * 1.001, p * 0.999, p + drift))
        p += drift
    return out


def test_features_use_only_bars_closed_before_the_decision():
    decided = T0
    past = _series(T0 - timedelta(hours=4), 240, drift=-0.01)
    future = _series(T0, 120, price=50, drift=5)  # absurd future must not matter
    a = decision_features(past, past, decided)
    b = decision_features(past + future, past + future, decided)
    assert a == b
    assert a["btc_ret_1h_pct"] < 0 and a["btc_ret_4h_pct"] < 0
    assert a["btc_falling"] is True


def test_features_are_none_without_enough_history():
    f = decision_features([], [], T0)
    assert f["btc_ret_1h_pct"] is None and f["btc_falling"] is None


# --- veto_flags ----------------------------------------------------------

def test_veto_flags_are_natural_zero_points_not_tuned_thresholds():
    flags = veto_flags(
        features={"btc_falling": True, "symbol_ret_1h_pct": 1.0},
        gate_metrics={"bull_probability": 0.2, "bear_probability": 0.45, "neutral_probability": 0.35},
        volume_confirmed=False, reentry_within_6h=True, gf_eq_verdict="WAIT",
    )
    assert flags == {
        "BEARISH_DOMINANT": True, "BULLISH_NOT_DOMINANT": True, "ALT_LONG_WHILE_BTC_FALLING": True,
        "NO_VOLUME_CONFIRMATION": True, "SAME_SYMBOL_REENTRY_6H": True, "GODFATHER_EQ_NOT_TRADE": True,
    }


def test_unknown_inputs_are_none_not_false():
    flags = veto_flags({"btc_falling": None}, {}, None, False, None)
    assert flags["ALT_LONG_WHILE_BTC_FALLING"] is None
    assert flags["BEARISH_DOMINANT"] is None
    assert flags["GODFATHER_EQ_NOT_TRADE"] is None
