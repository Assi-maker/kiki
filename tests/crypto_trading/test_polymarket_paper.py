import pytest

from crypto_trading.polymarket import paper as P


def test_score_is_quadratic_in_cents_and_zero_outside_max_spread():
    assert P.score(3.0, 0.0) == 1.0
    assert P.score(3.0, 1.5) == pytest.approx(0.25)
    assert P.score(3.0, 3.0) == 0.0
    assert P.score(3.0, 4.0) == 0.0


def test_q_min_single_sided_gets_one_third_inside_band_and_nothing_outside():
    assert P.q_min(30.0, 0.0, 0.5) == pytest.approx(10.0)
    assert P.q_min(30.0, 30.0, 0.5) == pytest.approx(30.0)
    assert P.q_min(30.0, 0.0, 0.95) == 0.0
    assert P.q_min(30.0, 12.0, 0.05) == 12.0


def test_adjusted_mid_ignores_dust_levels_below_min_size():
    bids = [(0.40, 100.0), (0.48, 1.0)]
    asks = [(0.60, 100.0), (0.52, 1.0)]
    assert P.adjusted_mid(bids, asks, 20) == pytest.approx(0.50)
    # cumulative size reaches the cutoff only at the second level
    assert P.adjusted_mid([(0.40, 15.0), (0.45, 10.0)], [(0.55, 30.0)], 20) == pytest.approx(0.475)


def test_competitor_q_is_conservative_upper_bound():
    bids = [(0.49, 100.0)]
    asks = []
    # one bid-only maker truly scores q/3, the bound counts q/2
    comp = P.competitor_q(bids, asks, 0.50, 3.0)
    true_single = 100 * P.score(3.0, 1.0) / 3
    assert comp >= true_single
    assert comp == pytest.approx(100 * P.score(3.0, 1.0) / 2)


def test_quotes_are_rounded_away_from_mid_and_never_cross():
    pb, pa = P.quote_prices(0.505, 2.0, 0.01, best_bid=0.50, best_ask=0.51)
    assert pb == pytest.approx(0.48) and pa == pytest.approx(0.53)
    # tight distance cannot cross the book
    pb, pa = P.quote_prices(0.50, 0.1, 0.01, best_bid=0.49, best_ask=0.51)
    assert pb <= 0.50 and pa >= 0.50
    assert pb < 0.51 and pa > 0.49


def test_bid_fill_from_yes_trade_no_trade_or_book_moving_through():
    t = [{"outcomeIndex": 0, "price": 0.47, "timestamp": 10}]
    assert P.bid_filled(0.48, t, best_ask=0.55)
    assert not P.bid_filled(0.46, t, best_ask=0.55)
    # a No trade at 0.53 is a Yes trade at 0.47
    assert P.bid_filled(0.48, [{"outcomeIndex": 1, "price": 0.53, "timestamp": 10}], best_ask=0.55)
    assert P.bid_filled(0.48, [], best_ask=0.48)
    assert P.ask_filled(0.53, [{"outcomeIndex": 0, "price": 0.53, "timestamp": 1}], best_bid=0.40)
    assert P.ask_filled(0.53, [], best_bid=0.53)
    assert not P.ask_filled(0.53, [{"outcomeIndex": 1, "price": 0.48, "timestamp": 1}], best_bid=0.40)


def test_pair_merge_realizes_one_dollar_and_inventory_limits_quotes():
    pos = P.Position(cid="c", size=20)
    pos.fill_bid(0.48)   # +20 Yes at 0.48
    assert not pos.may_bid() and pos.may_ask()
    pos.fill_ask(0.53)   # +20 No at 0.47 -> merge 20 pairs
    assert pos.yes == 0 and pos.no == 0
    assert pos.realized == pytest.approx(20 * (1 - 0.48 - 0.47))
    assert pos.may_bid() and pos.may_ask()


def test_position_value_at_resolution_and_mark():
    pos = P.Position(cid="c", size=20)
    pos.fill_bid(0.80)
    assert pos.value(yes_price=0.0) == pytest.approx(-16.0)
    assert pos.value(yes_price=1.0) == pytest.approx(4.0)
    assert pos.value(yes_price=0.80) == pytest.approx(0.0)


def test_reward_share_uses_our_qmin_against_competitors():
    our = P.our_qmin(size=20, pb=0.48, pa=0.52, mid=0.50, v=3.0, bid_on=True, ask_on=True)
    assert our == pytest.approx(20 * P.score(3.0, 2.0))
    one = P.our_qmin(size=20, pb=0.48, pa=0.52, mid=0.50, v=3.0, bid_on=True, ask_on=False)
    assert one == pytest.approx(our / 3)
    assert P.share(our, 0.0) == 1.0
    assert P.share(our, our) == pytest.approx(0.5)
    assert P.share(0.0, 5.0) == 0.0


