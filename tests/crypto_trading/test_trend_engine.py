from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_trading.trend import engine
from crypto_trading.trend.engine import (
    LiveNotAllowedError,
    StaleDataError,
    plan_trades,
    run_once,
)
from crypto_trading.trend.signal import TrendConfig
from crypto_trading.trend.store import TrendStore

DAY = 86_400_000
NOW = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
TODAY_MS = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)


def cfg(**kw) -> TrendConfig:
    base = dict(mode="PAPER", assets=["BTC-USDT", "ETH-USDT"], lookbacks_days=[20, 50], vol_target_annual=0.4,
                vol_window_days=30, max_scale=2.0, rebalance_band=0.05, leverage=3,
                catastrophe_stop_pct=0.2, capital_cap_usdt=None, paper_start_equity=1000.0,
                fee_rate=0.0005, slippage_rate=0.0003)
    base.update(kw)
    return TrendConfig(**base)


class FakeMarket:
    def __init__(self, trend: dict[str, float], start: dict[str, float] | None = None, last_day_ms: int = TODAY_MS):
        self.trend = trend
        self.start = start or {"BTC-USDT": 50000.0, "ETH-USDT": 2000.0}
        self.last_day_ms = last_day_ms  # open time of the still-running candle
        self.funding = {}

    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        g = self.trend[symbol]
        if interval == "1m":
            return [{"time": self.last_day_ms, "close": str(self.start[symbol] * (1 + g) ** 121)}]
        rows = []
        for i in range(122):
            t = self.last_day_ms - (121 - i) * DAY
            wiggle = 1 + 0.01 * ((i % 3) - 1)
            rows.append({"time": t, "close": str(self.start[symbol] * (1 + g) ** i * wiggle)})
        return list(reversed(rows))  # BingX returns newest first

    def get_funding_rate(self, symbol, limit=1, start_time_ms=None, end_time_ms=None):
        return self.funding.get(symbol, [])

    def get_contracts(self):
        return [
            {"symbol": "BTC-USDT", "quantityPrecision": 4, "pricePrecision": 1, "tradeMinQuantity": 0.0001,
             "tradeMinUSDT": 2},
            {"symbol": "ETH-USDT", "quantityPrecision": 2, "pricePrecision": 2, "tradeMinQuantity": 0.01,
             "tradeMinUSDT": 2},
        ]


class FakeTrader:
    def __init__(self, equity="1000", positions=None):
        self.equity = equity
        self.positions = positions or {}
        self.orders = []
        self.stops = {}
        self.cancelled = []
        self.leverage = []

    def set_leverage(self, symbol, leverage=10, side="LONG"):
        self.leverage.append((symbol, leverage, side))
        return {}

    def get_balance(self):
        return {"equity": self.equity, "availableMargin": self.equity}

    def get_all_positions(self):
        return [{"symbol": s, "positionSide": side, "positionAmt": str(q)}
                for s, (side, q) in self.positions.items() if q != 0]

    def get_open_orders(self, symbol):
        return [{"orderId": oid, "clientOrderId": cid, "type": "STOP_MARKET"}
                for oid, (sym, cid) in self.stops.items() if sym == symbol]

    def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        self.stops.pop(order_id)
        return {}

    def place_market_order(self, symbol, side, quantity, client_order_id):
        self.orders.append((symbol, side, Decimal(quantity), client_order_id))
        side0, q = self.positions.get(symbol, ("LONG", Decimal(0)))
        q = Decimal(str(q)) + (Decimal(quantity) if side == "BUY" else -Decimal(quantity))
        self.positions[symbol] = ("LONG", q)
        return {"orderId": f"o{len(self.orders)}"}

    def place_stop_loss_order(self, symbol, quantity, stop_price, client_order_id):
        oid = f"s{len(self.stops)}-{client_order_id}"
        self.stops[oid] = (symbol, client_order_id)
        self.last_stop = (symbol, Decimal(quantity), Decimal(stop_price))
        return {"orderId": oid}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)


