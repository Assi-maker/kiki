"""Grid bot lab on the user's own LIVE data (pre-registered before any result).

Question: would a well-built futures GRID BOT have made money - and made it
DAILY - on exactly the coins and the period the bot traded LIVE on BingX
(112 closed LIVE trades, 66 coins, 2026-09-06 .. 2026-10-01)?

Data: grid_data.py -> continuous BingX 1m candles + BingX funding.

THE GRID (state-of-the-art retail futures grid: auto range, volatility step,
range stop, auto re-arm, trend filter):
  * sigma_d = std of 1m log returns over the previous 24 h x sqrt(1440)
  * centre P0 = last close; geometric levels L_j = P0 (1+s)^j, j = -n..n
      s = max(k_step * sigma_d, 0.20 %)          (step >= 5x maker round trip)
      n = clamp(floor(ln(1 + k_range sigma_d) / ln(1+s)), 2, 40)
  * size: q = NOTIONAL / n / P0 per level -> at most NOTIONAL (1000 USDT,
    = 100 margin x 10, the LIVE size) of inventory on one side
  * NEUTRAL: buys below / sells (shorts) above; LONG: buys below, a sell
    only ever closes a bought level (never short)
  * a filled buy at L_i places a sell at L_i+1 and vice versa (classic grid)
  * range stop: a 1m close beyond the outer level by one more step -> close
    everything at market; re-arm after >= 60 min with a fresh range
  * trend filter (variant): (re)arm only when |4 h log return| < 1 sigma_4h
  * margin guard: if the grid's running P/L reaches -90 % of the 100 USDT
    margin, close at market and count it as a LIQUIDATION.

FILL MODEL (deliberately conservative):
  * maker limit fills when the 1m path trades THROUGH the level by >= 1 bp
  * path O->L->H->C for an up bar, O->H->L->C for a down bar
  * an order created inside a bar cannot fill in that same bar (no intrabar
    ping-pong profits that a 1m bar cannot prove)
  * fees: maker 0.02 % per grid fill; market exits 0.05 % taker + 0.05 % slip
  * real BingX funding on the net position at every settlement.

TESTS:
  A. trade-for-trade: for each LIVE trade, a grid on the same coin, same
     start, same end, same notional as that trade (100 or 1000 USDT) - vs the
     trade's real P/L. A24: same but the grid runs 24 h from the trade start.
  B. continuous daily: every one of the 66 coins, the whole period, 1000 USDT
     each; P/L per coin-day (marked to market at 00:00 UTC).

VARIANTS (all reported, no picking): mode {NEUTRAL, LONG} x k_range {1, 2} x
k_step {0.05, 0.10, 0.20} x trend filter {off, on} = 24.
Headline config, chosen before any run: NEUTRAL, k_range 2, k_step 0.10,
trend filter on.

    python -m crypto_trading.entry_research.grid_lab
"""

from __future__ import annotations

import itertools
import json
import math
import sqlite3
import sys
from bisect import bisect_right
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

DB = Path("data/entry_research/grid.db")
OUT = Path("docs/superpowers/reports/2026-10-02-grid-lab.json")
MIN = 60_000
DAY = 86_400_000
MAKER = 0.0002
TAKER = 0.0005
SLIP = 0.0005
THROUGH = 0.0001
MIN_STEP = 0.002
MARGIN = 100.0
COOLDOWN = 60
LIQ_INTRABAR = False  # robustness: liquidate on the bar's adverse extreme, losing the whole margin + 0.5 % fee


@dataclass(frozen=True)
class Variant:
    mode: str
    k_range: float
    k_step: float
    trend: bool

    @property
    def name(self) -> str:
        return f"{self.mode}_R{self.k_range:g}_S{self.k_step:g}_{'TF' if self.trend else 'noTF'}"


VARIANTS = [Variant(m, r, s, t) for m, r, s, t in itertools.product(
    ("NEUTRAL", "LONG"), (1.0, 2.0), (0.05, 0.10, 0.20), (False, True))]
HEADLINE = Variant("NEUTRAL", 2.0, 0.10, True)


def load(sym: str, db: sqlite3.Connection):
    rows = db.execute("SELECT t, o, h, l, c FROM k1m WHERE symbol=? ORDER BY t", (sym,)).fetchall()
    fund = db.execute("SELECT t, rate FROM funding WHERE symbol=? ORDER BY t", (sym,)).fetchall()
    return rows, fund


