"""Tick-level validation of the buy-only grid (pre-registered before any result).

Question: do the 4 buy-only grids that passed grid_lab14 on 5m candles keep
a real edge when fills come from ACTUAL Binance trades instead of candle wicks?

PRE-REGISTERED DESIGN (no change after results):
  Variants: exactly the 4 grid_lab14 approvals - LONG_R1_S0.1_noTF,
    LONG_R1_S0.1_TF, LONG_R1_S0.2_noTF, LONG_R1_S0.2_TF. Same rules as
    grid_lab.run_grid (sigma from the previous 1440 1m closes, range +-1 sigma,
    step k*sigma >= 0.20 %, n in [2, 40], 1000 USDT max inventory, range stop
    and -90 % margin guard on the 1m close, 60 min cooldown, 4 h trend filter).
  Blocks: FALL 2025-10-01 .. 2025-11-30 (coin median -25 % / -23 %),
          MIXED 2026-04-01 .. 2026-05-31 (+8 % / -2 %). One warm-up day each.
  Coins: the user's LIVE coins in the Binance universe, excluding BTC/ETH (file
    size), ranked by mean 5m quote volume in 2025-10, top 15 with data in both
    blocks (selection() - computed from volume only, never from grid results).
  Data: Binance USDT-M aggTrades (data.binance.vision), every trade in order.
  EXACT FILL: a resting buy at L fills only when a trade prints STRICTLY below L
    (the level was swept, queue position irrelevant); a sell at L only on a trade
    strictly above L. A counter order is live 1 s after its fill (latency).
    A level whose counter order is not live yet blocks the levels behind it (as
    the candle engine's fresh-order rule).
  Exchange liquidation on every trade: equity since arming <= -100 % margin ->
    whole margin + 0.5 % of notional lost.
  Fees: maker 0.02 % per grid fill; stop / guard exits 0.05 % taker + 0.05 % slip.
  Funding: real Binance funding at each settlement on the open inventory.
  Comparisons on the SAME trades: candle engine (grid_lab.run_grid, its own
    1 bp rule) on 1m and 5m bars built from these trades; buy-and-hold 1000 USDT.

VERDICT RULE: the hypothesis survives only if, at tick level, at least one
variant has portfolio P/L after fees > 0 in BOTH blocks. Otherwise it is
classified NOT VERIFIED and nothing is built on it.
ROBUSTNESS (run only if it survives): touch fills (trade <= L), 5 s latency.

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.grid_tick [workers]
"""

from __future__ import annotations

import io
import json
import math
import sys
import time
import urllib.request
import zipfile
from bisect import bisect_right
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from crypto_trading.entry_research import grid_lab as G

CACHE = Path("data/entry_research/grid_tick")
OUT = Path("docs/superpowers/reports/2026-10-02-grid-tick.json")
URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/{s}/{s}-aggTrades-{d}.zip"
BLOCKS = {"FALL": (date(2025, 10, 1), date(2025, 11, 30)), "MIXED": (date(2026, 4, 1), date(2026, 5, 31))}
VARIANT_NAMES = ["LONG_R1_S0.1_noTF", "LONG_R1_S0.1_TF", "LONG_R1_S0.2_noTF", "LONG_R1_S0.2_TF"]
VARIANTS = [v for v in G.VARIANTS if v.name in VARIANT_NAMES]
NOTIONAL = 1000.0
MIN_MS = 60_000


# ------------------------------------------------------------------ selection