@pytest.fixture
def store(tmp_path):
    s = TrendStore(tmp_path / "trend.db")
    yield s
    s.close()


def test_paper_buys_in_uptrend_and_is_idempotent(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    r = run_once(cfg(), m, store, NOW)
    assert r.status == "DONE" and r.day == "2026-10-01"
    assert all(w > 0 for w in r.weights.values())
    st = store.paper_state()
    assert st["qty"]["BTC-USDT"] > 0 and st["qty"]["ETH-USDT"] > 0
    assert r.equity == pytest.approx(1000.0, rel=0.01)  # only fees + slippage lost
    again = run_once(cfg(), m, store, NOW)
    assert again.status == "ALREADY_DONE"
    assert len(store.db.execute("SELECT * FROM orders").fetchall()) == 2


def test_paper_flat_in_downtrend(store):
    m = FakeMarket({"BTC-USDT": -0.004, "ETH-USDT": -0.004})
    r = run_once(cfg(), m, store, NOW)
    assert r.weights == {"BTC-USDT": 0.0, "ETH-USDT": 0.0}
    assert r.trades == {}
    assert r.equity == pytest.approx(1000.0)


def test_paper_marks_to_market_and_charges_funding(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    run_once(cfg(), m, store, NOW)
    qty = store.paper_state()["qty"]["BTC-USDT"]
    m2 = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004}, last_day_ms=TODAY_MS + DAY)
    m2.funding["BTC-USDT"] = [{"fundingRate": "0.001", "markPrice": "60000"}]
    r = run_once(cfg(), m2, store, datetime(2026, 10, 3, 0, 30, tzinfo=UTC))
    assert r.status == "DONE"
    assert f"funding={qty * 60:.4f}" == r.detail


def test_stale_data_refuses_and_records_failure(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004}, last_day_ms=TODAY_MS - 2 * DAY)
    with pytest.raises(StaleDataError):
        run_once(cfg(), m, store, NOW)
    assert store.run_status("PAPER", "2026-10-01") == "FAILED"
    assert store.paper_state() is None


