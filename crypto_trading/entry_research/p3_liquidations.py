"""P3-A: what liquidation data can be had, and what a 1-day sample shows.

No free liquidation HISTORY exists (checked 2026-10-01): Binance publishes
none (data.binance.vision liquidationSnapshot is empty), Coinalyze/CoinGlass
need keys, OKX's public endpoint pages back only ~24 h. So P3-A cannot be
tested out of sample; this script is a DESCRIPTIVE sample only:

OKX filled liquidations (last ~24 h) for FAMILIES are bucketed per minute
into long- and short-liquidation USD; a cluster = a minute in the top 1 % of
total liquidation USD for that instrument. The Binance perp's return after
the cluster minute is measured at 5/15/30/60/120/240 min, in the direction
of a REVERSAL (after long liquidations: up) - the classic hypothesis - and
the share of the move that happened BEFORE the bot could know (the cluster
minute itself) is reported.

    python -m crypto_trading.entry_research.p3_liquidations
"""

from __future__ import annotations

import json
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

import httpx

OUT = Path("data/entry_research/p3_liquidations_sample.json")
FAMILIES = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "SUI", "LTC"]
HORIZONS = (5, 15, 30, 60, 120, 240)


def okx_liquidations(client: httpx.Client, fam: str) -> list[dict]:
    out, after = [], None
    for _ in range(40):
        params = {"instType": "SWAP", "instFamily": f"{fam}-USDT", "state": "filled", "limit": 100}
        if after:
            params["after"] = after
        data = (
            client.get(
                "https://www.okx.com/api/v5/public/liquidation-orders", params=params, timeout=15
            )
            .json()
            .get("data")
            or []
        )
        details = data[0]["details"] if data else []
        if not details:
            break
        out.extend(details)
        after = min(int(d["ts"]) for d in details)
        time.sleep(0.25)
    return out


def binance_1m(client: httpx.Client, symbol: str, start_ms: int, end_ms: int) -> dict[int, float]:
    closes, cursor = {}, start_ms
    while cursor < end_ms:
        rows = client.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": cursor, "limit": 1500},
            timeout=15,
        ).json()
        if not rows:
            break
        for r in rows:
            closes[int(r[0]) // 60000] = float(r[4])
        cursor = int(rows[-1][0]) + 60000
        time.sleep(0.1)
    return closes


def main() -> None:
    results = {"families": {}, "clusters": []}
    with httpx.Client() as client:
        for fam in FAMILIES:
            liqs = okx_liquidations(client, fam)
            if not liqs:
                continue
            per_min: dict[int, dict[str, float]] = defaultdict(lambda: {"long": 0.0, "short": 0.0})
            for d in liqs:
                usd = float(d["sz"]) * float(d["bkPx"])  # contracts x price; relative use only
                side = "long" if d.get("posSide") == "long" else "short"
                per_min[int(d["ts"]) // 60000][side] += usd
            mins = sorted(per_min)
            totals = sorted(v["long"] + v["short"] for v in per_min.values())
            cut = totals[int(0.99 * (len(totals) - 1))]
            closes = binance_1m(
                client, f"{fam}USDT", (mins[0] - 30) * 60000, (mins[-1] + 250) * 60000
            )
            results["families"][fam] = {
                "liquidations": len(liqs),
                "minutes": len(mins),
                "first": mins[0] * 60,
                "last": mins[-1] * 60,
            }
            for m in mins:
                v = per_min[m]
                if v["long"] + v["short"] < cut or m not in closes or (m - 1) not in closes:
                    continue
                dom = "long" if v["long"] >= v["short"] else "short"
                rev = 1 if dom == "long" else -1  # long liquidations -> reversal is UP
                rec = {
                    "family": fam,
                    "minute": m * 60,
                    "dominant": dom,
                    "move_in_cluster_minute_pct": (closes[m] / closes[m - 1] - 1) * 100,
                }
                for h in HORIZONS:
                    if m + h in closes:
                        rec[f"reversal_{h}m_pct"] = rev * (closes[m + h] / closes[m] - 1) * 100
                results["clusters"].append(rec)
    summary = {}
    for h in HORIZONS:
        vals = [c[f"reversal_{h}m_pct"] for c in results["clusters"] if f"reversal_{h}m_pct" in c]
        if vals:
            summary[f"{h}m"] = {
                "n": len(vals),
                "mean_pct": st.mean(vals),
                "share_reversal": sum(v > 0 for v in vals) / len(vals),
            }
    results["summary"] = summary
    OUT.write_text(json.dumps(results, default=str))
    print(json.dumps({"families": results["families"], "summary": summary}, indent=1))


if __name__ == "__main__":
    main()
