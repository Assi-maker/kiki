"""Aggregate grid_tick results: tick vs candle fills vs buy-and-hold.

    python -m crypto_trading.entry_research.grid_tick_report [primary|robust]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from crypto_trading.entry_research.grid_tick import BLOCKS, CACHE, VARIANT_NAMES

MIN_PER_BLOCK = {b: ((d1 - d0).days + 1) * 1440 for b, (d0, d1) in BLOCKS.items()}


def load(mode: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(CACHE.glob(f"{mode}_*.json"))]


def mdd(daily: dict[str, float]) -> float:
    eq = peak = 0.0
    worst = 0.0
    for k in sorted(daily, key=int):
        eq += daily[k]
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return worst


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "primary"
    res = load(mode)
    keys = sorted({k for r in res for k in r["engines"]})
    print(f"files: {len(res)}   coins per block: " + ", ".join(f"{b}={sum(r['block'] == b for r in res)}" for b in BLOCKS))
    summary = {}
    for block in BLOCKS:
        rs = [r for r in res if r["block"] == block]
        if not rs:
            continue
        print(f"\n== {block} {BLOCKS[block][0]}..{BLOCKS[block][1]}  ({len(rs)} coins x 1000 USDT) ==")
        print(f"{'engine':30s} {'P/L':>8s} {'fees':>7s} {'fund':>6s} {'fills':>7s} {'stops':>5s} {'liq':>4s} "
              f"{'MDD':>7s} {'win%':>5s} {'avgInv':>7s} {'maxInv':>7s} {'coins+':>6s}")
        for key in keys:
            es = [r["engines"][key] for r in rs if key in r["engines"]]
            daily: dict[str, float] = {}
            for e in es:
                for d, x in e["daily"].items():
                    daily[d] = daily.get(d, 0.0) + x
            win = 100 * sum(x > 0 for x in daily.values()) / max(len(daily), 1)
            row = {
                "pnl": sum(e["pnl"] for e in es), "fees": sum(e.get("fees", 0.0) for e in es),
                "funding": sum(e.get("funding", 0.0) for e in es), "fills": sum(e.get("fills", 0) for e in es),
                "stops": sum(e.get("stops", 0) for e in es),
                "liq": sum(e.get("liquidations", 0) + e.get("guards", 0) for e in es),
                "mdd": mdd(daily), "win": win,
                "avg_inv": sum(e.get("inv_minutes", 0.0) for e in es) / len(es) / MIN_PER_BLOCK[block] * 1000
                if "inv_minutes" in es[0] else None,
                "max_inv": max(e.get("max_inv", 0.0) for e in es) if "max_inv" in es[0] else None,
                "coins_pos": sum(e["pnl"] > 0 for e in es),
                "per_coin": {r["symbol"]: round(r["engines"][key]["pnl"], 1) for r in rs if key in r["engines"]},
            }
            summary[f"{block}|{key}"] = row
            ai = f"{row['avg_inv']:7.0f}" if row["avg_inv"] is not None else "      -"
            mi = f"{row['max_inv']:7.0f}" if row["max_inv"] is not None else "      -"
            print(f"{key:30s} {row['pnl']:8.1f} {row['fees']:7.1f} {row['funding']:6.1f} {row['fills']:7d} "
                  f"{row['stops']:5d} {row['liq']:4d} {row['mdd']:7.1f} {win:5.1f} {ai} {mi} {row['coins_pos']:3d}/{len(es)}")
        if mode == "primary":
            print("  share of the CANDLE result that disappears at tick level (1 - tick/candle):")
            for v in VARIANT_NAMES:
                t = summary[f"{block}|tick|{v}"]["pnl"]
                for res_ in ("candle5m", "candle1m"):
                    c = summary[f"{block}|{res_}|{v}"]["pnl"]
                    share = (1 - t / c) * 100 if c > 0 else float("nan")
                    print(f"    {v:20s} vs {res_}: tick {t:8.1f}  candle {c:8.1f}  gone {share:6.1f} %")
    Path(f"docs/superpowers/reports/2026-10-02-grid-tick-{mode}.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
