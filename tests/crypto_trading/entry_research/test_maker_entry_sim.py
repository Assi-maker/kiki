"""P1 maker-entry simulation integrity (research only)."""

import pytest

from crypto_trading.entry_research.maker_entry_sim import (
    TAKER_SLIP,
    evaluate_row,
    maker_fill,
)
from crypto_trading.entry_research.trade_review import FEE, NOTIONAL


def bar(i, o, h, lo, c):
    return (i * 60_000, o, h, lo, c, 1.0)


def test_touching_the_limit_is_not_a_fill_trading_through_is():
    bars = [bar(0, 100, 100.2, 99.9, 100), bar(1, 100, 100.1, 99.95, 100)]
    assert maker_fill(bars, 99.9, 5).filled is False  # only touched
    bars.append(bar(2, 100, 100, 99.89, 99.95))
    fill = maker_fill(bars, 99.9, 5)
    assert fill.filled and fill.price == 99.9 and fill.minutes == 3


def test_a_limit_never_fills_after_its_lifetime():
    bars = [bar(0, 100, 100.1, 99.99, 100), bar(1, 100, 100, 99.99, 100), bar(2, 99, 99, 98, 98)]
    assert maker_fill(bars, 99.5, 2).filled is False


def test_a_gap_below_the_limit_fills_at_the_limit_not_better():
    bars = [bar(0, 98, 98.5, 97, 98)]
    assert maker_fill(bars, 99.5, 1).price == 99.5


def _row(bars):
    return {"bars": bars, "sl": 95.0, "tp": 105.0}


def test_maker_entry_pays_maker_fee_and_keeps_absolute_brackets():
    bars = [bar(0, 100, 100, 99.7, 99.8)] + [bar(i, 99.8, 105.5, 99.8, 105) for i in range(1, 3)]
    out = evaluate_row(_row(bars), 100.0, 0.002, 5, 0.0002)
    assert out["filled"]
    gross = 105.0 / 99.8 - 1
    expected = NOTIONAL * gross - NOTIONAL * 0.0002 - NOTIONAL * (1 + gross) * FEE
    assert out["maker"] == pytest.approx(expected)


def test_a_fill_minute_that_also_hits_the_stop_is_stopped_at_once():
    bars = [bar(0, 100, 100, 94, 94.5), bar(1, 94.5, 110, 94.5, 109)]
    out = evaluate_row(_row(bars), 100.0, 0.002, 5, 0.0002)
    assert out["filled"] and out["maker"] < -40


def test_missed_order_uses_the_taker_result_as_lost_opportunity_and_fallback_enters_late():
    bars = [bar(i, 100 + i, 100.5 + i, 100 + i, 100.4 + i) for i in range(10)]
    out = evaluate_row(_row(bars), 100.0, 0.002, 3, 0.0002)
    assert out["filled"] is False and out["maker"] is None
    assert out["taker"] is not None
    assert out["fallback"] is not None  # entered at market after 3 minutes
    taker_entry = 100.0 * (1 + TAKER_SLIP)
    assert taker_entry > 100.0
