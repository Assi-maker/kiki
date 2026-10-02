"""Daily rebalance of the P5 trend strategy (PAPER or LIVE on BingX perps).

One run per closed UTC day and mode, idempotent:
  1. take only CLOSED daily candles; the newest must be yesterday (UTC),
     otherwise refuse to trade (stale data);
  2. target weights from signal.target_weights;
  3. PAPER: mark the ledger to market, charge real BingX funding since the
     last run, trade toward the target at the close, fee + slippage;
     LIVE: equity and positions come from the exchange (source of truth),
     market orders on the LONG bucket toward the target, then a fresh
     exchange-side catastrophe STOP_MARKET under every open position;
  4. a trade smaller than rebalance_band x equity is skipped (cost control),
     except a move to zero (a signal exit is always executed).

LIVE refuses to run under CRYPTO_TRADING_SAFE_MODE, without credentials,
or with a SHORT position on a strategy symbol (not ours - never touched).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Protocol

import yaml

from crypto_trading.trend.signal import TrendConfig, target_weights
from crypto_trading.trend.store import TrendStore

log = logging.getLogger("crypto_trading.trend")

DAY_MS = 86_400_000
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "trend_strategy.yaml"
HISTORY_DAYS = 120
STOP_TAG = "s"


class StaleDataError(RuntimeError):
    pass


class LiveNotAllowedError(RuntimeError):
    pass


class MarketData(Protocol):
    def get_klines(self, symbol: str, interval: str, limit: int = 100,
                   start_time_ms: int | None = None, end_time_ms: int | None = None) -> list[dict]: ...
    def get_funding_rate(self, symbol: str, limit: int = 1, start_time_ms: int | None = None,
                         end_time_ms: int | None = None) -> list[dict]: ...
    def get_contracts(self) -> list[dict]: ...


def load_config(path: Path = CONFIG_PATH) -> TrendConfig:
    return TrendConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))


def closed_day(now: datetime) -> str:
    """The most recent fully closed UTC day."""
    return (now.astimezone(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")


def daily_closes(market: MarketData, symbol: str, now_ms: int) -> tuple[list[float], int]:
    rows = market.get_klines(symbol, "1d", limit=HISTORY_DAYS + 2)
    closed = sorted((int(r["time"]), float(r["close"])) for r in rows if int(r["time"]) + DAY_MS <= now_ms)
    for (t0, _), (t1, _) in zip(closed, closed[1:]):
        if t1 - t0 != DAY_MS:
            raise StaleDataError(f"{symbol}: gap in daily candles at {t0}->{t1}")
    if not closed:
        raise StaleDataError(f"{symbol}: no closed daily candles")
    return [c for _, c in closed], closed[-1][0]


@dataclass
class Contract:
    step: Decimal
    min_qty: Decimal
    min_usdt: Decimal
    price_step: Decimal


def contracts(market: MarketData, symbols: list[str]) -> dict[str, Contract]:
    out = {}
    for c in market.get_contracts():
        if c["symbol"] in symbols:
            out[c["symbol"]] = Contract(
                step=Decimal(1).scaleb(-int(c["quantityPrecision"])),
                min_qty=Decimal(str(c.get("tradeMinQuantity", 0))),
                min_usdt=Decimal(str(c.get("tradeMinUSDT", 0))),
                price_step=Decimal(1).scaleb(-int(c["pricePrecision"])),
            )
    missing = set(symbols) - set(out)
    if missing:
        raise StaleDataError(f"contracts missing: {sorted(missing)}")
    return out


def round_qty(qty: float, step: Decimal) -> Decimal:
    return Decimal(str(max(qty, 0.0))).quantize(step, rounding=ROUND_DOWN)


def plan_trades(current: dict[str, Decimal], target: dict[str, Decimal], prices: dict[str, float],
                equity: float, cfg: TrendConfig, specs: dict[str, Contract]) -> dict[str, Decimal]:
    """Signed quantity to trade per symbol (positive = buy). Band-filtered."""
    out = {}
    for sym, tgt in target.items():
        cur = current.get(sym, Decimal(0))
        diff = tgt - cur
        if diff == 0:
            continue
        exit_all = tgt == 0 and cur > 0
        notional = float(abs(diff)) * prices[sym]
        if not exit_all and notional < cfg.rebalance_band * equity:
            continue
        if abs(diff) < specs[sym].min_qty or notional < float(specs[sym].min_usdt):
            continue
        out[sym] = diff
    return out


@dataclass
class RunResult:
    mode: str
    day: str
    status: str
    weights: dict[str, float] = field(default_factory=dict)
    equity: float | None = None
    trades: dict[str, str] = field(default_factory=dict)
    detail: str = ""


def _client_id(day: str, sym: str, kind: str, n: int, now: datetime) -> str:
    """Unique per run attempt: a retry an hour later must not reuse an ID the
    exchange already saw (BingX rejects duplicate clientOrderIDs)."""
    return f"tr{day.replace('-', '')}{sym.split('-')[0][:6]}{kind}{n}{now.strftime('%H%M%S')}"[:32]


# ---------------------------------------------------------------- PAPER


def run_paper(cfg: TrendConfig, market: MarketData, store: TrendStore, now: datetime) -> RunResult:
    day = closed_day(now)
    now_ms = int(now.timestamp() * 1000)
    closes, prices, last_ts = {}, {}, {}
    for sym in cfg.assets:
        closes[sym], last_ts[sym] = daily_closes(market, sym, now_ms)
        prices[sym] = closes[sym][-1]
        if datetime.fromtimestamp(last_ts[sym] / 1000, UTC).strftime("%Y-%m-%d") != day:
            raise StaleDataError(f"{sym}: newest closed candle is not {day}")
    weights = target_weights(closes, cfg)
    specs = contracts(market, cfg.assets)

    state = store.paper_state() or {
        "equity": cfg.paper_start_equity, "cash": cfg.paper_start_equity, "last_day": None,
        "qty": {s: 0.0 for s in cfg.assets}, "last_price": dict(prices), "last_ts": now_ms,
    }
    # real funding settled since the last run: a LONG pays rate x notional at the settlement mark price
    funding = 0.0
    for sym, q in state["qty"].items():
        if q > 0:
            rows = market.get_funding_rate(sym, limit=100, start_time_ms=state["last_ts"] + 1, end_time_ms=now_ms)
            funding += sum(q * float(r["markPrice"]) * float(r["fundingRate"]) for r in rows)
    state["cash"] -= funding
    equity = state["cash"] + sum(state["qty"].get(s, 0.0) * prices[s] for s in cfg.assets)

    current = {s: Decimal(str(state["qty"].get(s, 0.0))) for s in cfg.assets}
    target = {s: round_qty(weights[s] * equity / prices[s], specs[s].step) for s in cfg.assets}
    trades = plan_trades(current, target, prices, equity, cfg, specs)
    for sym, diff in trades.items():
        side = "BUY" if diff > 0 else "SELL"
        fill = prices[sym] * (1 + cfg.slippage_rate if diff > 0 else 1 - cfg.slippage_rate)
        fee = float(abs(diff)) * fill * cfg.fee_rate
        state["cash"] -= float(diff) * fill + fee
        state["qty"][sym] = float(current[sym] + diff)
        store.record_order("PAPER", day, sym, "REBAL", side, float(abs(diff)), fill, fee, None, None)
    equity = state["cash"] + sum(state["qty"][s] * prices[s] for s in cfg.assets)
    state.update(equity=equity, last_day=day, last_price=prices, last_ts=now_ms)
    store.save_paper_state(state)
    store.record_equity("PAPER", day, equity, weights, prices)
    return RunResult("PAPER", day, "DONE", weights, equity, {s: str(d) for s, d in trades.items()},
                     f"funding={funding:.4f}")


# ---------------------------------------------------------------- LIVE


class Trader(Protocol):
    def set_leverage(self, symbol: str, leverage: int = 10, side: str = "LONG") -> dict: ...
    def get_all_positions(self) -> list[dict]: ...
    def get_balance(self) -> dict: ...
    def get_open_orders(self, symbol: str) -> list[dict]: ...
    def cancel_order(self, symbol: str, order_id: str) -> dict: ...
    def place_market_order(self, symbol: str, side: str, quantity: str, client_order_id: str) -> dict: ...
    def place_stop_loss_order(self, symbol: str, quantity: str, stop_price: str, client_order_id: str) -> dict: ...


def _long_positions(trader: Trader, symbols: list[str]) -> dict[str, Decimal]:
    out = {s: Decimal(0) for s in symbols}
    for p in trader.get_all_positions():
        sym = p.get("symbol")
        if sym not in out:
            continue
        side = p.get("positionSide", "LONG")
        amt = Decimal(str(p.get("positionAmt", "0")))
        if side == "SHORT" or amt < 0:
            raise LiveNotAllowedError(f"{sym}: SHORT position present - not this strategy's, refusing to trade")
        out[sym] += amt
    return out


def run_live(cfg: TrendConfig, market: MarketData, trader: Trader, store: TrendStore, now: datetime) -> RunResult:
    day = closed_day(now)
    now_ms = int(now.timestamp() * 1000)
    closes, prices = {}, {}
    for sym in cfg.assets:
        closes[sym], last_ts = daily_closes(market, sym, now_ms)
        if datetime.fromtimestamp(last_ts / 1000, UTC).strftime("%Y-%m-%d") != day:
            raise StaleDataError(f"{sym}: newest closed candle is not {day}")
    weights = target_weights(closes, cfg)
    specs = contracts(market, cfg.assets)
    for sym in cfg.assets:
        last = market.get_klines(sym, "1m", limit=1)
        prices[sym] = float(last[0]["close"])

    balance = trader.get_balance()
    equity = float(balance.get("equity", 0) or 0)
    if cfg.capital_cap_usdt is not None:
        equity = min(equity, cfg.capital_cap_usdt)
    if equity <= 0:
        raise LiveNotAllowedError(f"no futures equity to trade (equity={balance.get('equity')})")

    current = _long_positions(trader, cfg.assets)
    target = {s: round_qty(weights[s] * equity / prices[s], specs[s].step) for s in cfg.assets}
    trades = plan_trades(current, target, prices, equity, cfg, specs)

    done = {}
    for n, (sym, diff) in enumerate(sorted(trades.items(), key=lambda kv: kv[1])):  # sells first
        side = "BUY" if diff > 0 else "SELL"
        if side == "BUY":
            trader.set_leverage(sym, cfg.leverage, "LONG")
        cid = _client_id(day, sym, "b" if side == "BUY" else "x", n, now)
        res = trader.place_market_order(sym, side, str(abs(diff)), cid)
        store.record_order("LIVE", day, sym, "REBAL", side, float(abs(diff)), prices[sym], 0.0, cid,
                           str(res.get("orderId", "")))
        done[sym] = str(diff)
        log.info("trend LIVE %s %s %s (target %s)", side, abs(diff), sym, target[sym])

    time.sleep(2)  # let the exchange settle the fills before reading positions back
    after = _long_positions(trader, cfg.assets)
    for sym in cfg.assets:
        for o in trader.get_open_orders(sym):
            if str(o.get("clientOrderId", o.get("clientOrderID", ""))).startswith("tr") and \
                    str(o.get("type", "")).startswith("STOP"):
                trader.cancel_order(sym, str(o["orderId"]))
        if after[sym] > 0:
            stop = Decimal(str(prices[sym] * (1 - cfg.catastrophe_stop_pct))).quantize(
                specs[sym].price_step, rounding=ROUND_DOWN)
            cid = _client_id(day, sym, STOP_TAG, 0, now)
            res = trader.place_stop_loss_order(sym, str(after[sym]), str(stop), cid)
            store.record_order("LIVE", day, sym, "STOP", "SELL", float(after[sym]), float(stop), 0.0, cid,
                               str(res.get("orderId", "")))
    store.record_equity("LIVE", day, equity, weights, prices)
    return RunResult("LIVE", day, "DONE", weights, equity, done, f"positions={ {k: str(v) for k, v in after.items()} }")


# ---------------------------------------------------------------- entry


def run_once(cfg: TrendConfig, market: MarketData, store: TrendStore, now: datetime,
             trader: Trader | None = None, safe_mode: bool = False) -> RunResult:
    day = closed_day(now)
    mode = cfg.mode
    if store.run_status(mode, day) == "DONE":
        return RunResult(mode, day, "ALREADY_DONE")
    if mode == "LIVE":
        if safe_mode:
            raise LiveNotAllowedError("CRYPTO_TRADING_SAFE_MODE is set - LIVE trend run refused")
        if trader is None:
            raise LiveNotAllowedError("LIVE mode without BingX credentials")
    store.start_run(mode, day)
    try:
        result = run_live(cfg, market, trader, store, now) if mode == "LIVE" else run_paper(cfg, market, store, now)
    except Exception as exc:
        store.finish_run(mode, day, "FAILED", {"error": repr(exc)})
        raise
    store.finish_run(mode, day, result.status, {"weights": result.weights, "equity": result.equity,
                                                "trades": result.trades, "detail": result.detail})
    return result