def selection(k: int = 15) -> list[str]:
    import numpy as np

    from crypto_trading.entry_research import grid_lab14 as L14

    syms = json.loads((L14.CACHE / "symbols.json").read_text())
    qv = np.load(L14.CACHE / "qv.npy", mmap_mode="r")
    c = np.load(L14.CACHE / "c.npy", mmap_mode="r")
    user = L14.user_coins() - {"BTCUSDT", "ETHUSDT"}

    def col(d: date) -> int:
        return int((datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000 - L14.T0) // L14.STEP)

    a, b = col(date(2025, 10, 1)), col(date(2025, 11, 1))
    ranked = []
    for i, s in enumerate(syms):
        if s not in user:
            continue
        ok = all(not math.isnan(c[i, col(d0 - timedelta(days=1))]) and not math.isnan(c[i, col(d1)])
                 for d0, d1 in BLOCKS.values())
        v = float(np.nanmean(qv[i, a:b]))
        if ok and not math.isnan(v):
            ranked.append((v, s))
    return [s for _, s in sorted(ranked, reverse=True)[:k]]


# ------------------------------------------------------------------ data

def day_trades(sym: str, d: date) -> tuple[list[int], list[float]]:
    """(time ms, price) of every aggTrade that day, consecutive equal prices collapsed."""
    import pandas as pd

    raw = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(URL.format(s=sym, d=d.isoformat()), timeout=60) as r:
                raw = r.read()
            break
        except Exception:  # noqa: BLE001 - retry network
            time.sleep(2 * (attempt + 1))
    if raw is None:
        raise RuntimeError(f"download failed {sym} {d}")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as f:
            head = f.read(20)
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, header=0 if head.startswith(b"agg") else None, usecols=[1, 5],
                             names=None if head.startswith(b"agg") else ["agg_trade_id", "price", "quantity",
                                                                          "first_trade_id", "last_trade_id",
                                                                          "transact_time", "is_buyer_maker"])
    df.columns = ["price", "t"]
    df = df.sort_values("t", kind="stable")
    keep = df.price.ne(df.price.shift())
    df = df[keep]
    return df.t.astype("int64").tolist(), df.price.astype(float).tolist()


def funding(sym: str, a_ms: int, b_ms: int) -> list[tuple[int, float]]:
    out, t = [], a_ms
    while t < b_ms:
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={t}&endTime={b_ms}&limit=1000"
        with urllib.request.urlopen(url, timeout=30) as r:
            rows = json.loads(r.read())
        out += [(int(x["fundingTime"]), float(x["fundingRate"])) for x in rows]
        if len(rows) < 1000:
            break
        t = int(rows[-1]["fundingTime"]) + 1
    return out


# ------------------------------------------------------------------ tick engine

