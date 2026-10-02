"""Grid bot out-of-sample check: 14 months, all market regimes (pre-registered).

Why: on the user's own LIVE window (25 days, 59/66 coins rising) the neutral
grid lost in 12/12 variants and the long grids' profit was market beta. This
run asks whether ANY of the same 24 grids makes money across up, down and
sideways markets.

Data: P4 cache = Binance USDT-M 5m bars 2025-08-01 .. 2026-09-30, 133
symbols, real funding (cumulative `cf`). Open of a bar = previous close.
Missing bars are carried flat (no trading signal can arise from them).

Engine: grid_lab.run_grid, UNCHANGED rules, scaled to 5m bars
(sigma over 288 bars = 24 h, trend window 4 h, cooldown 60 min).
5m bars hide more intrabar path than 1m; the "no fill of an order created in
the same bar" rule keeps that conservative (it removes round trips, never adds).

Universes: ALL (133 symbols) and USER (the user's LIVE coins present on Binance).
Notional 1000 USDT per coin; portfolio figures scaled to 4 LIVE slots.

APPROVAL (a variant "works" only if ALL hold, on the ALL universe):
  1. portfolio P/L > 0 in each period TRAIN 2025-08..2026-01, VALID 02..04,
     TEST 05..07, HOLDOUT 08..09;
  2. grid harvest (P/L minus the P/L of just holding its inventory) > 0 in
     each period - i.e. the profit is not market beta;
  3. share of winning portfolio days > 50 % in each period (the user's goal:
     money DAILY).
Buy-and-hold of the same coins is reported per period for comparison.

    python -m crypto_trading.entry_research.grid_lab14 [workers]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from crypto_trading.entry_research import grid_lab as G

CACHE = Path("data/entry_research/p4_cache")
OUT = Path("docs/superpowers/reports/2026-10-02-grid-lab14.json")
T0 = 1754006400000
STEP = 300_000
BPD = 288
PERIODS = {"TRAIN": ("2025-08-01", "2026-02-01"), "VALID": ("2026-02-01", "2026-05-01"),
           "TEST": ("2026-05-01", "2026-08-01"), "HOLDOUT": ("2026-08-01", "2026-10-01")}


def period_of(day: int) -> str | None:
    iso = datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")
    for name, (a, b) in PERIODS.items():
        if a <= iso < b:
            return name
    return None


def series(k: int):
    c = np.load(CACHE / "c.npy", mmap_mode="r")[k].astype("f8")
    h = np.load(CACHE / "h.npy", mmap_mode="r")[k].astype("f8")
    lo = np.load(CACHE / "l.npy", mmap_mode="r")[k].astype("f8")
    cf = np.load(CACHE / "cf.npy", mmap_mode="r")[k]
    valid = np.flatnonzero(~np.isnan(c))
    if len(valid) < 3 * BPD:
        return None
    a, b = valid[0], valid[-1] + 1
    c, h, lo, cf = c[a:b], h[a:b], lo[a:b], cf[a:b]
    for j in range(1, len(c)):  # carry gaps flat
        if np.isnan(c[j]):
            c[j] = c[j - 1]
        if np.isnan(h[j]) or np.isnan(lo[j]):
            h[j] = lo[j] = c[j]
    t = T0 + STEP * (a + np.arange(len(c)))
    bars = []
    prev = c[0]
    for j in range(len(c)):
        o = prev
        bars.append((int(t[j]), o, max(h[j], o, c[j]), min(lo[j], o, c[j]), c[j]))
        prev = c[j]
    cfv = np.nan_to_num(cf, nan=0.0)
    d = np.diff(cfv, prepend=cfv[0])
    fund = [(int(t[j]) + STEP, float(d[j])) for j in np.flatnonzero(d != 0)]
    return bars, fund


def worker(k: int) -> dict | None:
    sym = json.loads((CACHE / "symbols.json").read_text())[k]
    s = series(k)
    if s is None:
        return None
    bars, fund = s
    closes = [b[4] for b in bars]
    sig = G.sigma_series(closes, BPD)
    out = {"symbol": sym, "daily": {}, "inv": {}, "stats": {}}
    for v in G.VARIANTS:
        daily, inv = {}, {}
        st = G.run_grid(bars, fund, sig, BPD + 1, len(bars), v, 1000.0, daily, BPD, inv)
        out["daily"][v.name] = daily
        out["inv"][v.name] = inv
        out["stats"][v.name] = st
    bh = {}
    first = {}
    for t, o, h, lo, c in bars[BPD + 1:]:
        dd = t // G.DAY
        first.setdefault(dd, o)
        bh[dd] = 1000.0 * (c / first[dd] - 1)
    out["bh"] = bh
    return out


def user_coins() -> set[str]:
    db = sqlite3.connect("file:data/entry_research/grid.db?mode=ro", uri=True)
    return {r[0].replace("-", "") for r in db.execute("SELECT symbol FROM done")}


def summarize(results: list[dict], universe: set[str] | None) -> dict:
    rs = [r for r in results if universe is None or r["symbol"] in universe]
    n = len(rs)
    summary = {"n_coins": n, "variants": {}}

    def port(key: str, vname: str | None) -> dict[int, float]:
        agg: dict[int, float] = {}
        for r in rs:
            src = r[key] if vname is None else r[key][vname]
            for d, x in src.items():
                agg[int(d)] = agg.get(int(d), 0.0) + x
        return {d: x / n * 4 for d, x in agg.items()}

    def by_period(p: dict[int, float]) -> dict:
        res = {}
        for name in PERIODS:
            xs = [x for d, x in sorted(p.items()) if period_of(d) == name]
            res[name] = {"total": sum(xs), "win_pct": 100 * sum(x > 0 for x in xs) / max(len(xs), 1),
                         "worst_day": min(xs) if xs else 0.0, "days": len(xs)}
        return res

    summary["buy_and_hold"] = by_period(port("bh", None))
    for v in G.VARIANTS:
        p = by_period(port("daily", v.name))
        inv = by_period(port("inv", v.name))
        for name in PERIODS:
            p[name]["harvest"] = p[name]["total"] - inv[name]["total"]
        ok = all(p[k]["total"] > 0 and p[k]["harvest"] > 0 and p[k]["win_pct"] > 50 for k in PERIODS)
        summary["variants"][v.name] = {"periods": p, "approved": ok,
                                       "liquidations": sum(r["stats"][v.name]["liquidations"] for r in rs),
                                       "stops": sum(r["stats"][v.name]["stops"] for r in rs)}
    return summary


def main() -> None:
    syms = json.loads((CACHE / "symbols.json").read_text())
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = [r for r in ex.map(worker, range(len(syms))) if r is not None]
    out = {"generated": datetime.now(UTC).isoformat(), "headline": G.HEADLINE.name,
           "ALL": summarize(results, None), "USER": summarize(results, user_coins())}
    OUT.write_text(json.dumps(out))
    for uni in ("ALL", "USER"):
        s = out[uni]
        print(f"\n== {uni} ({s['n_coins']} coins), 4-slot portfolio, per period: total / harvest / win% ==")
        bh = s["buy_and_hold"]
        print(f"{'buy&hold':24s} " + "  ".join(f"{k}:{bh[k]['total']:8.0f}          {bh[k]['win_pct']:4.0f}%" for k in PERIODS))
        for name, v in s["variants"].items():
            p = v["periods"]
            flag = " APPROVED" if v["approved"] else ""
            head = " <HEADLINE" if name == G.HEADLINE.name else ""
            print(f"{name:24s} " + "  ".join(f"{k}:{p[k]['total']:8.0f}/{p[k]['harvest']:7.0f} {p[k]['win_pct']:4.0f}%" for k in PERIODS)
                  + f" liq {v['liquidations']}{flag}{head}")
    print("\nAPPROVED (ALL):", [k for k, v in out["ALL"]["variants"].items() if v["approved"]])


if __name__ == "__main__":
    main()
