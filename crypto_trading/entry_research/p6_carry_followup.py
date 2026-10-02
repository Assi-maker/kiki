"""P6 C1 follow-up - POST HOC (written after seeing the pre-registered C1 fail).

The pre-registered C1 lost because 3x perp leverage gets the short liquidated
on +30 % alt pumps (33 events in the headline variant, ~ -4.5 % of capital
each), while funding (+74 % of capital over 6.75 y) minus costs was positive.
It also showed mismatched spot/perp pairs (|spot/perp - 1| > 5 % on hundreds
of days: SC, RAY, FTT, STRAX, ... = renamed / delisted tokens) -> fake basis.

Changes (and nothing else): perp leverage 1x (liquidation only on a +90 %
day; capital per notional = 2), and a data-quality rule: no entry when
|spot/perp - 1| > 2 % at the decision close, forced exit when > 5 %.
Same 9 variants, same costs. Because every period has now been seen, a pass
here is EXPLORATORY ONLY and would need a forward test before any use.

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.p6_carry_followup
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from crypto_trading.entry_research import p5_lab as P5
from crypto_trading.entry_research import p6_carry as C


def main() -> None:
    pc, ph, qv, sc, fund = C.load()
    gap = (sc / pc - 1).abs()
    ok_entry = gap <= 0.02
    C.LEV = 1.0
    C.CAP_PER_NOTIONAL = 2.0
    # mask mismatched coin-days: hide the spot price where the pair is broken, so the
    # simulator cannot enter (needs finite spot) and force-exits through the data-gap path
    sc_masked = sc.where(gap <= 0.05)
    rows, series = [], {}
    for K, t_in in C.VARIANTS:
        name = f"C1b_K{K}_T{int(t_in * 100)}"
        fund_masked = fund.where(ok_entry | (gap <= 0.05), 0.0)
        s, st = C.simulate_c1(pc, ph, qv, sc_masked, fund_masked, K, t_in)
        series[name] = s
        row = {"name": name, **{k: round(float(v), 4) for k, v in st.items()}}
        for per, (a, b) in P5.PERIODS.items():
            x = s[a:b]
            row[f"{per}_ann"] = P5.stats(x)["ann_ret"]
            row[f"{per}_win"] = float((x > 0).sum() / max((x != 0).sum(), 1))
        full = P5.stats(s["2020":"2026-09"])
        row.update(ALL_ann=full["ann_ret"], ALL_sharpe=full["sharpe"], ALL_mdd=full["mdd"])
        rows.append(row)
    df = pd.DataFrame(rows)
    years = pd.DataFrame({n: s["2020":"2026-09"].groupby(s["2020":"2026-09"].index.year).apply(lambda x: (1 + x).prod() - 1)
                          for n, s in series.items()})
    pd.set_option("display.width", 300)
    print(df.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(years.to_string(float_format=lambda v: f"{v:+.3f}"))
    with open("docs/superpowers/reports/2026-10-02-p6-carry-followup.json", "w") as f:
        json.dump({"rows": rows, "years": years.round(4).to_dict()}, f, default=float)


if __name__ == "__main__":
    main()
