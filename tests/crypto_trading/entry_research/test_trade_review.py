"""Integrity tests for the 2026-10-01 trade-review exit replay."""

from datetime import UTC, datetime, timedelta

import pytest

from crypto_trading.entry_research.trade_review import (
    FEE,
    NOTIONAL,
    STOP_SLIP,
    Rule,
    _resample,
    period_of,
    simulate,
)

BASE = Rule("BASE", "baseline")


def bar(i, o, h, lo, c):
    return (i * 60_000, o, h, lo, c, 1.0)


def test_stop_first_when_one_bar_spans_both_levels():
    out = simulate(BASE, 100.0, 95.0, 105.0, [bar(0, 100, 106, 94, 100)])
    assert out.reason == "SL"
    assert out.exit_price == 95.0


def test_profit_protection_applies_only_from_the_next_bar():
    # bar 0 reaches +1 % and falls back below entry inside the same bar:
    # the break-even stop must NOT fire in bar 0 (it did not exist yet).
    bars = [bar(0, 100, 101.5, 99.5, 100.2), bar(1, 100.2, 100.3, 99.0, 99.2)]
    out = simulate(BASE, 100.0, 95.0, 105.0, bars)
    assert out.reason == "BE"
    assert out.exit_price == 100.0
    assert out.minutes == 1


def test_no_profit_protection_rule_keeps_original_stop():
    bars = [bar(0, 100, 101.5, 99.5, 100.2), bar(1, 100.2, 100.3, 94.0, 94.5)]
    out = simulate(Rule("NO_PP", "x", pp_at=None), 100.0, 95.0, 105.0, bars)
    assert out.reason == "SL"


def test_gap_through_stop_fills_at_open():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 93, 93.5, 92, 93)]
    out = simulate(BASE, 100.0, 95.0, 105.0, bars)
    assert out.exit_price == 93
    expected = (
        NOTIONAL * (93 / 100 - 1) - NOTIONAL * FEE - NOTIONAL * 0.93 * FEE - NOTIONAL * STOP_SLIP
    )
    assert out.pnl == pytest.approx(expected)


def test_time_exit_at_hold_limit_uses_that_bars_open():
    bars = [bar(i, 100, 100.4, 99.8, 100.1) for i in range(400)]
    out = simulate(BASE, 100.0, 95.0, 105.0, bars)
    assert out.reason == "TIME"
    assert out.minutes == 360


def test_mfe_lock_ratchets_and_never_lowers():
    rule = Rule("D", "x", trail_frac=0.5, trail_at=2.0)
    bars = [
        bar(0, 100, 104, 100, 103.8),  # MFE 4 % -> stop 102 from bar 1
        bar(1, 103.8, 103.9, 102.5, 102.6),  # MFE unchanged, stop stays 102
        bar(2, 102.6, 102.7, 101.9, 102.0),  # hits 102
    ]
    out = simulate(rule, 100.0, 95.0, 110.0, bars)
    assert out.reason == "LOCK"
    assert out.exit_price == pytest.approx(102.0)


def test_retrace_exit_fills_at_next_open_not_at_the_close_that_triggered_it():
    rule = Rule("E", "x", pp_at=None, retrace=0.5, retrace_at=2.0)
    bars = [
        bar(0, 100, 104, 100, 103.5),
        bar(1, 103.5, 103.6, 101.5, 101.8),  # close gives back > 50 % of 4 %
        bar(2, 101.0, 101.2, 100.8, 101.1),
    ]
    out = simulate(rule, 100.0, 95.0, 110.0, bars)
    assert out.reason == "RETRACE"
    assert out.exit_price == 101.0


def test_closer_target_scales_distance():
    out = simulate(
        Rule("K", "x", tp_scale=0.8), 100.0, 95.0, 105.0, [bar(0, 100, 104.1, 99.9, 104)]
    )
    assert out.reason == "TP"
    assert out.exit_price == pytest.approx(104.0)


def test_lock_never_placed_above_the_bar_close():
    # A huge spike to +8 % that closes at +0.5 %: the lock cannot sit above the
    # close (an exchange would reject a long stop above the mark price).
    rule = Rule("A", "x", locks=((4.0, 3.0),))
    bars = [bar(0, 100, 108, 100, 100.5), bar(1, 100.5, 100.6, 100.3, 100.4)]
    out = simulate(rule, 100.0, 95.0, 120.0, bars)
    assert out.reason in {"BE", "LOCK"}
    assert out.exit_price <= 100.5


def test_resample_aligns_on_epoch_buckets():
    bars = [(i * 60_000, 1.0 + i, 2.0 + i, 0.5, 1.5 + i, 1.0) for i in range(10)]
    out = _resample(bars, 5)
    assert [b[0] for b in out] == [0, 300_000]
    assert out[0][1] == 1.0 and out[0][4] == 1.5 + 4 and out[0][5] == 5.0


def test_periods_are_chronological_and_disjoint():
    t = datetime(2026, 9, 21, 23, 59, tzinfo=UTC)
    assert period_of(t) == "VALID"
    assert period_of(t + timedelta(minutes=1)) == "TEST"


def test_discretionary_exit_uses_next_bar_open_only():
    bars = [
        bar(0, 100, 101, 99, 100.5),
        bar(1, 100.7, 103, 100.6, 102.9),
        bar(2, 102.9, 103, 102, 102.5),
    ]
    out = simulate(BASE, 100.0, 95.0, 110.0, bars, events=((60_000, "EXIT", 0.0),))
    assert out.reason == "DISCRETIONARY_EXIT"
    assert out.exit_price == 100.7


def test_discretionary_stop_is_capped_below_the_bar_open_and_never_lowers():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 100, 100.2, 99.0, 99.1)]
    out = simulate(
        Rule("x", "x", pp_at=None),
        100.0,
        95.0,
        110.0,
        bars,
        events=((60_000, "STOP", 101.0), (60_000, "STOP", 90.0)),
    )
    assert out.exit_price == pytest.approx(100 * 0.9999)
