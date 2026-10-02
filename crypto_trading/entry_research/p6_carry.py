"""P6 funding carry lab - harvest funding instead of predicting price (pre-registered).

Why a different track: every earlier lab (P1-P5, grid) tried to PREDICT the
next price move and found no edge after costs. Funding is a cash payment the
market makes every 8 h (sometimes 4 h / 1 h) from one side to the other. A
delta-neutral book (long spot + short perp of the same coin) has ~zero price
exposure and collects positive funding every day. The literature (basis /
cash-and-carry) and P5 F5 (BTC/ETH only) show it is positive but small on
majors; ALT funding is far higher and more volatile - untested so far.

Data: P5 daily DB (Binance USDT-M perps 2019-09..2026-10, 261 symbols that
were listed before 2024-04, incl. delisted; FULL funding history) + P6 spot
daily candles (249 have a Binance spot pair). Known gap: perps listed after
2024-04 are missing (the P5 download was stopped by the memory reaper).

STRATEGY C1 (cash-and-carry, positive funding):
  * decide at the daily close d using only data known then; hold over day d+1
  * universe on day d: spot AND perp data, perp listed >= 30 days, top 50 by
    trailing 30 d perp quote volume
  * signal: APR_K = mean daily funding over the last K days x 365
  * a coin ENTERS when APR_K > T_in, EXITS when APR_K < T_in / 2 (hysteresis)
  * at most N = 5 positions, each 1/5 of capital; free slots go to the highest
    APR_K entrants; held positions keep their slot while above the exit level
  * per position: notional X long spot + X short perp with perp margin X / 3,
    so capital per unit notional = 1 + 1/3
  * daily P/L per unit notional = spot return - perp return + funding settled
    during day d+1 (a short RECEIVES positive funding)
  * costs per entry and per exit: spot 0.10 % + perp 0.05 % taker, + 0.05 %
    slippage on each leg = 0.25 % of notional per side (0.50 % round trip)
  * perp margin is topped up at each daily close (transfer); intraday: if the
    perp HIGH exceeds the previous close by 0.9 x 1/3 the short is liquidated:
    the short is closed at the day's HIGH, spot sold at the close, plus 1 % of
    notional penalty + exit costs.
Variants: K in {1, 3, 7} x T_in in {10 %, 20 %, 40 %} = 9. Headline: K 3, T_in 20 %.

STRATEGY C2 (funding snipe, perp only, P4 5m archive 2025-08..2026-09):
  enter a perp 5 min before a settlement on the side that RECEIVES funding if
  the PREVIOUS settled rate |r| > theta, exit 5 min after; cost 0.05 % taker +
  0.05 % slippage per side. theta in {0.10 %, 0.20 %, 0.50 %} = 3 variants.

Periods (C1): TRAIN 2020..2022, VALID 2023..2024-06, TEST 2024-07..2025-09,
HOLDOUT 2025-10..2026-09.
APPROVAL (C1): net daily mean > 0 in all four periods; BH q < 0.10 over the 9
on TRAIN+VALID (20-day block bootstrap); share of positive days > 60 % in
each period; max drawdown on capital > -20 %.
APPROVAL (C2): mean net per trade > 0 in both halves (2025-08..2026-02,
2026-03..2026-09) with >= 100 trades each.

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.p6_carry
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_trading.entry_research import p5_lab as P5

DB = Path("data/entry_research/p5/daily.db")
OUT = Path("docs/superpowers/reports/2026-10-02-p6-carry.json")
LEG_COST = 0.0010 + 0.0005 + 0.0005 + 0.0005  # spot fee + perp fee + 2 x slippage, per side
LEV = 3.0
CAP_PER_NOTIONAL = 1 + 1 / LEV
N_SLOTS = 5
TOP_LIQ = 50
VARIANTS = [(k, t) for k in (1, 3, 7) for t in (0.10, 0.20, 0.40)]
HEADLINE = (3, 0.20)


def load():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    k = pd.read_sql("SELECT symbol, t, h, c, qv FROM klines", db)
    s = pd.read_sql("SELECT symbol, t, c FROM spot", db)
    f = pd.read_sql("SELECT symbol, t, rate FROM funding", db)
    k["d"] = pd.to_datetime(k.t, unit="ms")
    s["d"] = pd.to_datetime(s.t, unit="ms")
    pc = k.pivot(index="d", columns="symbol", values="c").sort_index()
    ph = k.pivot(index="d", columns="symbol", values="h").reindex(pc.index)
    qv = k.pivot(index="d", columns="symbol", values="qv").reindex(pc.index)
    sc = s.pivot(index="d", columns="symbol", values="c").reindex(pc.index)
    f["d"] = pd.to_datetime((f.t - 1) // P5.DAY * P5.DAY, unit="ms")
    fund = f.groupby(["d", "symbol"]).rate.sum().unstack().reindex(pc.index).fillna(0.0)
    cols = sorted(set(pc.columns) & set(sc.columns))
    return pc[cols], ph[cols], qv[cols], sc[cols], fund.reindex(columns=cols).fillna(0.0)


def simulate_c1(pc, ph, qv, sc, fund, K: int, t_in: float) -> tuple[pd.Series, dict]:
    age = pc.notna().cumsum()
    liq = qv.rolling(30, min_periods=20).mean().where((age >= 30) & sc.notna())
    rank = liq.rank(axis=1, ascending=False)
    eligible = (rank <= TOP_LIQ).to_numpy()
    apr = (fund.rolling(K, min_periods=K).mean() * 365).to_numpy()
    rs = (sc / sc.shift(1) - 1).to_numpy()
    rp = (pc / pc.shift(1) - 1).to_numpy()
    hi = (ph / pc.shift(1) - 1).to_numpy()
    fr = fund.to_numpy()
    T, S = pc.shape
    held: set[int] = set()
    daily = np.zeros(T)
    stats = {"entries": 0, "exits": 0, "liquidations": 0, "pos_days": 0, "funding": 0.0, "basis": 0.0, "costs": 0.0}
    slot = 1.0 / N_SLOTS / CAP_PER_NOTIONAL  # notional per slot as a fraction of capital
    for d in range(T - 1):
        # P/L of positions held over day d+1 (decided at close d)
        pnl = 0.0
        for j in list(held):
            if not (np.isfinite(rs[d + 1, j]) and np.isfinite(rp[d + 1, j])):
                pnl -= LEG_COST * slot  # data gap: forced exit at cost
                held.discard(j)
                stats["exits"] += 1
                continue
            if np.isfinite(hi[d + 1, j]) and hi[d + 1, j] >= 0.9 / LEV:
                # perp short closed at the liquidation level (the day's high), spot sold at the close
                pnl += slot * (rs[d + 1, j] - hi[d + 1, j] - 0.01 - LEG_COST)
                stats["liquidations"] += 1
                stats["costs"] += slot * (0.01 + LEG_COST)
                held.discard(j)
                continue
            leg = rs[d + 1, j] - rp[d + 1, j] + fr[d + 1, j]
            pnl += slot * leg
            stats["funding"] += slot * fr[d + 1, j]
            stats["basis"] += slot * (rs[d + 1, j] - rp[d + 1, j])
            stats["pos_days"] += 1
        # decisions at close d+1 for day d+2
        a = apr[d + 1]
        for j in list(held):
            if not (np.isfinite(a[j]) and a[j] >= t_in / 2):
                held.discard(j)
                pnl -= LEG_COST * slot
                stats["costs"] += LEG_COST * slot
                stats["exits"] += 1
        free = N_SLOTS - len(held)
        if free > 0:
            cand = [(a[j], j) for j in range(S) if eligible[d + 1, j] and j not in held
                    and np.isfinite(a[j]) and a[j] > t_in and np.isfinite(sc.iat[d + 1, j])]
            for _, j in sorted(cand, reverse=True)[:free]:
                held.add(j)
                pnl -= LEG_COST * slot
                stats["costs"] += LEG_COST * slot
                stats["entries"] += 1
        daily[d + 1] = pnl
    return pd.Series(daily, index=pc.index), stats


def c2_snipe() -> dict:
    """Funding snipe on the P4 5m archive (perp only)."""
    from crypto_trading.entry_research import grid_lab14 as L14

    syms = json.loads((L14.CACHE / "symbols.json").read_text())
    c = np.load(L14.CACHE / "c.npy", mmap_mode="r")
    cf = np.load(L14.CACHE / "cf.npy", mmap_mode="r")
    cost = 2 * (0.0005 + 0.0005)
    split = int((pd.Timestamp("2026-03-01").value // 1_000_000 - L14.T0) // L14.STEP)
    out = {}
    for theta in (0.001, 0.002, 0.005):
        trades = {"H1": [], "H2": []}
        for i in range(len(syms)):
            ci = np.asarray(c[i], dtype="f8")
            cfi = np.nan_to_num(np.asarray(cf[i], dtype="f8"))
            jumps = np.flatnonzero(np.diff(cfi) != 0) + 1  # bar k whose END settles a rate cf[k]-cf[k-1]
            prev = None
            for k in jumps:
                rate = cfi[k] - cfi[k - 1]
                if prev is not None and abs(prev) > theta and k - 1 >= 0 and k + 1 < len(ci):
                    e, x = ci[k - 1], ci[k + 1]  # close 5 min before the settlement bar ends .. 5 min after
                    if np.isfinite(e) and np.isfinite(x):
                        side = -1.0 if prev > 0 else 1.0  # short receives positive funding
                        pnl = side * (x / e - 1) + abs(rate) * (1 if np.sign(rate) == np.sign(prev) else -1) - cost
                        trades["H1" if k < split else "H2"].append(pnl)
                prev = rate
        out[f"theta{theta}"] = {h: {"n": len(v), "mean": float(np.mean(v)) if v else 0.0,
                                    "win": float(np.mean(np.array(v) > 0)) if v else 0.0} for h, v in trades.items()}
        out[f"theta{theta}"]["approved"] = all(out[f"theta{theta}"][h]["n"] >= 100 and out[f"theta{theta}"][h]["mean"] > 0
                                               for h in ("H1", "H2"))
    return out


def main() -> None:
    pc, ph, qv, sc, fund = load()
    rows, psel, series = [], {}, {}
    for K, t_in in VARIANTS:
        name = f"C1_K{K}_T{int(t_in * 100)}"
        s, st = simulate_c1(pc, ph, qv, sc, fund, K, t_in)
        series[name] = s
        psel[name] = P5.block_p(s[P5.PERIODS["TRAIN"][0]:P5.PERIODS["VALID"][1]].to_numpy())
        row = {"name": name, "p_sel": psel[name], **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in st.items()}}
        for per, (a, b) in P5.PERIODS.items():
            x = s[a:b]
            stt = P5.stats(x)
            row[f"{per}_ann"] = stt["ann_ret"]
            row[f"{per}_win"] = float((x > 0).sum() / max((x != 0).sum(), 1))
            row[f"{per}_active"] = float((x != 0).mean())
            row[f"{per}_mdd"] = stt["mdd"]
        full = P5.stats(s["2020":"2026-09"])
        row.update(ALL_ann=full["ann_ret"], ALL_sharpe=full["sharpe"], ALL_mdd=full["mdd"])
        rows.append(row)
    passed = P5.bh(psel)
    df = pd.DataFrame(rows)
    df["bh"] = df.name.isin(passed)
    df["approved"] = (df.bh & np.all([df[f"{p}_ann"] > 0 for p in P5.PERIODS], axis=0)
                      & np.all([df[f"{p}_win"] > 0.60 for p in P5.PERIODS], axis=0) & (df.ALL_mdd > -0.20))
    years = pd.DataFrame({n: s["2020":"2026-09"].groupby(s["2020":"2026-09"].index.year).apply(lambda x: (1 + x).prod() - 1)
                          for n, s in series.items()})
    c2 = c2_snipe()
    OUT.write_text(json.dumps({"c1": rows, "c1_years": years.round(4).to_dict(), "c2": c2,
                               "approved": df[df.approved].name.tolist()}, default=float))
    pd.set_option("display.width", 300)
    cols = ["name", "p_sel", "bh"] + [f"{p}_{m}" for p in P5.PERIODS for m in ("ann", "win")] + ["ALL_ann", "ALL_mdd", "liquidations", "entries"]
    print(df[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(years.to_string(float_format=lambda v: f"{v:+.3f}"))
    print(json.dumps(c2, indent=1))
    print("C1 APPROVED:", df[df.approved].name.tolist())


if __name__ == "__main__":
    main()
