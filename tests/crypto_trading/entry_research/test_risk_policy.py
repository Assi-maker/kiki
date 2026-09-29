from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.entry_research import risk_policy as rp
from crypto_trading.shadow.evaluation import Bar

T0 = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
A = rp.Policy("A", Decimal("0.10"), Decimal("0.05"), "FIXED")


def _flat(start, n, price=100.0):
    return [Bar(start + timedelta(minutes=i), price, price, price, price) for i in range(n)]


def _opp(sym="X-USDT", stop=99.0, target=105.0, decided=T0, cid="c"):
    return rp.Opportunity(cid, sym, decided - timedelta(minutes=15), decided, stop, target)


def test_path_enters_after_the_decision_and_hits_the_target():
    bars = _flat(T0, 30)
    bars[10] = Bar(bars[10].t, 100, 106, 100, 105)
    p = rp.simulate_path(bars, _opp())
    assert p.entry_at == T0 + timedelta(minutes=1)
    assert p.reason == "TP" and p.exit == 105.0


def test_profit_protection_break_even_is_active_only_from_the_next_bar():
    bars = _flat(T0, 30)
    bars[5] = Bar(bars[5].t, 100, 101.2, 100, 101)       # +1 % reached
    bars[6] = Bar(bars[6].t, 101, 101, 99.5, 99.8)        # back through entry
    p = rp.simulate_path(bars, _opp())
    assert p.be_at == bars[5].t + timedelta(minutes=1)
    assert p.reason == "BE"
    assert p.exit == pytest.approx(100 * (1 - rp.STOP_SLIP))


def test_an_entry_already_through_the_stop_is_not_an_opportunity():
    assert rp.simulate_path(_flat(T0, 30, price=98.0), _opp()) is None


def test_policy_a_is_full_size_or_nothing():
    bars = _flat(T0, 400)
    path = rp.simulate_path(bars, _opp(stop=99.0))  # ~1.7 % worst case on 1000 -> ~17 USDT
    qty, reason = rp.decide(A, _opp(stop=99.0), path, Decimal("420"), [], path.entry_at)
    assert reason == "APPROVE" and qty == Decimal("10.000")
    wide = _opp(stop=96.0)
    qty, reason = rp.decide(A, wide, rp.simulate_path(bars, wide), Decimal("420"), [], path.entry_at)
    assert qty == 0 and "GROUP_RISK_CAP" in reason


def test_allocation_policy_never_exceeds_the_same_caps_and_never_exceeds_base():
    alloc = rp.Policy("C", Decimal("0.10"), Decimal("0.05"), "ALLOC")
    bars = _flat(T0, 400)
    wide = _opp(stop=96.0)
    path = rp.simulate_path(bars, wide)
    qty, reason = rp.decide(alloc, wide, path, Decimal("420"), [], path.entry_at)
    assert reason == "ALLOCATED" and 0 < qty < Decimal("10")
    risk = rp.worst_case_risk_usdt(qty, Decimal("100"), Decimal("96"), rp._limits(alloc))
    assert risk <= Decimal("420") * Decimal("0.05") + Decimal("0.01")


def test_a_break_even_protected_open_trade_frees_risk_only_after_it_happened():
    bars = _flat(T0, 400)
    bars[3] = Bar(bars[3].t, 100, 101.5, 100, 101)   # first trade reaches +1 % at minute 3
    first = _opp(sym="A-USDT", stop=99.0, cid="a")
    p1 = rp.simulate_path(bars, first)
    open_trades = [rp.OpenTrade(first, p1, Decimal("10"), 17.0)]
    second = _opp(sym="B-USDT", stop=99.0, cid="b")
    p2 = rp.simulate_path(_flat(T0, 400), second)
    # before break-even is active: group 17 + 17 > 21 -> reject
    assert rp.decide(A, second, p2, Decimal("420"), open_trades, T0 + timedelta(minutes=2))[0] == 0
    # after: the first trade only carries its cost risk -> approve
    assert rp.decide(A, second, p2, Decimal("420"), open_trades, T0 + timedelta(minutes=10))[1] == "APPROVE"


def test_replay_respects_max_positions_and_one_per_symbol():
    opps = []
    for i, sym in enumerate(["A", "B", "C", "D", "E", "A"]):
        o = _opp(sym=f"{sym}-USDT", stop=99.95, decided=T0 + timedelta(seconds=i), cid=f"c{i}")
        opps.append((o, rp.simulate_path(_flat(T0, 500), o)))
    loose = rp.Policy("loose", Decimal("1"), Decimal("1"), "FIXED")
    res = rp.replay(loose, opps)
    assert len(res.trades) == 4
    assert res.rejected.get("MAX_POSITIONS", 0) + res.rejected.get("ONE_PER_SYMBOL", 0) == 2