def test_selection_respects_budget_and_skips_short_markets_when_asked():
    now = 1_000_000
    cands = [
        dict(cid="a", rate=100, v=4.5, n=20, mid=0.5, comp=10.0, tick=0.01, bb=0.49, ba=0.51, end=now + 3600),
        dict(cid="b", rate=50, v=4.5, n=20, mid=0.5, comp=10.0, tick=0.01, bb=0.49, ba=0.51, end=now + 10 * 86400),
        dict(cid="c", rate=40, v=4.5, n=20, mid=0.5, comp=10.0, tick=0.01, bb=0.49, ba=0.51, end=now + 10 * 86400),
    ]
    picked = P.select(cands, d=2.0, budget=40.0, now=now, min_hours=0)
    assert [c["cid"] for c in picked] == ["a", "b"]
    picked = P.select(cands, d=2.0, budget=40.0, now=now, min_hours=48)
    assert [c["cid"] for c in picked] == ["b", "c"]


def test_last_trade_on_wrong_side_counts_as_adverse_fill():
    # thin book 0.12/0.49, mid 0.305, but the market last traded at 0.20
    assert P.bid_filled(0.28, [], best_ask=0.49, last_trade=0.20)
    assert not P.ask_filled(0.33, [], best_bid=0.12, last_trade=0.20)
    assert P.ask_filled(0.33, [], best_bid=0.12, last_trade=0.40)
    assert not P.bid_filled(0.28, [], best_ask=0.49, last_trade=None)


def test_market_view_reads_last_trade_and_skips_one_sided_books():
    m = dict(n=20, v=4.5)
    v = P.market_view(m, ([(0.48, 100.0)], [(0.52, 100.0)], 0.51))
    assert v["mid"] == pytest.approx(0.50) and v["ltp"] == 0.51
    assert P.market_view(m, ([], [(0.52, 100.0)], None)) is None


def test_evaluator_daily_net_and_verdict_on_synthetic_db(monkeypatch):
    import sqlite3
    from crypto_trading.polymarket import evaluate as E
    monkeypatch.setattr(E, "N_DAYS", 1)
    db = sqlite3.connect(":memory:")
    db.executescript(P.SCHEMA)
    day0 = 1_790_899_200          # 2026-10-02 00:00 UTC (warm-up day)
    day1 = day0 + 86400
    db.execute("INSERT INTO markets VALUES('c','q','y',NULL,100,4.5,20,0.01)")
    for i in range(10):
        db.execute("INSERT INTO ticks VALUES(?,0,1,0,0.1,19.0,1.0)", (day0 + 60 * i,))
    for i in range(1440):
        ts = day1 + 60 * i
        db.execute("INSERT INTO ticks VALUES(?,0,1,0,0.1,19.0,1.0)", (ts,))
        mid = 0.50 if i < 720 else 0.40
        db.execute("INSERT INTO samples VALUES('d2_all',?,'c',?,0.48,0.52,1,1,6,0,1,100,?)", (ts, mid, 100 / 1440))
    # bought 20 Yes at 0.48, market falls to 0.40 -> -1.6 inventory, +100 rewards
    db.execute("INSERT INTO fills VALUES('d2_all',?,'c','bid',0.48,20,0.45,'ltp')", (day1 + 60 * 800,))
    r = E.evaluate(db, resolve=False)
    v = r["variants"]["d2_all"]
    assert r["days"] == ["2026-10-03"]
    assert v["total_rewards"] == pytest.approx(100.0)
    assert v["inventory_pnl"] == pytest.approx(-1.6)
    assert v["daily"][0]["net"] == pytest.approx(98.4)
    assert v["fill_reasons"] == {"ltp": 1}
    assert v["verdict"] == "FAIL"     # 1 positive day < 5 required
    assert r["invariants"]["capital_ok"] and r["invariants"]["inventory_ok"]


def test_selection_skips_markets_trading_outside_our_quotes():
    base = dict(rate=100, v=4.5, n=20, mid=0.5, comp=0.0, tick=0.01, bb=0.40, ba=0.60, end=None)
    assert P.select([dict(base, cid="x", ltp=0.30)], d=2.0, budget=100, now=0, min_hours=0) == []
    assert [c["cid"] for c in P.select([dict(base, cid="y", ltp=0.50)], d=2.0, budget=100, now=0, min_hours=0)] == ["y"]
