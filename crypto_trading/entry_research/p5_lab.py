"""P5 slow-timescale strategy lab (pre-registered before any result was seen).

Why: P1-P4 tested intraday entries (5m-24h holds) on ~14 months: 0 edge, the
round-trip cost (~0.16 %) eats every intraday move. P5 tests the strategy
classes with a documented crypto premium at DAYS-WEEKS horizon, where cost is
small relative to the move, on 7 years of daily data (2019-09 .. 2026-10).

Data: p5_data.py (Binance USDT-M perps incl. delisted; daily close; full funding).

Execution model (no look-ahead):
  * weights for day d+1 are computed from data known at the close of day d
    and earn the close(d) -> close(d+1) return;
  * a LONG pays the funding settled during day d+1, a SHORT receives it;
  * cost = 0.08 % per unit of turnover (0.05 % taker + 0.03 % slippage),
    and 0.15 % for names outside the 10 most liquid of the day.

Families (fixed grid, nothing added after results):
  F1 TS trend BTC/ETH:   asset in {BTC, ETH, BTC+ETH}; L in {20,50,100,200};
                         mode in {long/flat, long/short}; vol target 40 %/yr
                         (scale = 0.4 / realised 30d vol, capped at 2)      24
  F2 TS trend broad:     top-30 liquid universe, each name long (/short) on
                         its own L-day sign, equal risk, L in {20,50,100}   6
  F3 XS momentum:        top-30, long top-6 / short bottom-6 by L-day return,
                         L in {7,14,30,60}, rebalance every {1,7} days,
                         sign in {momentum, reversal}                       16
  F4 XS funding carry:   top-30, short the 6 highest / long the 6 lowest
                         trailing-K-day mean funding, K in {3,7,14}, weekly 3
  F5 cash-and-carry:     long spot + short perp on {BTC, ETH}; in position
                         while trailing 7d funding APR > thr, thr in
                         {0, 5 %, 10 %}; earns funding, pays 2 legs cost    6
                                                                    total 55
Periods: TRAIN 2020-01..2022-12, VALID 2023-01..2024-06,
         TEST 2024-07..2025-09, HOLDOUT 2025-10..2026-09.

Approval rule (all must hold):
  1. selection on TRAIN+VALID: one-sided p (day-block bootstrap, 20-day blocks)
     of mean net daily return > 0, BH over all 55 at q < 0.10;
  2. net mean > 0 in TRAIN, VALID, TEST and HOLDOUT separately;
  3. max drawdown over the whole sample < 50 % at the tested sizing.
F1/F2 long-only variants are additionally compared with buy-and-hold.

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.p5_lab
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("data/entry_research/p5/daily.db")
OUT = Path("docs/superpowers/reports")
DAY = 86_400_000
COST = 0.0008
COST_ILLIQ = 0.0015
PERIODS = {
    "TRAIN": ("2020-01-01", "2022-12-31"),
    "VALID": ("2023-01-01", "2024-06-30"),
    "TEST": ("2024-07-01", "2025-09-30"),
    "HOLDOUT": ("2025-10-01", "2026-09-30"),
}
TOPN = 30
K_XS = 6


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    k = pd.read_sql("SELECT symbol, t, c, qv FROM klines", db)
    f = pd.read_sql("SELECT symbol, t, rate FROM funding", db)
    k["d"] = pd.to_datetime(k.t, unit="ms")
    close = k.pivot(index="d", columns="symbol", values="c").sort_index()
    qv = k.pivot(index="d", columns="symbol", values="qv").reindex(close.index)
    # funding settled during day d (d 00:00 < t <= d+1 00:00) is paid by a position held over day d
    f["d"] = pd.to_datetime((f.t - 1) // DAY * DAY, unit="ms")
    fund = f.groupby(["d", "symbol"]).rate.sum().unstack().reindex(close.index).fillna(0.0)
    return close, qv, fund


def universe(close: pd.DataFrame, qv: pd.DataFrame, n: int = TOPN) -> pd.DataFrame:
    """Point-in-time top-n by trailing 30d quote volume, >= 60 days listed."""
    age = close.notna().cumsum()
    liq = qv.rolling(30, min_periods=20).mean().where(age >= 60)
    rank = liq.rank(axis=1, ascending=False)
    return rank <= n


def simulate(w: pd.DataFrame, ret: pd.DataFrame, fund: pd.DataFrame, liq10: pd.DataFrame) -> pd.Series:
    """w = target weights decided at close d (index d) -> held over day d+1."""
    w = w.fillna(0.0)
    held = w.shift(1).fillna(0.0)
    pnl = (held * ret.fillna(0.0)).sum(axis=1) - (held * fund).sum(axis=1)
    turn = (w - w.shift(1).fillna(0.0)).abs()
    cost_rate = pd.DataFrame(COST_ILLIQ, index=w.index, columns=w.columns).where(~liq10.reindex_like(w).fillna(False), COST)
    cost = (turn * cost_rate).sum(axis=1).shift(1).fillna(0.0)
    return pnl - cost


def vol_scale(ret: pd.Series, target: float = 0.40) -> pd.Series:
    vol = ret.rolling(30, min_periods=20).std() * np.sqrt(365)
    return (target / vol).clip(upper=2.0)


def strategies(close, qv, fund):
    ret = close.pct_change(fill_method=None)
    uni = universe(close, qv)
    liq10 = universe(close, qv, 10)
    out = {}
    # F1
    for name, assets in {"BTC": ["BTCUSDT"], "ETH": ["ETHUSDT"], "BTC+ETH": ["BTCUSDT", "ETHUSDT"]}.items():
        for L in (20, 50, 100, 200):
            for mode in ("LF", "LS"):
                w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
                for a in assets:
                    sig = np.sign(close[a] / close[a].shift(L) - 1)
                    if mode == "LF":
                        sig = sig.clip(lower=0)
                    w[a] = sig * vol_scale(ret[a]) / len(assets)
                out[f"F1_{name}_L{L}_{mode}"] = simulate(w, ret, fund, liq10)
    # F2
    for L in (20, 50, 100):
        for mode in ("LF", "LS"):
            sig = np.sign(close / close.shift(L) - 1).where(uni)
            if mode == "LF":
                sig = sig.clip(lower=0)
            vol = ret.rolling(30, min_periods=20).std() * np.sqrt(365)
            raw = (sig * (0.40 / vol).clip(upper=2.0)).fillna(0.0)
            n = uni.sum(axis=1).replace(0, np.nan)
            out[f"F2_L{L}_{mode}"] = simulate(raw.div(n, axis=0), ret, fund, liq10)
    # F3
    for L in (7, 14, 30, 60):
        mom = (close / close.shift(L) - 1).where(uni)
        r = mom.rank(axis=1, ascending=False)
        cnt = mom.notna().sum(axis=1)
        base = (r <= K_XS).astype(float) - r.gt(cnt - K_XS, axis=0).astype(float)
        base = base.mul((cnt >= 2 * K_XS).astype(float), axis=0) / (2 * K_XS)
        for H in (1, 7):
            w = base if H == 1 else base.where(pd.Series(np.arange(len(base)) % H == 0, index=base.index), np.nan).ffill()
            for sign, lab in ((1, "MOM"), (-1, "REV")):
                out[f"F3_L{L}_H{H}_{lab}"] = simulate(sign * w, ret, fund, liq10)
    # F4
    for K in (3, 7, 14):
        fm = fund.rolling(K, min_periods=K).mean().where(uni)
        r = fm.rank(axis=1, ascending=False)
        cnt = fm.notna().sum(axis=1)
        base = -(r <= K_XS).astype(float) + r.gt(cnt - K_XS, axis=0).astype(float)
        base = base.mul((cnt >= 2 * K_XS).astype(float), axis=0) / (2 * K_XS)
        w = base.where(pd.Series(np.arange(len(base)) % 7 == 0, index=base.index), np.nan).ffill()
        out[f"F4_K{K}"] = simulate(w, ret, fund, liq10)
    # F5: long spot (assumed = perp price) + short perp -> price cancels, earns funding, 2 legs of cost
    for a in ("BTCUSDT", "ETHUSDT"):
        apr = fund[a].rolling(7, min_periods=7).mean() * 365
        for thr in (0.0, 0.05, 0.10):
            on = (apr > thr).astype(float)
            held = on.shift(1).fillna(0.0)
            pnl = held * fund[a] - (on.diff().abs().fillna(0.0) * 2 * COST).shift(1).fillna(0.0)
            out[f"F5_{a[:3]}_thr{int(thr * 100)}"] = pnl / 2  # capital: 1 spot + 1 perp margin
    bench = {"BH_BTC": ret["BTCUSDT"].fillna(0.0), "BH_ETH": ret["ETHUSDT"].fillna(0.0)}
    return out, bench


def block_p(x: np.ndarray, block: int = 20, n_boot: int = 4000, seed: int = 7) -> float:
    """One-sided p that the mean is <= 0, circular block bootstrap of the centred series."""
    x = x[~np.isnan(x)]
    if len(x) < 2 * block:
        return 1.0
    rng = np.random.default_rng(seed)
    xc = x - x.mean()
    nb = int(np.ceil(len(x) / block))
    starts = rng.integers(0, len(x), size=(n_boot, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_boot, -1)[:, : len(x)] % len(x)
    means = xc[idx].mean(axis=1)
    return float((means >= x.mean()).mean())


def stats(s: pd.Series) -> dict:
    s = s.dropna()
    if len(s) == 0 or s.std() == 0:
        return {"n": len(s), "ann_ret": 0.0, "sharpe": 0.0, "mdd": 0.0, "hit": 0.0}
    eq = (1 + s).cumprod()
    return {
        "n": len(s),
        "ann_ret": float((eq.iloc[-1]) ** (365 / len(s)) - 1),
        "sharpe": float(s.mean() / s.std() * np.sqrt(365)),
        "mdd": float((eq / eq.cummax() - 1).min()),
        "hit": float((s > 0).sum() / max((s != 0).sum(), 1)),
    }


def bh(p: dict[str, float], q: float = 0.10) -> set[str]:
    items = sorted(p.items(), key=lambda kv: kv[1])
    m = len(items)
    k = 0
    for i, (_, pv) in enumerate(items, start=1):
        if pv <= q * i / m:
            k = i
    return {name for name, _ in items[:k]}


def main() -> None:
    close, qv, fund = load()
    out, bench = strategies(close, qv, fund)
    rows = []
    psel = {}
    for name, s in {**out, **bench}.items():
        row = {"name": name}
        sel = s[PERIODS["TRAIN"][0] : PERIODS["VALID"][1]]
        if name in out:
            psel[name] = block_p(sel.to_numpy())
            row["p_sel"] = psel[name]
        for per, (a, b) in PERIODS.items():
            st = stats(s[a:b])
            row.update({f"{per}_{k}": v for k, v in st.items() if k != "n"})
        full = stats(s[PERIODS["TRAIN"][0] : PERIODS["HOLDOUT"][1]])
        row.update({f"ALL_{k}": v for k, v in full.items()})
        rows.append(row)
    passed_bh = bh(psel)
    df = pd.DataFrame(rows)
    df["bh"] = df.name.isin(passed_bh)
    df["all_periods_pos"] = np.all([df[f"{p}_ann_ret"] > 0 for p in PERIODS], axis=0)
    df["approved"] = df.bh & df.all_periods_pos & (df.ALL_mdd > -0.5)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "2026-10-02-p5-slow-lab.csv", index=False)
    (OUT / "2026-10-02-p5-daily-returns.json").write_text(
        json.dumps({k: {str(d.date()): round(float(v), 6) for d, v in s.items() if v != 0} for k, s in out.items() if k in passed_bh})
    )
    pd.set_option("display.width", 250)
    cols = ["name", "p_sel", "bh", "TRAIN_sharpe", "VALID_sharpe", "TEST_sharpe", "HOLDOUT_sharpe", "ALL_ann_ret", "ALL_sharpe", "ALL_mdd", "approved"]
    print(df.sort_values("p_sel")[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print("BH survivors:", sorted(passed_bh))
    print("APPROVED:", df[df.approved].name.tolist())


if __name__ == "__main__":
    main()
