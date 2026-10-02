"""P5 robustness checks on the approved trend cells (run AFTER p5_lab).

Post-hoc by construction - nothing here can approve a cell, it can only
reject one. Checks per candidate:
  * cost x2 and x3;
  * one extra day of execution delay (weights decided at close d-1);
  * calendar-year returns;
  * the lookback ensemble (mean of the L20 and L50 weights) that the engine
    would trade, so the parameter choice is not a single picked cell.

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.p5_robust
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from crypto_trading.entry_research import p5_lab as L


def trend_weights(close: pd.DataFrame, ret: pd.DataFrame, assets: list[str], lookbacks: list[int], mode: str,
                  target: float = 0.40) -> pd.DataFrame:
    w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for a in assets:
        sig = sum(np.sign(close[a] / close[a].shift(lb) - 1) for lb in lookbacks) / len(lookbacks)
        if mode == "LF":
            sig = sig.clip(lower=0)
        w[a] = sig * L.vol_scale(ret[a], target) / len(assets)
    return w


def main() -> None:
    close, qv, fund = L.load()
    ret = close.pct_change(fill_method=None)
    liq10 = L.universe(close, qv, 10)
    cands = {
        "BTC_L20_LF": (["BTCUSDT"], [20], "LF"),
        "ETH_L50_LF": (["ETHUSDT"], [50], "LF"),
        "BTC+ETH_L20_LF": (["BTCUSDT", "ETHUSDT"], [20], "LF"),
        "BTC+ETH_L50_LS": (["BTCUSDT", "ETHUSDT"], [50], "LS"),
        "ENS_BTC+ETH_L20/50_LF": (["BTCUSDT", "ETHUSDT"], [20, 50], "LF"),
        "ENS_BTC+ETH_L20/50/100_LF": (["BTCUSDT", "ETHUSDT"], [20, 50, 100], "LF"),
    }
    rows, years = [], {}
    base_cost, base_illiq = L.COST, L.COST_ILLIQ
    for name, (assets, lbs, mode) in cands.items():
        w = trend_weights(close, ret, assets, lbs, mode)
        row = {"name": name}
        for mult in (1, 2, 3):
            L.COST, L.COST_ILLIQ = base_cost * mult, base_illiq * mult
            s = L.simulate(w, ret, fund, liq10)["2020-01-01":"2026-09-30"]
            st = L.stats(s)
            row[f"x{mult}_ann"], row[f"x{mult}_sharpe"], row[f"x{mult}_mdd"] = st["ann_ret"], st["sharpe"], st["mdd"]
            if mult == 1:
                years[name] = s.groupby(s.index.year).apply(lambda x: (1 + x).prod() - 1)
                for per, (a, b) in L.PERIODS.items():
                    row[f"{per}_ann"] = L.stats(s[a:b])["ann_ret"]
                row["turnover_yr"] = float(w.diff().abs().sum(axis=1)["2020":"2026-09"].sum() / 6.75)
        L.COST, L.COST_ILLIQ = base_cost, base_illiq
        s_lag = L.simulate(w.shift(1), ret, fund, liq10)["2020-01-01":"2026-09-30"]
        st = L.stats(s_lag)
        row["lag1_ann"], row["lag1_sharpe"] = st["ann_ret"], st["sharpe"]
        rows.append(row)
    pd.set_option("display.width", 300)
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    bh = {a: ret[a]["2020":"2026-09"].groupby(ret[a]["2020":"2026-09"].index.year).apply(lambda x: (1 + x).prod() - 1)
          for a in ("BTCUSDT", "ETHUSDT")}
    print(pd.DataFrame({**years, **{f"BH_{k}": v for k, v in bh.items()}}).to_string(float_format=lambda v: f"{v:+.2f}"))


if __name__ == "__main__":
    main()
