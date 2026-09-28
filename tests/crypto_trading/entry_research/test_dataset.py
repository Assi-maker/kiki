from datetime import UTC, datetime, timedelta

import pytest

from crypto_trading.entry_research import dataset as ds
from crypto_trading.shadow.evaluation import Bar

T0 = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _bars(start, n, price=100.0, step=0.0, rng=1.0):
    out = []
    for i in range(n):
        p = price + step * i
        out.append(Bar(start + timedelta(minutes=i), p, p + rng / 2, p - rng / 2, p))
    return out


def test_closed_before_excludes_the_bar_running_at_t0():
    bars = _bars(T0 - timedelta(minutes=3), 6)
    kept = ds.closed_before(bars, T0)
    assert [b.t for b in kept] == [T0 - timedelta(minutes=3), T0 - timedelta(minutes=2),
                                   T0 - timedelta(minutes=1)]


def test_price_features_are_blind_to_bars_after_t0():
    pre = _bars(T0 - timedelta(hours=4, minutes=20), 260, step=0.01)
    future_spike = [Bar(T0 + timedelta(minutes=i), 500, 900, 400, 800) for i in range(30)]
    a = ds.price_features(ds.closed_before(pre, T0))
    b = ds.price_features(ds.closed_before(pre + future_spike, T0))
    assert a == b
    assert a["ret_4h"] is not None and a["atr15_pct"] is not None


def test_resample_drops_an_incomplete_bucket():
    bars = _bars(datetime(2026, 9, 5, 12, 0, tzinfo=UTC), 20)  # 15 + 5 minutes
    assert len(ds.resample(bars, 15)) == 1


def test_standardized_outcome_uses_atr_bracket_and_real_costs():
    after = _bars(T0, 400, price=100.0, rng=0.0)
    after[5] = Bar(after[5].t, 100, 106.5, 100, 106)  # target at 100 * (1 + 3 * 2 %) = 106
    out = ds.standardized_outcome(after, atr_pct=2.0)
    assert out["reason"] == "TP"
    # risk = 4 % of entry; gain 6 % - 0.1 % fee -> (6 - 0.1) / 4
    assert out["r"] == pytest.approx((6.0 - 0.1) / 4.0)
    assert out["risk_pct"] == pytest.approx(4.0)


def test_standardized_stop_includes_slippage():
    after = _bars(T0, 400, price=100.0, rng=0.0)
    after[3] = Bar(after[3].t, 100, 100, 90, 95)
    out = ds.standardized_outcome(after, atr_pct=1.0)
    assert out["reason"] == "SL"
    exit_price = 98 * (1 - ds.STOP_SLIP)
    assert out["r"] == pytest.approx((exit_price - 100 - 0.1) / 2)


def test_independence_keeps_first_signal_per_symbol_per_6h():
    rows = [{"symbol": "A", "t0": T0}, {"symbol": "A", "t0": T0 + timedelta(hours=2)},
            {"symbol": "B", "t0": T0 + timedelta(hours=2)}, {"symbol": "A", "t0": T0 + timedelta(hours=6)}]
    ds.mark_independent(rows)
    assert [r["independent"] for r in rows] == [True, False, True, True]


def test_periods_are_time_ordered_and_disjoint():
    assert ds.period_of(datetime(2026, 9, 12, 23, tzinfo=UTC)) == "TRAIN"
    assert ds.period_of(datetime(2026, 9, 13, tzinfo=UTC)) == "VALID"
    assert ds.period_of(datetime(2026, 9, 26, tzinfo=UTC)) == "TEST"
