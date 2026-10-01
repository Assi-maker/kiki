"""P4 post-hoc inspection (DESCRIPTIVE ONLY - after selection was frozen with 0 survivors).

For the cells that looked best anywhere (chosen by looking at all periods =
data snooping, so nothing here can approve anything):
  - market-adjusted return per trade = own return - MEAN return of the
    eligible universe over the same window (removes the HOLDOUT bull drift),
    with day-clustered one-sided p per period;
  - a daily equity curve (each day's trades share that day's capital equally,
    non-compounding) and its max drawdown, in % of capital;
  - F6 per sample day.

    python -m crypto_trading.entry_research.p4_inspect
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from crypto_trading.entry_research import p4_data as D
from crypto_trading.entry_research import p4_lab as P

OUT = Path("data/entry_research/p4_inspect.json")
TARGETS = [
    ("F2_IDIO_SHOCK", {"z": 4.0, "shock": "DOWN", "oi": "OI_DOWN"}, 1, ("2h", "4h", "24h")),
    ("F2_IDIO_SHOCK", {"z": 6.0, "shock": "DOWN", "oi": "ALL"}, 1, ("4h", "24h")),
    ("F2_IDIO_SHOCK", {"z": 3.0, "shock": "UP", "oi": "OI_DOWN"}, 1, ("30m", "1h", "2h")),
    ("F4_BREAKOUT", {"N_days": 14, "break": "UP", "conf": "HOLD", "regime": "ALL"}, 1, ("8h", "24h")),
    ("F6_REAL_LIQ", {"theta": 0.1, "liquidated": "LONGS"}, 1, ("1h", "4h", "24h")),
    ("F6_REAL_LIQ", {"theta": 0.03, "liquidated": "SHORTS"}, 1, ("4h", "24h")),
    ("F1_XS_REL", {"L": "7d", "q": "TOP"}, 1, ("24h",)),
]


def daily_equity(net: np.ndarray, e: np.ndarray) -> tuple[float, float]:
    day = e // 288
    sums = defaultdict(list)
    for d, v in zip(day, net):
        sums[d].append(v)
    eq = np.cumsum([np.mean(sums[d]) for d in sorted(sums)])
    dd = float((np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]).max())
    return float(eq[-1]) if len(eq) else 0.0, dd


def main() -> None:
    C = np.asarray(D.load("c"), dtype="f4")
    Hh = np.asarray(D.load("h"), dtype="f4")
    Ll = np.asarray(D.load("l"), dtype="f4")
    QV = np.asarray(D.load("qv"), dtype="f4")
    OIV = np.asarray(D.load("oiv"), dtype="f4")
    CF = D.load("cf")
    syms = D.symbols()
    first = np.argmax(np.isfinite(C), axis=1)
    age_ok = ((np.arange(D.N)[None, :] - first[:, None]) >= P.AGE_MIN) & np.isfinite(C)
    sets, cells, meta = [], [], {}
    P.build_f1(C, age_ok, sets, cells)
    P.build_f2(C, QV, OIV, age_ok, sets, cells)
    P.build_f4(C, Hh, Ll, QV, age_ok, syms.index("BTCUSDT"), sets, cells)
    P.build_f6(QV, syms, sets, cells, meta)
    del QV, OIV
    out = []
    for fam, params, side, horizons in TARGETS:
        ts = next(s for s in sets if s.family == fam and all(s.params.get(k) == v for k, v in params.items()))
        for h in horizons:
            hb = P.H_BARS[h]
            e = ts.k.astype(np.int64) + P.LAT
            x = e + hb
            ok = x < D.N
            s, e, x = ts.sym[ok], e[ok], x[ok]
            ret = C[s, x] / C[s, e].astype("f8") - 1
            # universe median over the same window, per distinct entry bar
            ue = np.unique(e)
            r_all = C[:, ue + hb] / C[:, ue].astype("f8") - 1
            r_all[~age_ok[:, ue]] = np.nan
            mk = dict(zip(ue, np.nanmean(r_all, axis=0)))
            mret = np.array([mk[v] for v in e])
            net = side * ret - P.COST - side * (CF[s, x] - CF[s, e])
            adj = side * (ret - mret)
            fin = np.isfinite(net) & np.isfinite(adj)
            per = P.period_of(e)
            row = {"family": fam, "params": params, "side": "LONG" if side > 0 else "SHORT", "h": h, "periods": {}}
            for pi, pn in enumerate(P.PERIOD_NAMES):
                m = fin & (per == pi)
                g = (e[m] // 12) if fam == "F6_REAL_LIQ" else (e[m] // 288)
                am, ap, G = P.cluster_p(adj[m], g)
                tot, dd = daily_equity(net[m], e[m])
                row["periods"][pn] = {"n": int(m.sum()), "net": float(net[m].mean()) if m.any() else 0.0, "mkt_adj_gross": am, "p_adj": ap, "clusters": G, "daily_equity_total": tot, "daily_equity_mdd": dd}
            if fam == "F6_REAL_LIQ":
                byday = defaultdict(list)
                for v, ee in zip(net[fin], e[fin]):
                    byday[datetime.fromtimestamp((D.T0 + int(ee) * D.STEP) / 1000, UTC).strftime("%Y-%m-%d")].append(v)
                row["by_day"] = {d: (len(v), float(np.mean(v))) for d, v in sorted(byday.items())}
            out.append(row)
            print(fam, params, h, " | ".join(f"{pn} n={v['n']} net={v['net']*100:+.2f} adj={v['mkt_adj_gross']*100:+.2f} p={v['p_adj']:.3f} mdd={v['daily_equity_mdd']*100:.0f}%" for pn, v in row["periods"].items()), flush=True)
            if "by_day" in row:
                print("   by day:", {d: f"{n}:{m*100:+.1f}" for d, (n, m) in row["by_day"].items()})
    OUT.write_text(json.dumps(out, default=float, indent=1))


if __name__ == "__main__":
    main()