def sigma_series(closes: list[float], bpd: int = 1440) -> list[float | None]:
    """sig[i] = daily vol from the bpd (bars per day) log returns ending at close i-1 (known before bar i)."""
    s1 = [0.0]
    s2 = [0.0]
    for j in range(1, len(closes)):
        r = math.log(closes[j] / closes[j - 1])
        s1.append(s1[-1] + r)
        s2.append(s2[-1] + r * r)
    out: list[float | None] = [None] * len(closes)
    for i in range(bpd + 1, len(closes)):
        a, b = s1[i - 1] - s1[i - 1 - bpd], s2[i - 1] - s2[i - 1 - bpd]  # returns j = i-bpd .. i-1 only
        var = (b - a * a / bpd) / (bpd - 1)
        out[i] = math.sqrt(max(var, 0.0)) * math.sqrt(bpd)
    return out


def run_grid(bars, fund, sig_s, i0: int, i1: int, v: Variant, notional: float, daily: dict | None = None,
             bpd: int = 1440, daily_inv: dict | None = None) -> dict:
    """Simulate one grid account on bars[i0:i1] (indices into the full series)."""
    T = [b[0] for b in bars]
    C = [b[4] for b in bars]
    ftimes = [f[0] for f in fund]
    fi = bisect_right(ftimes, T[i0] - 1)
    cash = 0.0
    pos = 0.0
    active = False
    cooldown_until = i0
    stats = {"fills": 0, "stops": 0, "liquidations": 0, "arms": 0, "fees": 0.0, "funding": 0.0,
             "inv_pnl": 0.0, "exposure_min": 0.0}
    levels: list[float] = []
    idx = n = 0
    q = s = 0.0
    arm_equity = 0.0
    last_day = None
    day_open_eq = 0.0

    def close_all(price: float) -> None:
        nonlocal cash, pos
        if pos != 0:
            px = price * (1 - SLIP) if pos > 0 else price * (1 + SLIP)
            cash += pos * px
            fee = abs(pos) * px * TAKER
            cash -= fee
            stats["fees"] += fee
            pos = 0.0

    for i in range(i0, i1):
        t, o, h, lo, c = bars[i]
        if daily is not None:
            d = t // DAY
            if d != last_day:
                eq = cash + pos * o
                if last_day is not None:
                    daily[last_day] = daily.get(last_day, 0.0) + eq - day_open_eq
                last_day, day_open_eq = d, eq
        while fi < len(fund) and ftimes[fi] <= t:
            if pos != 0:
                fpay = pos * C[i - 1] * fund[fi][1]
                cash -= fpay
                stats["funding"] -= fpay
            fi += 1
        if not active and i >= cooldown_until:
            sig = sig_s[i]
            ok = sig is not None and sig > 0
            w4h = bpd // 6
            if ok and v.trend and i > w4h:
                ok = abs(math.log(C[i - 1] / C[i - 1 - w4h])) < sig * math.sqrt(1 / 6)
            if ok:
                p0 = C[i - 1]
                s = max(v.k_step * sig, MIN_STEP)
                n = max(2, min(40, int(math.log(1 + v.k_range * sig) / math.log(1 + s))))
                levels = [p0 * (1 + s) ** j for j in range(-n, n + 1)]  # levels[k] = L_{k-n}
                idx = n
                q = notional / n / p0
                active = True
                arm_equity = cash + pos * p0
                stats["arms"] += 1
        if pos != 0:  # analysis only: P/L of simply HOLDING the inventory carried into this bar
            stats["inv_pnl"] += pos * (c - C[i - 1])
            if daily_inv is not None:
                daily_inv[t // DAY] = daily_inv.get(t // DAY, 0.0) + pos * (c - C[i - 1])
            stats["exposure_min"] += abs(pos) * c / notional
        if active:
            path = (o, lo, h, c) if c >= o else (o, h, lo, c)
            fresh: set[int] = set()
            for a, b in zip(path, path[1:]):
                if b < a:  # moving down: buys below idx
                    while idx > 0 and (idx - 1) not in fresh and b <= levels[idx - 1] * (1 - THROUGH):
                        px = levels[idx - 1]
                        pos += q
                        cash -= q * px
                        fee = q * px * MAKER
                        cash -= fee
                        stats["fees"] += fee
                        stats["fills"] += 1
                        idx -= 1
                        fresh.add(idx + 1)
                elif b > a:  # moving up: sells above idx
                    while idx < 2 * n and (idx + 1) not in fresh and b >= levels[idx + 1] * (1 + THROUGH):
                        if v.mode == "LONG" and pos <= 1e-12:
                            break
                        px = levels[idx + 1]
                        pos -= q
                        cash += q * px
                        fee = q * px * MAKER
                        cash -= fee
                        stats["fees"] += fee
                        stats["fills"] += 1
                        idx += 1
                        fresh.add(idx - 1)
                if v.mode == "LONG" and pos < 1e-12:
                    pos = 0.0
            eq = cash + pos * c
            worst = cash + pos * (lo if pos > 0 else h)
            if LIQ_INTRABAR and worst - arm_equity <= -MARGIN * notional / 1000.0:
                cash = arm_equity - MARGIN * notional / 1000.0 - 0.005 * notional
                pos = 0.0
                stats["liquidations"] += 1
                active = False
                cooldown_until = i + COOLDOWN * bpd // 1440
            elif eq - arm_equity <= -0.9 * MARGIN * notional / 1000.0:
                close_all(c)
                stats["liquidations"] += 1
                active = False
                cooldown_until = i + COOLDOWN * bpd // 1440
            elif c > levels[-1] * (1 + s) or c < levels[0] * (1 - s):
                close_all(c)
                stats["stops"] += 1
                active = False
                cooldown_until = i + COOLDOWN * bpd // 1440
    close_all(C[i1 - 1])
    if daily is not None and last_day is not None:
        daily[last_day] = daily.get(last_day, 0.0) + cash - day_open_eq
    stats["pnl"] = cash
    return stats


def _index(bars, ms: int) -> int:
    return bisect_right([b[0] for b in bars], ms) - 1


def worker(sym: str) -> dict:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    bars, fund = load(sym, db)
    sig_s = sigma_series([b[4] for b in bars])
    from crypto_trading.entry_research.grid_data import live_trades

    trades = [t for t in live_trades() if t["symbol"] == sym]
    out = {"symbol": sym, "A": {}, "A24": {}, "B": {}, "Bdaily": {}}
    for v in VARIANTS:
        a, a24 = [], []
        for t in trades:
            s_ms = int(t["start"].timestamp() * 1000)
            e_ms = int(t["end"].timestamp() * 1000)
            i0 = _index(bars, s_ms) + 1
            i1 = max(_index(bars, e_ms) + 1, i0 + 1)
            notional = float(t["notional"] or 1000)
            a.append(run_grid(bars, fund, sig_s, i0, i1, v, notional)["pnl"])
            i24 = min(i0 + 1440, len(bars))
            a24.append(run_grid(bars, fund, sig_s, i0, i24, v, notional)["pnl"])
        out["A"][v.name], out["A24"][v.name] = a, a24
        daily: dict = {}
        first = 1441
        out["B"][v.name] = run_grid(bars, fund, sig_s, first, len(bars), v, 1000.0, daily)
        out["Bdaily"][v.name] = {str(k): d for k, d in daily.items()}
    return out


def real_trade_pnl(t: dict) -> float:
    gross = (float(t["exit"]) - float(t["entry"])) * float(t["qty"])
    notional = float(t["entry"]) * float(t["qty"])
    fees = float(t["fees"]) if t["fees"] is not None else notional * TAKER * 2
    funding = float(t["funding"] or 0)
    return gross - fees - funding


def main() -> None:
    from crypto_trading.entry_research.grid_data import live_trades

    trades = live_trades()
    syms = sorted({t["symbol"] for t in trades})
    with ProcessPoolExecutor(max_workers=int(sys.argv[1]) if len(sys.argv) > 1 else 3) as ex:
        results = list(ex.map(worker, syms))
    real = [real_trade_pnl(t) for t in trades]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated": datetime.now(UTC).isoformat(), "real_trades": real,
                               "variants": [asdict(v) | {"name": v.name} for v in VARIANTS],
                               "headline": HEADLINE.name, "results": results}))
    print("written", OUT)


if __name__ == "__main__":
    main()