def test_live_refused_in_safe_mode_and_without_trader(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    with pytest.raises(LiveNotAllowedError):
        run_once(cfg(mode="LIVE"), m, store, NOW, trader=FakeTrader(), safe_mode=True)
    with pytest.raises(LiveNotAllowedError):
        run_once(cfg(mode="LIVE"), m, store, NOW, trader=None)
    assert store.run_status("LIVE", "2026-10-01") is None


def test_live_opens_positions_with_catastrophe_stops(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    t = FakeTrader(equity="1000")
    r = run_once(cfg(mode="LIVE"), m, store, NOW, trader=t)
    assert r.status == "DONE"
    assert {o[1] for o in t.orders} == {"BUY"}
    assert all(lev == 3 for _, lev, _ in t.leverage)
    assert len(t.stops) == 2
    sym, qty, stop = t.last_stop
    price = float(m.get_klines(sym, "1m")[0]["close"])
    assert float(stop) == pytest.approx(price * 0.8, rel=1e-3)
    assert qty == t.positions[sym][1]
    gross = sum(float(q) * float(m.get_klines(s, "1m")[0]["close"]) for s, (_, q) in t.positions.items())
    assert gross <= 2.0 * 1000 + 1e-6


def test_live_exit_sells_everything_and_removes_stops(store):
    m = FakeMarket({"BTC-USDT": -0.004, "ETH-USDT": -0.004})
    t = FakeTrader(equity="1000", positions={"BTC-USDT": ("LONG", Decimal("0.0003"))})
    t.stops["old"] = ("BTC-USDT", "tr20260930BTCs0")
    r = run_once(cfg(mode="LIVE"), m, store, NOW, trader=t)
    assert r.status == "DONE"
    assert t.orders == [("BTC-USDT", "SELL", Decimal("0.0003"), t.orders[0][3])]  # tiny exit still executed
    assert t.positions["BTC-USDT"][1] == 0
    assert "old" in t.cancelled and t.stops == {}


def test_live_ignores_foreign_stop_orders(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    t = FakeTrader()
    t.stops["manual"] = ("BTC-USDT", "userstop1")
    run_once(cfg(mode="LIVE"), m, store, NOW, trader=t)
    assert "manual" in t.stops


def test_live_refuses_with_short_position(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    t = FakeTrader(positions={"ETH-USDT": ("SHORT", Decimal("1"))})
    with pytest.raises(LiveNotAllowedError):
        run_once(cfg(mode="LIVE"), m, store, NOW, trader=t)
    assert t.orders == []


def test_live_refuses_without_equity(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    with pytest.raises(LiveNotAllowedError):
        run_once(cfg(mode="LIVE"), m, store, NOW, trader=FakeTrader(equity="0"))


def test_live_capital_cap_limits_size(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})
    t = FakeTrader(equity="100000")
    r = run_once(cfg(mode="LIVE", capital_cap_usdt=500.0), m, store, NOW, trader=t)
    assert r.equity == 500.0
    gross = sum(float(q) * float(m.get_klines(s, "1m")[0]["close"]) for s, (_, q) in t.positions.items())
    assert gross <= 2.0 * 500 + 1e-6


def test_plan_trades_band_and_minimums():
    specs = {"X": engine.Contract(Decimal("0.01"), Decimal("0.01"), Decimal("2"), Decimal("0.01"))}
    c = cfg(assets=["X"])
    p = {"X": 100.0}
    # 0.4 units = 40 USDT < 5 % of 1000 -> skipped
    assert plan_trades({"X": Decimal("5")}, {"X": Decimal("5.4")}, p, 1000.0, c, specs) == {}
    # 0.6 units = 60 USDT -> traded
    assert plan_trades({"X": Decimal("5")}, {"X": Decimal("5.6")}, p, 1000.0, c, specs) == {"X": Decimal("0.6")}
    # full exit always executed, even below band
    assert plan_trades({"X": Decimal("0.1")}, {"X": Decimal("0")}, p, 1000.0, c, specs) == {"X": Decimal("-0.1")}
    # below exchange minimum notional -> skipped
    assert plan_trades({"X": Decimal("0")}, {"X": Decimal("0.01")}, p, 0.0, c, specs) == {}


def test_live_retry_after_failure_uses_new_client_ids(store):
    m = FakeMarket({"BTC-USDT": 0.004, "ETH-USDT": 0.004})

    class FailingOnce(FakeTrader):
        calls = 0

        def place_market_order(self, symbol, side, quantity, client_order_id):
            FailingOnce.calls += 1
            if FailingOnce.calls == 2:
                raise RuntimeError("network")
            return super().place_market_order(symbol, side, quantity, client_order_id)

    t = FailingOnce()
    with pytest.raises(RuntimeError):
        run_once(cfg(mode="LIVE"), m, store, NOW, trader=t)
    assert store.run_status("LIVE", "2026-10-01") == "FAILED"
    first_ids = {o[3] for o in t.orders}
    r = run_once(cfg(mode="LIVE"), m, store, datetime(2026, 10, 2, 1, 30, tzinfo=UTC), trader=t)
    assert r.status == "DONE"
    retry_ids = {o[3] for o in t.orders} - first_ids
    assert retry_ids and not (retry_ids & first_ids)
    assert len(r.trades) == 1  # the already-filled leg is not bought twice
    assert all(len(cid) <= 32 for cid in first_ids | retry_ids)


def test_status_works_without_the_live_trading_stack(tmp_path, monkeypatch, capsys):
    # the Railway PAPER image has no uvicorn, so importing the live connector fails there
    from crypto_trading.trend import __main__ as cli

    def no_stack():
        raise ModuleNotFoundError("No module named 'uvicorn'")

    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "trend.db")
    monkeypatch.setattr(cli, "_trader", no_stack)
    assert cli.cmd_status() == 0
    assert "LIVE account: not available (No module named 'uvicorn')" in capsys.readouterr().out
