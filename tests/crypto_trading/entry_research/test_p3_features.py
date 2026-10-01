"""P3 spot/perp and lead/lag integrity (point-in-time, no look-ahead)."""

import math

import pytest

from crypto_trading.entry_research.p3_leadlag import corr, events
from crypto_trading.entry_research.p3_spot import features

T = 1_000_000_200  # a 5m-aligned close time


def _spot(price_at):  # price_at(close_time) -> spot 1m bars keyed by OPEN time
    return {x - 60: (price_at(x), 10.0, 6.0) for x in range(T - 7200, T + 7200, 60)}


def _perp(price_at):
    return {x - 300: price_at(x) for x in range(T - 7200, T + 7200, 300)}


def test_spot_features_only_use_bars_closed_at_t():
    base = _spot(lambda x: 100.0)
    perp = _perp(lambda x: 100.0)
    before = features(T, 0.0, base, perp)
    future = dict(base)
    for x in range(T + 60, T + 7200, 60):  # change only bars closing AFTER T
        future[x - 60] = (500.0, 99.0, 99.0)
    assert features(T, 0.0, future, perp) == before


def test_spot_lead_sign_and_imbalance():
    spot = _spot(lambda x: 100.0 + (1.0 if x == T else 0.0))
    perp = _perp(lambda x: 100.0)
    f = features(T, 0.1, spot, perp)
    assert f["spot_lead5"] == pytest.approx(0.01)
    assert f["spot_ti15"] == pytest.approx(0.2)  # (2*6-10)/10
    assert f["ti_gap15"] == pytest.approx(0.1)
    assert f["basis"] == pytest.approx(math.log(100 / 101))


def test_corr_and_event_measurement_start_at_detection():
    assert corr([1.0, 2.0, 3.0] * 20, [2.0, 4.0, 6.0] * 20) == pytest.approx(1.0)
    spot = [100.0] * 86400
    perp = [100.0] * 86400
    for t in range(40000, 86400):
        spot[t] = 101.0  # spot jumps at 40000; perp follows 2 s later
    for t in range(40002, 86400):
        perp[t] = 101.0
    evs = events(spot, perp)
    assert len(evs) >= 1
    e = evs[0]
    assert e["gap_bp"] > 0
    assert e["L0_H5"] > 90  # captured only with zero latency
    assert e["L5_H5"] == pytest.approx(0.0)  # gone by 5 s
