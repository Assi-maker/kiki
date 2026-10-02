import pytest

from crypto_trading.entry_research import grid_lab as G
from crypto_trading.entry_research.grid_tick import NOTIONAL, TickGrid

V = G.Variant("LONG", 1.0, 0.10, False)
SIG = 0.05  # daily vol 5 % -> step 0.5 %, n = 9


def armed(**kw) -> TickGrid:
    g = TickGrid(V, **kw)
    g.maybe_arm(mi=0, t_ms=0, p0=100.0, sig=SIG, ret4h=0.0)
    assert g.active and g.n == 9
    return g


def test_touch_at_level_does_not_fill_strictly_below_does():
    g = armed()
    lvl = g.levels[g.n - 1]
    g.trade(1000, lvl)
    assert g.st["fills"] == 0
    g.trade(2000, lvl * 0.99999)
    assert g.st["fills"] == 1 and g.pos == pytest.approx(g.q)


def test_touch_mode_fills_at_level():
    g = armed(touch=True)
    g.trade(1000, g.levels[g.n - 1])
    assert g.st["fills"] == 1


def test_counter_order_waits_for_latency():
    g = armed(latency_ms=1000)
    buy = g.levels[g.n - 1]
    g.trade(10_000, buy * 0.999)
    sell = g.levels[g.n]
    g.trade(10_500, sell * 1.001)  # 0.5 s later: counter sell not live yet
    assert g.st["fills"] == 1 and g.pos > 0
    g.trade(11_000, sell * 1.001)  # 1 s later: live
    assert g.st["fills"] == 2 and g.pos == 0


def test_round_trip_profit_is_step_minus_maker_fees():
    g = armed()
    buy, sell = g.levels[g.n - 1], g.levels[g.n]
    g.trade(1_000, buy * 0.999)
    g.trade(5_000, sell * 1.001)
    expected = g.q * (sell - buy) - g.q * (buy + sell) * G.MAKER
    assert g.cash == pytest.approx(expected)


def test_buy_only_never_shorts():
    g = armed()
    g.trade(1_000, 150.0)
    assert g.pos == 0 and g.st["fills"] == 0


def test_tick_liquidation_loses_whole_margin():
    g = armed()
    g.trade(1_000, 95.6)  # fills all 9 buys (bottom level ~95.6)
    assert g.pos > 0
    g.trade(2_000, 80.0)  # equity far below -100 margin
    assert g.st["liquidations"] == 1 and not g.active
    assert g.cash == pytest.approx(-G.MARGIN - 0.005 * NOTIONAL)


def test_range_stop_on_minute_close_closes_at_market():
    g = armed()
    g.trade(1_000, g.levels[g.n - 2] * 0.999)
    pos = g.pos
    low = g.levels[0] * (1 - g.s) * 0.999
    g.minute_close(1, low)
    assert g.st["stops"] == 1 and g.pos == 0 and not g.active
    assert g.cooldown_until == 1 + G.COOLDOWN
    assert pos > 0


def test_trend_filter_blocks_arming():
    g = TickGrid(G.Variant("LONG", 1.0, 0.10, True))
    g.maybe_arm(0, 0, 100.0, SIG, ret4h=0.5)
    assert not g.active
    g.maybe_arm(0, 0, 100.0, SIG, ret4h=0.001)
    assert g.active


def test_funding_charged_on_inventory():
    g = armed()
    g.trade(1_000, g.levels[g.n - 1] * 0.999)
    g.pay_funding(0.001)
    assert g.st["funding"] == pytest.approx(-g.pos * g.last_p * 0.001)


def test_low_price_coin_inventory_never_exceeds_grid_size():
    """Regression 2026-10-02: PUMP numbers (q = 17 320.5 coins) produced a float residue
    (7.3e-12 > 1e-12) that allowed one extra sell; held drifted to 10 levels with n = 9."""
    import random

    g = TickGrid(V)
    p0 = 0.006415
    g.maybe_arm(0, 0, p0, SIG, 0.0)
    rng = random.Random(1)
    t = 0
    for _ in range(20_000):
        t += 1500
        k = rng.randrange(0, g.n + 1)
        g.trade(t, g.levels[k] * (0.999 if rng.random() < 0.5 else 1.001))
        assert 0 <= g.held <= g.n
        assert g.held == g.n - g.idx
        assert g.pos == g.held * g.q
