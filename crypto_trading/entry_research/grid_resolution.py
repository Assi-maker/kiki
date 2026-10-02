"""Bar-resolution bias check (post hoc): the same grid on the same BingX days,
fed with 1m bars vs the same data aggregated to 5m bars. If 5m bars earn
clearly more, the 14-month (5m) results are inflated by the coarse fill model.

    python -m crypto_trading.entry_research.grid_resolution
"""

from __future__ import annotations

import sqlite3

from crypto_trading.entry_research import grid_lab as G

NAMES = ["LONG_R1_S0.1_noTF", "LONG_R1_S0.1_TF", "LONG_R1_S0.2_noTF", "LONG_R1_S0.2_TF", G.HEADLINE.name]


def to5m(bars):
    out = []
    for k in range(0, len(bars) - 4, 5):
        chunk = bars[k:k + 5]
        out.append((chunk[0][0], chunk[0][1], max(b[2] for b in chunk), min(b[3] for b in chunk), chunk[-1][4]))
    return out


def main() -> None:
    db = sqlite3.connect(f"file:{G.DB}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT symbol FROM done ORDER BY symbol")]
    tot = {(n, r): 0.0 for n in NAMES for r in ("1m", "5m")}
    for sym in syms:
        bars, fund = G.load(sym, db)
        b5 = to5m(bars)
        s1 = G.sigma_series([b[4] for b in bars], 1440)
        s5 = G.sigma_series([b[4] for b in b5], 288)
        start_t = bars[1441][0]
        i5 = next(i for i, b in enumerate(b5) if b[0] >= start_t)
        for v in G.VARIANTS:
            if v.name not in NAMES:
                continue
            tot[(v.name, "1m")] += G.run_grid(bars, fund, s1, 1441, len(bars), v, 1000.0)["pnl"]
            tot[(v.name, "5m")] += G.run_grid(b5, fund, s5, max(i5, 289), len(b5), v, 1000.0, None, 288)["pnl"]
    print(f"{'variant':22s} {'1m bars':>10s} {'5m bars':>10s}   (66 BingX coins, 25 days, 1000 USDT each)")
    for n in NAMES:
        print(f"{n:22s} {tot[(n, '1m')]:10.0f} {tot[(n, '5m')]:10.0f}")


if __name__ == "__main__":
    main()
