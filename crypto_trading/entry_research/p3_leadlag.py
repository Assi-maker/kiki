"""P3-C: does Binance SPOT lead the PERPETUAL - and can the bot use it?

Research only. Data: p3_data.py micro sample - 1-second last prices for spot
(1s klines) and perp (aggTrades), 6 symbols x 8 days (two per period).

1. Cross-correlation of non-overlapping returns at window D (1 s ... 15 min):
   corr(spot[k], perp[k+1]) = spot leads perp by one window,
   corr(perp[k], spot[k+1]) = perp leads spot, and the same-window corr.
2. Event study: a "spot ahead" event = |spot return - perp return| over the
   last 10 s in the day's top 0.1 % (>= 60 s apart). The perp's move in the
   gap's direction is measured from t + L to t + L + H, for latency
   L in {0, 1, 5, 30, 60, 300, 900} s - what is left to trade after the bot
   has seen it. Cost: 0.16 % taker round trip (0.04 % if both legs were
   maker, which is the optimistic bound).

    python -m crypto_trading.entry_research.p3_leadlag
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research.p3_data import LEADLAG_DAYS, LEADLAG_SYMBOLS, MICRO_DB

OUT = Path("data/entry_research/p3_leadlag.json")
WINDOWS = (1, 5, 10, 30, 60, 180, 300, 900)
LATENCIES = (0, 1, 5, 30, 60, 300, 900)
HOLDS = (5, 30, 60, 300, 900)
GAP_WINDOW = 10
TOP_SHARE = 0.001
SPACING = 60
COST_TAKER_BP = 16.0
COST_MAKER_BP = 4.0
DAY_PERIOD = {
    d: p
    for d, p in zip(
        LEADLAG_DAYS,
        ["TRAIN", "TRAIN", "VALID", "VALID", "TEST", "TEST", "HOLDOUT", "HOLDOUT"],
        strict=True,
    )
}


def series(db: sqlite3.Connection, market: str, symbol: str, day: str) -> list[float] | None:
    t0 = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())
    rows = db.execute(
        "SELECT s, price FROM px1s WHERE market=? AND symbol=? AND s>=? AND s<?",
        (market, symbol, t0, t0 + 86400),
    ).fetchall()
    if len(rows) < 20000:
        return None
    px: list[float | None] = [None] * 86400
    for s, p in rows:
        px[s - t0] = p
    last = next(p for p in px if p is not None)
    out = []
    for p in px:
        last = p if p is not None else last  # forward fill: last trade so far
        out.append(last)
    return out


def corr(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 30:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / math.sqrt(va * vb)


def lead_lag(spot: list[float], perp: list[float]) -> dict:
    out = {}
    for w in WINDOWS:
        rs = [math.log(spot[i + w] / spot[i]) for i in range(0, 86400 - w, w)]
        rp = [math.log(perp[i + w] / perp[i]) for i in range(0, 86400 - w, w)]
        out[w] = {
            "same": corr(rs, rp),
            "spot_leads": corr(rs[:-1], rp[1:]),
            "perp_leads": corr(rp[:-1], rs[1:]),
        }
    return out


def events(spot: list[float], perp: list[float]) -> list[dict]:
    w = GAP_WINDOW
    gaps = []
    for t in range(w, 86400 - max(LATENCIES) - max(HOLDS)):
        g = math.log(spot[t] / spot[t - w]) - math.log(perp[t] / perp[t - w])
        gaps.append((abs(g), t, g))
    if not gaps:
        return []
    cut = sorted(x[0] for x in gaps)[int((1 - TOP_SHARE) * (len(gaps) - 1))]
    out, last = [], -(10**9)
    for a, t, g in gaps:
        if a < cut or t - last < SPACING or a == 0:
            continue
        last = t
        sgn = 1 if g > 0 else -1
        rec = {"gap_bp": g * 1e4}
        for lat in LATENCIES:
            for hold in HOLDS:
                a0, a1 = t + lat, t + lat + hold
                rec[f"L{lat}_H{hold}"] = sgn * math.log(perp[a1] / perp[a0]) * 1e4
        out.append(rec)
    return out


def main() -> None:
    db = sqlite3.connect(f"file:{MICRO_DB}?mode=ro", uri=True)
    ll = defaultdict(lambda: defaultdict(list))
    ev_by_period = defaultdict(list)
    coverage = []
    for symbol in LEADLAG_SYMBOLS:
        for day in LEADLAG_DAYS:
            spot, perp = series(db, "spot", symbol, day), series(db, "perp", symbol, day)
            if spot is None or perp is None:
                coverage.append((symbol, day, "missing"))
                continue
            coverage.append((symbol, day, "ok"))
            for w, vals in lead_lag(spot, perp).items():
                for k, v in vals.items():
                    if v is not None:
                        ll[w][k].append(v)
            ev_by_period[DAY_PERIOD[day]].extend(events(spot, perp))
    leadlag = {w: {k: st.mean(v) for k, v in d.items()} for w, d in ll.items()}
    ev_summary = {}
    for period, evs in (
        *ev_by_period.items(),
        ("ALL", [e for v in ev_by_period.values() for e in v]),
    ):
        rec = {
            "n": len(evs),
            "median_abs_gap_bp": st.median(abs(e["gap_bp"]) for e in evs) if evs else None,
        }
        for lat in LATENCIES:
            for hold in HOLDS:
                key = f"L{lat}_H{hold}"
                vals = [e[key] for e in evs]
                if vals:
                    rec[key] = {
                        "mean_bp": st.mean(vals),
                        "share_pos": sum(v > 0 for v in vals) / len(vals),
                        "net_taker_bp": st.mean(vals) - COST_TAKER_BP,
                        "net_maker_bp": st.mean(vals) - COST_MAKER_BP,
                    }
        ev_summary[period] = rec
    OUT.write_text(
        json.dumps({"coverage": coverage, "leadlag": leadlag, "events": ev_summary}, default=str)
    )
    print("ok", sum(c[2] == "ok" for c in coverage), "symbol-days")


if __name__ == "__main__":
    main()
