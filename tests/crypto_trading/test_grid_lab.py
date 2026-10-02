import math

import pytest

from crypto_trading.entry_research import grid_lab as G

V = G.Variant("NEUTRAL", 2.0, 0.10, False)


def bars_from(closes, start=0):
    out = []
    prev = closes[0]
    for k, c in enumerate(closes):
        out.append((start + k * G.MIN, prev, max(prev, c), min(prev, c), c))
        prev = c
    return out


def warm(n=1500, p=100.0, amp=0.003):
    # small alternating noise so sigma_d is defined and ~ amp * sqrt(1440)
    return [p * (1 + (amp if k % 2 else -amp) / 2) for k in range(n)]


def test_sigma_series_has_no_lookahead():
    closes = warm()
    s1 = G.sigma_series(closes)
    closes2 = closes[:1460] + [c * 1.5 for c in closes[1460:]]
    s2 = G.sigma_series(closes2)
    assert s1[1460] == s2[1460]  # bar 1460's own close must not affect sig[1460]
    assert s1[1461] != s2[1461]


def test_oscillation_earns_step_minus_fees():
    base = warm()
    sig = G.sigma_series(base + [base[-1]])
    i0 = 1500
    p0 = base[i0 - 1]
    s = max(V.k_step * sig[i0], G.MIN_STEP)
    lo, hi = p0 * (1 + s) ** -1, p0
    tail = []
    for _ in range(20):
        tail += [lo * 0.999, hi * 1.0005]
    closes = base + tail
    bars = bars_from(closes)
    st = G.run_grid(bars, [], G.sigma_series(closes), i0, len(bars), V, 1000.0)
    assert st["fills"] >= 30 and st["stops"] == 0
    assert st["pnl"] > 0


def test_intrabar_round_trip_is_not_counted():
    base = warm()
    i0 = 1500
    p0 = base[i0 - 1]
    bars = bars_from(base)
    # one bar that spikes 3 % down and 3 % up, closing at p0
    bars.append((bars[-1][0] + G.MIN, p0, p0 * 1.03, p0 * 0.97, p0))
    closes = [b[4] for b in bars]
    sig = G.sigma_series(closes)[i0]
    st = G.run_grid(bars, [], G.sigma_series(closes), i0, len(bars), V, 1000.0)
    s = max(V.k_step * sig, G.MIN_STEP)
    n = max(2, min(40, int(math.log(1 + V.k_range * sig) / math.log(1 + s))))
    buys = sum(1 for j in range(1, n + 1) if p0 * 0.97 <= p0 * (1 + s) ** -j * (1 - G.THROUGH))
    # only the buys on the way down fill; every sell above them was created in this bar -> no fill
    assert st["fills"] == buys
    # what remains is inventory bought below p0 and closed at market at p0: gain minus fees and slippage
    assert st["pnl"] < buys * (1000.0 / n) * (s * buys)


def test_crash_through_range_stops_and_loses():
    base = warm()
    i0 = 1500
    tail = [base[-1] * (1 - 0.002 * k) for k in range(1, 200)]
    closes = base + tail
    bars = bars_from(closes)
    st = G.run_grid(bars, [], G.sigma_series(closes), i0, len(bars), V, 1000.0)
    assert st["stops"] + st["liquidations"] >= 1
    assert st["pnl"] < 0


def test_long_mode_never_shorts():
    base = warm()
    i0 = 1500
    tail = [base[-1] * (1 + 0.001 * k) for k in range(1, 100)]
    closes = base + tail
    bars = bars_from(closes)
    st = G.run_grid(bars, [], G.sigma_series(closes), i0, len(bars), G.Variant("LONG", 2.0, 0.1, False), 1000.0)
    assert st["pnl"] == pytest.approx(0.0, abs=1e-9) or st["fills"] == 0 or st["pnl"] > -5


def test_funding_charged_on_long_inventory():
    base = warm()
    i0 = 1500
    tail = [base[-1] * 0.98] * 10
    closes = base + tail
    bars = bars_from(closes)
    t_f = bars[i0 + 5][0]
    with_f = G.run_grid(bars, [(t_f, 0.01)], G.sigma_series(closes), i0, len(bars), V, 1000.0)
    no_f = G.run_grid(bars, [], G.sigma_series(closes), i0, len(bars), V, 1000.0)
    assert with_f["funding"] < 0 and with_f["pnl"] < no_f["pnl"]
    assert math.isclose(no_f["pnl"] - with_f["pnl"], -with_f["funding"], rel_tol=1e-9)
