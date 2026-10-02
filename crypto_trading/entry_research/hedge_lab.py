"""Hedged structures lab (pre-registered before any result).

A. STRADDLE WITH STOPS (user's data: grid.db BingX 1m, 66 coins, 25 days)
   Open a LONG and a SHORT of 1000 USDT each at the same close. Each leg has
   its own take-profit +X % and stop -Y % (price move). When one leg stops,
   the other keeps running until its TP / SL or 4 h, then the next straddle
   starts. Fees: 0.05 % taker per leg per side. Same-bar TP+SL -> SL
   (pessimistic). Variants (X, Y): (0.5, 0.25), (1, 0.5), (2, 0.5), (2, 1),
   (4, 1) = 5.

B. PAIRS TRADING / STAT ARB (P4 archive: Binance 5m -> 1h, 133 coins,
   2025-08 .. 2026-09, real funding ignored on both legs - it mostly offsets)
   * universe: top 40 coins by trailing 30 d quote volume, refreshed weekly
   * every Monday 00:00: candidate pairs = all pairs inside the universe,
     ranked by correlation of 1h log returns over the previous 30 days; the
     top P = 10 pairs (each coin in at most one pair) are traded that week
   * hedge ratio beta = OLS of log(A) on log(B) over the previous 30 days;
     spread = log(A) - beta log(B); z = (spread - mean_7d) / std_7d, all
     from closed bars only
   * enter when |z| > Z_in: short the rich leg, long the cheap leg, 500 USDT
     on leg A and 500 x beta on leg B; exit when |z| < 0.5, or |z| > 4 (stop),
     or after 48 h, or at the end of the week
   * costs: 0.05 % taker + 0.05 % slippage per leg per side (entry + exit,
     both legs)
   Variants: Z_in in {1.5, 2.0, 2.5} = 3.
   Periods: H1 2025-08..2026-02, H2 2026-03..2026-09.

"Works" (each lab): total net > 0 AND net > 0 in each half / period AND
more than 50 % positive days.

    uv run --with numpy python -m crypto_trading.entry_research.hedge_lab
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from crypto_trading.entry_research.grid_data import DB as GRID_DB

FEE = 0.0005
SLIP = 0.0005
CACHE = Path("data/entry_research/p4_cache")
T0 = 1754006400000
STEP5 = 300_000


# ------------------------------------------------------------------ A. straddle

def straddle(bars, x: float, y: float) -> tuple[list[float], list[int]]:
    pnls, times = [], []
    i, n = 0, len(bars)
    while i < n - 2:
        c = bars[i][4]
        legs = {1: None, -1: None}  # side -> exit pnl
        j = i + 1
        while j < n and j <= i + 240 and (legs[1] is None or legs[-1] is None):
            h, lo = bars[j][2], bars[j][3]
            for side in (1, -1):
                if legs[side] is not None:
                    continue
                tp = c * (1 + side * x)
                sl = c * (1 - side * y)
                hit_tp = h > tp if side > 0 else lo < tp
                hit_sl = lo < sl if side > 0 else h > sl
                if hit_sl:
                    legs[side] = -y
                elif hit_tp:
                    legs[side] = x
            j += 1
        j = min(j, n - 1)
        for side in (1, -1):
            if legs[side] is None:
                legs[side] = side * (bars[j][4] / c - 1)
        pnl = 1000.0 * (legs[1] + legs[-1]) - 2 * 1000.0 * 2 * FEE
        pnls.append(pnl)
        times.append(bars[i][0])
        i = j + 1
    return pnls, times


def lab_a() -> list[dict]:
    db = sqlite3.connect(f"file:{GRID_DB}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT symbol FROM done ORDER BY symbol")]
    data = {s: db.execute("SELECT t, o, h, l, c FROM k1m WHERE symbol=? ORDER BY t", (s,)).fetchall()[1440:]
            for s in syms}
    mid = data[syms[0]][len(data[syms[0]]) // 2][0]
    out = []
    for x, y in ((0.005, 0.0025), (0.01, 0.005), (0.02, 0.005), (0.02, 0.01), (0.04, 0.01)):
        allp, h1, h2, days = [], 0.0, 0.0, {}
        coins_pos = 0
        for s in syms:
            p, t = straddle(data[s], x, y)
            allp += p
            coins_pos += sum(p) > 0
            for v, tt in zip(p, t):
                if tt < mid:
                    h1 += v
                else:
                    h2 += v
                days[tt // 86_400_000] = days.get(tt // 86_400_000, 0.0) + v
        win_days = 100 * sum(v > 0 for v in days.values()) / len(days)
        out.append({"variant": f"TP{x * 100:g}%/SL{y * 100:g}%", "trades": len(allp),
                    "win_pct": 100 * sum(v > 0 for v in allp) / len(allp), "total": sum(allp),
                    "per_trade": sum(allp) / len(allp), "H1": h1, "H2": h2, "coins_pos": coins_pos,
                    "win_days": win_days, "works": sum(allp) > 0 and h1 > 0 and h2 > 0 and win_days > 50})
    return out


# ------------------------------------------------------------------ B. pairs

def hourly():
    syms = json.loads((CACHE / "symbols.json").read_text())
    c5 = np.load(CACHE / "c.npy", mmap_mode="r")
    q5 = np.load(CACHE / "qv.npy", mmap_mode="r")
    nh = c5.shape[1] // 12
    c = np.asarray(c5[:, 11: nh * 12: 12], dtype="f8")  # close of each hour = 12th 5m bar
    qv = np.nansum(np.asarray(q5[:, : nh * 12], dtype="f8").reshape(len(syms), nh, 12), axis=2)
    t = T0 + 3_600_000 * (np.arange(nh) + 1)  # time the hourly close is known
    return syms, c, qv, t


def lab_b() -> list[dict]:
    syms, c, qv, t = hourly()
    S, H = c.shape
    logp = np.log(c)
    r = np.diff(logp, axis=1, prepend=np.nan)
    split = int((datetime(2026, 3, 1, tzinfo=UTC).timestamp() * 1000 - T0) // 3_600_000)
    cost_side = FEE + SLIP
    out = []
    for z_in in (1.5, 2.0, 2.5):
        pnl_h = np.zeros(H)
        trades = []
        # weekly loop: Mondays
        week_starts = [h for h in range(30 * 24, H) if datetime.fromtimestamp(t[h - 1] / 1000, UTC).weekday() == 0
                       and datetime.fromtimestamp(t[h - 1] / 1000, UTC).hour == 0]
        for w, h0 in enumerate(week_starts):
            h1 = week_starts[w + 1] if w + 1 < len(week_starts) else H
            lo = h0 - 30 * 24
            vol = np.nansum(qv[:, lo:h0], axis=1)
            ok = np.isfinite(c[:, lo:h0]).all(axis=1) & np.isfinite(c[:, h0 - 1])
            cand = [i for i in np.argsort(-vol) if ok[i]][:40]
            rr = r[cand][:, lo + 1:h0]
            corr = np.corrcoef(rr)
            pairs = sorted(((corr[a, b], cand[a], cand[b]) for a in range(len(cand)) for b in range(a + 1, len(cand))),
                           reverse=True)
            used, chosen = set(), []
            for _, a, b in pairs:
                if a in used or b in used:
                    continue
                chosen.append((a, b))
                used |= {a, b}
                if len(chosen) == 10:
                    break
            for a, b in chosen:
                x, y = logp[b, lo:h0], logp[a, lo:h0]
                beta = float(np.polyfit(x, y, 1)[0])
                if not (0.2 < beta < 5):
                    continue
                spread = logp[a] - beta * logp[b]
                pos = 0  # +1 = long spread (long A, short B), -1 = short spread
                entry_h = None
                for h in range(h0, h1):
                    win = spread[h - 168:h]  # 7 days of closed hours before h
                    if not np.isfinite(win).all() or not np.isfinite(spread[h]):
                        continue
                    sd = win.std()
                    if sd == 0:
                        continue
                    z = (spread[h] - win.mean()) / sd
                    if pos != 0:
                        # P/L over hour h on 500 USDT gross per leg (A) and beta-weighted B
                        ra = c[a, h] / c[a, h - 1] - 1
                        rb = c[b, h] / c[b, h - 1] - 1
                        wa, wb = 500.0, 500.0 * beta
                        pnl_h[h] += pos * (wa * ra - wb * rb)
                        if abs(z) < 0.5 or abs(z) > 4 or h - entry_h >= 48 or h == h1 - 1:
                            pnl_h[h] -= (500.0 + 500.0 * beta) * cost_side
                            trades.append((entry_h, h))
                            pos = 0
                            continue
                    if pos == 0 and abs(z) > z_in and h < h1 - 1:
                        pos = -1 if z > 0 else 1
                        entry_h = h
                        pnl_h[h] -= (500.0 + 500.0 * beta) * cost_side
        days = {}
        for h in range(H):
            if pnl_h[h] != 0:
                d = int(t[h] // 86_400_000)
                days[d] = days.get(d, 0.0) + float(pnl_h[h])
        h1s, h2s = float(pnl_h[:split].sum()), float(pnl_h[split:].sum())
        win_days = 100 * sum(v > 0 for v in days.values()) / max(len(days), 1)
        out.append({"variant": f"Z{z_in}", "trades": len(trades), "total": h1s + h2s, "H1": h1s, "H2": h2s,
                    "per_trade": (h1s + h2s) / max(len(trades), 1), "win_days": win_days,
                    "avg_day": (h1s + h2s) / max(len(days), 1),
                    "works": h1s > 0 and h2s > 0 and win_days > 50})
    return out


def main() -> None:
    a = lab_a()
    print("A. straddle with stops (66 BingX coins, 25 days, 1000 USDT per leg)")
    for row in a:
        print("  " + "  ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in row.items()))
    b = lab_b()
    print("B. pairs trading (Binance 1h, 14 months, 10 pairs, 500 USDT per leg)")
    for row in b:
        print("  " + "  ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in row.items()))
    Path("docs/superpowers/reports/2026-10-02-hedge-lab.json").write_text(json.dumps({"A": a, "B": b}, default=float))


if __name__ == "__main__":
    main()
