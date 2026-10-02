"""P6 C3 - BingX vs Binance funding differential (descriptive upper bound).

BingX's public API returns only recent funding, so the only BingX funding
history available is the 25-day window in grid.db (66 coins, 2026-09-05..
2026-10-01). For each coin: sum of BingX and Binance funding over the window.
Cross-venue carry = long the perp on the venue with LOWER funding, short on
the HIGHER one; it earns the difference. UPPER BOUND: the side is chosen with
hindsight for the whole window (no prediction cost), one entry + one exit,
4 perp legs x (0.05 % taker + 0.05 % slip) = 0.4 % of notional.

    python -m crypto_trading.entry_research.p6_crossvenue
"""

from __future__ import annotations

import json
import sqlite3
import statistics as st
import urllib.request

from crypto_trading.entry_research.grid_data import DB


def binance_funding(sym: str, a: int, b: int) -> list[tuple[int, float]]:
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={a}&endTime={b}&limit=1000"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return [(int(x["fundingTime"]), float(x["fundingRate"])) for x in json.loads(r.read())]
    except Exception:  # noqa: BLE001 - not listed on Binance
        return []


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = []
    for (sym,) in db.execute("SELECT DISTINCT symbol FROM funding"):
        bx = db.execute("SELECT t, rate FROM funding WHERE symbol=? ORDER BY t", (sym,)).fetchall()
        if len(bx) < 10:
            continue
        a, b = bx[0][0], bx[-1][0]
        bn = binance_funding(sym.replace("-", ""), a, b)
        if len(bn) < 10:
            continue
        days = (b - a) / 86_400_000
        diff = sum(r for _, r in bx) - sum(r for _, r in bn)
        rows.append((sym, days, sum(r for _, r in bx), sum(r for _, r in bn), diff))
    print(f"coins with both venues: {len(rows)}")
    cost = 0.004
    ub = [abs(d) - cost for *_, d in rows]
    print(f"window ~{st.mean(r[1] for r in rows):.1f} days")
    print(f"|BingX - Binance| funding over the window: median {100 * st.median(abs(r[4]) for r in rows):.3f} %, "
          f"mean {100 * st.mean(abs(r[4]) for r in rows):.3f} %")
    print(f"hindsight upper bound after 0.4 % costs: median {100 * st.median(ub):+.3f} %, "
          f"coins > 0: {sum(u > 0 for u in ub)}/{len(ub)}, mean {100 * st.mean(ub):+.3f} % per {st.mean(r[1] for r in rows):.0f} days")
    for sym, days, x, n, d in sorted(rows, key=lambda r: -abs(r[4]))[:8]:
        print(f"  {sym:14s} BingX {100 * x:+.3f} %  Binance {100 * n:+.3f} %  diff {100 * d:+.3f} %")


if __name__ == "__main__":
    main()