class TickGrid:
    """grid_lab.run_grid rules, with fills decided by individual trades."""

    def __init__(self, v: G.Variant, latency_ms: int = 1000, touch: bool = False):
        self.v, self.lat, self.touch = v, latency_ms, touch
        self.cash = self.pos = 0.0
        self.active = False
        self.cooldown_until = 0  # minute index
        self.levels: list[float] = []
        self.live_at: list[int] = []
        self.idx = self.n = 0
        self.q = self.s = 0.0
        self.arm_eq = 0.0
        self.st = {"fills": 0, "stops": 0, "guards": 0, "liquidations": 0, "arms": 0, "fees": 0.0,
                   "funding": 0.0, "inv_minutes": 0.0, "max_inv": 0.0}
        self.last_p = 0.0

    def equity(self, p: float) -> float:
        return self.cash + self.pos * p

    def _market_close(self, p: float) -> None:
        if self.pos != 0:
            px = p * (1 - G.SLIP) if self.pos > 0 else p * (1 + G.SLIP)
            self.cash += self.pos * px
            fee = abs(self.pos) * px * G.TAKER
            self.cash -= fee
            self.st["fees"] += fee
            self.pos = 0.0

    def minute_close(self, mi: int, close: float) -> None:
        """Range stop and -90 % guard on the 1m close (as the candle engine)."""
        if not self.active:
            return
        if self.equity(close) - self.arm_eq <= -0.9 * G.MARGIN:
            self._market_close(close)
            self.st["guards"] += 1
            self.active, self.cooldown_until = False, mi + G.COOLDOWN
        elif close > self.levels[-1] * (1 + self.s) or close < self.levels[0] * (1 - self.s):
            self._market_close(close)
            self.st["stops"] += 1
            self.active, self.cooldown_until = False, mi + G.COOLDOWN

    def maybe_arm(self, mi: int, t_ms: int, p0: float, sig: float | None, ret4h: float | None) -> None:
        if self.active or mi < self.cooldown_until or sig is None or sig <= 0:
            return
        if self.v.trend and (ret4h is None or abs(ret4h) >= sig * math.sqrt(1 / 6)):
            return
        self.s = max(self.v.k_step * sig, G.MIN_STEP)
        self.n = max(2, min(40, int(math.log(1 + self.v.k_range * sig) / math.log(1 + self.s))))
        self.levels = [p0 * (1 + self.s) ** j for j in range(-self.n, self.n + 1)]
        self.live_at = [t_ms] * len(self.levels)
        self.idx = self.n
        self.q = NOTIONAL / self.n / p0
        self.active = True
        self.arm_eq = self.equity(p0)
        self.st["arms"] += 1

    def trade(self, t: int, p: float) -> None:
        self.last_p = p
        if not self.active:
            return
        lv, live = self.levels, self.live_at
        if self.touch:
            def below(x): return p <= x
            def above(x): return p >= x
        else:
            def below(x): return p < x
            def above(x): return p > x
        while self.idx > 0 and live[self.idx - 1] <= t and below(lv[self.idx - 1]):
            px = lv[self.idx - 1]
            self.pos += self.q
            self.cash -= self.q * px * (1 + G.MAKER)
            self.st["fees"] += self.q * px * G.MAKER
            self.st["fills"] += 1
            self.idx -= 1
            live[self.idx + 1] = t + self.lat
        while (self.idx < 2 * self.n and live[self.idx + 1] <= t and above(lv[self.idx + 1])
               and self.pos > 1e-12):
            px = lv[self.idx + 1]
            self.pos -= self.q
            self.cash += self.q * px * (1 - G.MAKER)
            self.st["fees"] += self.q * px * G.MAKER
            self.st["fills"] += 1
            self.idx += 1
            live[self.idx - 1] = t + self.lat
        if self.pos < 1e-12:
            self.pos = 0.0
        if self.pos > 0 and self.equity(p) - self.arm_eq <= -G.MARGIN:  # exchange liquidation, tick level
            self.cash = self.arm_eq - G.MARGIN - 0.005 * NOTIONAL
            self.pos = 0.0
            self.st["liquidations"] += 1
            self.active = False
            self.cooldown_until = (t // MIN_MS) + G.COOLDOWN  # in absolute minutes; compared below

    def pay_funding(self, rate: float) -> None:
        if self.pos != 0:
            f = self.pos * self.last_p * rate
            self.cash -= f
            self.st["funding"] -= f


# ------------------------------------------------------------------ one coin, one block

def run_block(sym: str, block: str, configs: dict[str, dict]) -> dict:
    """Stream the block's trades once; feed every engine config; build 1m bars."""
    d0, d1 = BLOCKS[block]
    warm = d0 - timedelta(days=1)
    t_start = int(datetime(d0.year, d0.month, d0.day, tzinfo=UTC).timestamp() * 1000)
    fund = funding(sym, t_start, int(datetime(d1.year, d1.month, d1.day, tzinfo=UTC).timestamp() * 1000) + 86_400_000)
    fi = 0
    engines = {name: TickGrid(**cfg) for name, cfg in configs.items()}
    bars: list[tuple] = []  # (t, o, h, l, c) 1m bars for the whole block incl. warm-up
    closes: list[float] = []
    s1 = [0.0]
    s2 = [0.0]
    daily = {name: {} for name in engines}
    day_open = {name: 0.0 for name in engines}
    cur_min = None
    o = h = lo = c = None
    started = False
    n_trades = 0
    day = warm

    def sigma_now() -> float | None:
        k = len(closes)
        if k < 1441:
            return None
        a, b = s1[k - 1] - s1[k - 1441], s2[k - 1] - s2[k - 1441]
        return math.sqrt(max((b - a * a / 1440) / 1439, 0.0)) * math.sqrt(1440)

    def close_minute(m: int, close: float) -> None:
        nonlocal fi
        bars.append((m * MIN_MS, o, h, lo, close))
        if closes:
            r = math.log(close / closes[-1])
            s1.append(s1[-1] + r)
            s2.append(s2[-1] + r * r)
        closes.append(close)
        next_t = (m + 1) * MIN_MS
        if next_t < t_start:
            return
        for e in engines.values():
            e.minute_close(m, close)
            e.last_p = close
            if e.pos > 0:
                e.st["inv_minutes"] += e.pos * close / NOTIONAL
                e.st["max_inv"] = max(e.st["max_inv"], e.pos * close)
        if next_t % 86_400_000 == 0:
            for name, e in engines.items():
                eq = e.equity(close)
                if next_t > t_start:
                    daily[name][str((next_t - 86_400_000) // 86_400_000)] = eq - day_open[name]
                day_open[name] = eq
        while fi < len(fund) and fund[fi][0] <= next_t:
            for e in engines.values():
                e.pay_funding(fund[fi][1])
            fi += 1
        sig = sigma_now()
        r4 = math.log(closes[-1] / closes[-241]) if len(closes) > 241 else None
        for e in engines.values():
            e.maybe_arm(m + 1, next_t, close, sig, r4)

    while day <= d1:
        ts, ps = day_trades(sym, day)
        n_trades += len(ts)
        for t, p in zip(ts, ps):
            m = t // MIN_MS
            if cur_min is None:
                cur_min, o, h, lo, c = m, p, p, p, p
            elif m != cur_min:
                close_minute(cur_min, c)
                for gap in range(cur_min + 1, m):  # minutes without trades: flat bars
                    o = h = lo = c
                    close_minute(gap, c)
                cur_min, o, h, lo = m, c, max(c, p), min(c, p)
                c = p
            else:
                h, lo, c = max(h, p), min(lo, p), p
            if t >= t_start:
                for e in engines.values():
                    e.trade(t, p)
        day += timedelta(days=1)
    close_minute(cur_min, c)
    end_p = closes[-1]
    out = {"symbol": sym, "block": block, "trades": n_trades, "engines": {}}
    for name, e in engines.items():
        e._market_close(end_p)
        eq = e.equity(end_p)
        out["engines"][name] = {"pnl": eq, "daily": daily[name], **e.st}
    # candle engine on bars built from the same trades
    i0 = next(i for i, b in enumerate(bars) if b[0] >= t_start)
    sig1 = G.sigma_series([b[4] for b in bars], 1440)
    b5 = [(bars[k][0], bars[k][1], max(x[2] for x in bars[k:k + 5]), min(x[3] for x in bars[k:k + 5]), bars[k + 4][4])
          for k in range(0, len(bars) - 4, 5)]
    i5 = next(i for i, b in enumerate(b5) if b[0] >= t_start)
    sig5 = G.sigma_series([b[4] for b in b5], 288)
    for v in VARIANTS:
        d1m, d5m = {}, {}
        r1 = G.run_grid(bars, fund, sig1, i0, len(bars), v, NOTIONAL, d1m, 1440)
        r5 = G.run_grid(b5, fund, sig5, max(i5, 289), len(b5), v, NOTIONAL, d5m, 288)
        out["engines"][f"candle1m|{v.name}"] = {**r1, "daily": {str(k): x for k, x in d1m.items()}}
        out["engines"][f"candle5m|{v.name}"] = {**r5, "daily": {str(k): x for k, x in d5m.items()}}
    p_start = bars[i0][1]
    bh_daily, prev = {}, p_start
    for b in bars[i0:]:
        if (b[0] + MIN_MS) % 86_400_000 == 0:
            bh_daily[str(b[0] // 86_400_000)] = NOTIONAL * (b[4] - prev) / p_start
            prev = b[4]
    out["engines"]["buy_and_hold"] = {"pnl": NOTIONAL * (end_p / p_start - 1), "daily": bh_daily}
    return out


def job(args: tuple[str, str, str]) -> str:
    sym, block, mode = args
    path = CACHE / f"{mode}_{block}_{sym}.json"
    if path.exists():
        return str(path)
    if mode == "primary":
        configs = {f"tick|{v.name}": {"v": v} for v in VARIANTS}
    else:
        configs = {}
        for v in VARIANTS:
            configs[f"touch|{v.name}"] = {"v": v, "touch": True}
            configs[f"lat5s|{v.name}"] = {"v": v, "latency_ms": 5000}
    res = run_block(sym, block, configs)
    if mode != "primary":
        res["engines"] = {k: x for k, x in res["engines"].items() if k.split("|")[0] in ("touch", "lat5s")}
    path.write_text(json.dumps(res))
    return str(path)


def main() -> None:
    mode = sys.argv[2] if len(sys.argv) > 2 else "primary"
    CACHE.mkdir(parents=True, exist_ok=True)
    coins = selection()
    (CACHE / "selection.json").write_text(json.dumps(coins))
    print("coins:", coins, flush=True)
    jobs = [(s, b, mode) for b in BLOCKS for s in coins]
    with ProcessPoolExecutor(max_workers=int(sys.argv[1]) if len(sys.argv) > 1 else 4) as ex:
        for p in ex.map(job, jobs):
            print("done", p, flush=True)


if __name__ == "__main__":
    main()
