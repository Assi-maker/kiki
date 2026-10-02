"""Robustness of the 4 grids approved by grid_lab14 (post hoc - can only reject).

Scenarios (each one alone, then all together):
  base       grid_lab14 rules
  thru5      a limit fills only if price trades >= 5 bp THROUGH the level
  thru10     ... >= 10 bp (removes fills on thin wicks)
  maker4     0.04 % per grid fill (2x BingX maker)
  liqbar     liquidation on the bar's adverse extreme, whole margin + 0.5 % lost
  all        thru10 + maker4 + liqbar

    uv run --with numpy python -m crypto_trading.entry_research.grid_robust14 [workers]
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from crypto_trading.entry_research import grid_lab as G
from crypto_trading.entry_research import grid_lab14 as L

OUT = Path("docs/superpowers/reports/2026-10-02-grid-robust14.json")
NAMES = ["LONG_R1_S0.1_noTF", "LONG_R1_S0.1_TF", "LONG_R1_S0.2_noTF", "LONG_R1_S0.2_TF", G.HEADLINE.name]
SCEN = {
    "base": dict(THROUGH=0.0001, MAKER=0.0002, LIQ_INTRABAR=False),
    "thru5": dict(THROUGH=0.0005, MAKER=0.0002, LIQ_INTRABAR=False),
    "thru10": dict(THROUGH=0.0010, MAKER=0.0002, LIQ_INTRABAR=False),
    "maker4": dict(THROUGH=0.0001, MAKER=0.0004, LIQ_INTRABAR=False),
    "liqbar": dict(THROUGH=0.0001, MAKER=0.0002, LIQ_INTRABAR=True),
    "all": dict(THROUGH=0.0010, MAKER=0.0004, LIQ_INTRABAR=True),
}
VARS = [v for v in G.VARIANTS if v.name in NAMES]


def worker(k: int) -> dict | None:
    s = L.series(k)
    if s is None:
        return None
    bars, fund = s
    sig = G.sigma_series([b[4] for b in bars], L.BPD)
    out = {}
    for sc, params in SCEN.items():
        for name, val in params.items():
            setattr(G, name, val)
        for v in VARS:
            daily = {}
            G.run_grid(bars, fund, sig, L.BPD + 1, len(bars), v, 1000.0, daily, L.BPD)
            out[f"{sc}|{v.name}"] = daily
    return out


def main() -> None:
    syms = json.loads((L.CACHE / "symbols.json").read_text())
    with ProcessPoolExecutor(max_workers=int(sys.argv[1]) if len(sys.argv) > 1 else 3) as ex:
        res = [r for r in ex.map(worker, range(len(syms))) if r is not None]
    n = len(res)
    table = {}
    for key in res[0]:
        agg = {}
        for r in res:
            for d, x in r[key].items():
                agg[int(d)] = agg.get(int(d), 0.0) + x
        port = {d: x / n * 4 for d, x in agg.items()}
        per = {}
        for pname in L.PERIODS:
            xs = [x for d, x in sorted(port.items()) if L.period_of(d) == pname]
            per[pname] = (round(sum(xs), 1), round(100 * sum(x > 0 for x in xs) / max(len(xs), 1), 1))
        table[key] = per
    OUT.write_text(json.dumps(table))
    for key, per in table.items():
        print(f"{key:28s} " + "  ".join(f"{p}:{t:8.0f} {w:4.0f}%" for p, (t, w) in per.items()))


if __name__ == "__main__":
    main()
