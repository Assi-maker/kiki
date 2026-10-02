"""BingX vs Binance price difference for the tick-validation coins (documentation).

The tick validation decides fills from Binance trades; the user trades on
BingX. A grid fill needs price to trade THROUGH a level, so what matters is
whether BingX prints the same lows/highs. Sample: every coin, 6 days (3 per
block), 1m candles from both venues' public APIs.

    python -m crypto_trading.entry_research.grid_venue
"""

from __future__ import annotations

import json
import statistics as st
import time
import urllib.request
from datetime import UTC, date, datetime

DAYS = [date(2025, 10, 10), date(2025, 10, 28), date(2025, 11, 17), date(2026, 4, 8), date(2026, 4, 27), date(2026, 5, 19)]
SEL = "data/entry_research/grid_tick/selection.json"


def get(url: str):
    for a in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            time.sleep(1 + a)
    return None


def binance(sym: str, t0: int) -> dict[int, tuple]:
    out = {}
    for k in range(2):
        rows = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1m&limit=720&startTime={t0 + k * 720 * 60000}") or []
        out.update({int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows})
    return out


def bingx(sym: str, t0: int) -> dict[int, tuple]:
    s = sym.replace("USDT", "-USDT")
    out = {}
    for k in range(2):
        a = t0 + k * 720 * 60000
        d = get(f"https://open-api.bingx.com/openApi/swap/v3/quote/klines?symbol={s}&interval=1m&limit=720&startTime={a}&endTime={a + 720 * 60000 - 1}")
        rows = (d or {}).get("data") or []
        out.update({int(r["time"]): (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in rows})
    return out


def main() -> None:
    coins = json.load(open(SEL))
    close_bp, low_bp, high_bp, range_ratio, deeper = [], [], [], [], []
    per = {}
    for sym in coins:
        cb, lb = [], []
        for d in DAYS:
            t0 = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
            a, b = binance(sym, t0), bingx(sym, t0)
            for t in a.keys() & b.keys():
                (ao, ah, al, ac), (bo, bh, bl, bc) = a[t], b[t]
                close_bp.append(abs(bc / ac - 1) * 1e4)
                low_bp.append((bl / al - 1) * 1e4)   # > 0: BingX low is HIGHER (shallower dip)
                high_bp.append((bh / ah - 1) * 1e4)  # < 0: BingX high is LOWER
                if ah > al and bh > bl:
                    range_ratio.append((bh - bl) / (ah - al))
                deeper.append(al < bl * (1 - 0.001))  # Binance dips >= 10 bp deeper than BingX
                cb.append(abs(bc / ac - 1) * 1e4)
                lb.append((bl / al - 1) * 1e4)
        per[sym] = (len(cb), st.median(cb) if cb else None, st.mean(lb) if lb else None)
    print(f"minutes compared: {len(close_bp)}")
    print(f"|close diff| median {st.median(close_bp):.2f} bp, p95 {sorted(close_bp)[int(.95 * len(close_bp))]:.2f} bp")
    print(f"low: BingX minus Binance mean {st.mean(low_bp):+.2f} bp (positive = BingX dips less)")
    print(f"high: BingX minus Binance mean {st.mean(high_bp):+.2f} bp (negative = BingX spikes less)")
    print(f"1m range BingX/Binance median {st.median(range_ratio):.3f}")
    print(f"minutes where Binance dipped >= 10 bp deeper than BingX: {100 * sum(deeper) / len(deeper):.2f} %")
    for s, (n, c, l) in per.items():
        print(f"  {s:14s} n={n:5d} |close| med {c if c is None else round(c, 2)} bp  low diff mean {l if l is None else round(l, 2)} bp")


if __name__ == "__main__":
    main()
