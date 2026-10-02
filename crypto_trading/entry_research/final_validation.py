"""Final pre-registered validation: Trend vs Carry vs Trend+Carry vs baselines.

The method, definitions, hypotheses and acceptance criteria are fixed in
docs/superpowers/reports/2026-10-02-final-trend-carry-validation.md
(section "FÖRREGISTRERING"), committed together with this file BEFORE the
first run. Nothing here is tuned after results.

Daily engine on Binance USDT-M perps + Binance spot (P5/P6 daily DB),
2019-09 .. 2026-10-01 UTC. Weights / positions are decided at the daily close
d from data known then and earn day d+1 (close d -> close d+1).

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.final_validation
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("data/entry_research/p5/daily.db")
OUT_JSON = Path("docs/superpowers/reports/2026-10-02-final-trend-carry-validation.json")
DAY_MS = 86_400_000

PERIODS = {
    "TRAIN": ("2020-01-01", "2022-12-31"),
    "VALID": ("2023-01-01", "2024-06-30"),
    "TEST": ("2024-07-01", "2025-09-30"),
    "HOLDOUT": ("2025-10-01", "2026-09-30"),
}
FULL = ("2020-01-01", "2026-09-30")
MAJORS = ("BTCUSDT", "ETHUSDT")
STABLE_LIKE = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "PAXGUSDT", "XAUTUSDT"}


# ------------------------------------------------------------------ costs / scenario

@dataclass(frozen=True)
class Scenario:
    name: str = "base"
    perp_fee: float = 0.0005      # BingX perp taker
    spot_fee: float = 0.0010      # BingX spot taker
    half_spread_major: float = 0.0001
    half_spread_alt: float = 0.0003
    slip_major: float = 0.0002
    slip_alt: float = 0.0005
    extra_fill: float = 0.0       # worse fills: added per leg per side
    funding_mult: float = 1.0     # multiplies every funding rate (both signs)
    funding_shift_day: float = 0.0  # added to the DAILY funding sum (negative = shorts receive less)
    basis_exit_major: float = 0.0   # adverse basis at every carry exit / rebalance (fraction of notional)
    basis_exit_alt: float = 0.0
    carry_perp_lev: float = 2.0     # perp margin = notional / lev
    trend_margin_lev: float = 3.0


SCENARIOS = [
    Scenario(),
    Scenario("fees x2", perp_fee=0.0010, spot_fee=0.0020),
    Scenario("slippage x2", slip_major=0.0004, slip_alt=0.0010),
    Scenario("slippage x3 + spread x2", slip_major=0.0006, slip_alt=0.0015, half_spread_major=0.0002, half_spread_alt=0.0006),
    Scenario("worse fills +0.05%/leg", extra_fill=0.0005),
    Scenario("funding x0.5", funding_mult=0.5),
    Scenario("funding ~0", funding_mult=0.0),
    Scenario("funding negative (-0.01%/8h shift)", funding_shift_day=-0.0003),
    Scenario("stressed basis (0.2%/0.5% per exit)", basis_exit_major=0.002, basis_exit_alt=0.005),
    Scenario("worse capital use (perp 1x)", carry_perp_lev=1.0),
    Scenario("all costs x2 + funding x0.5 + basis", perp_fee=0.0010, spot_fee=0.0020, slip_major=0.0004,
             slip_alt=0.0010, funding_mult=0.5, basis_exit_major=0.002, basis_exit_alt=0.005),
]


def leg_cost(sc: Scenario, major: bool, spot: bool) -> float:
    fee = sc.spot_fee if spot else sc.perp_fee
    hs = sc.half_spread_major if major else sc.half_spread_alt
    sl = sc.slip_major if major else sc.slip_alt
    return fee + hs + sl + sc.extra_fill


# ------------------------------------------------------------------ data

@dataclass
class Data:
    pc: pd.DataFrame   # perp close
    ph: pd.DataFrame   # perp high
    qv: pd.DataFrame   # perp quote volume
    sc: pd.DataFrame   # spot close
    fund: pd.DataFrame  # funding settled during day d (sum of rates)


def load() -> Data:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    k = pd.read_sql("SELECT symbol, t, h, c, qv FROM klines", db)
    s = pd.read_sql("SELECT symbol, t, c FROM spot", db)
    f = pd.read_sql("SELECT symbol, t, rate FROM funding", db)
    k["d"] = pd.to_datetime(k.t, unit="ms")
    s["d"] = pd.to_datetime(s.t, unit="ms")
    pc = k.pivot(index="d", columns="symbol", values="c").sort_index()
    pc = pc.loc[:"2026-10-01"]
    ph = k.pivot(index="d", columns="symbol", values="h").reindex(pc.index)
    qv = k.pivot(index="d", columns="symbol", values="qv").reindex(pc.index)
    sc = s.pivot(index="d", columns="symbol", values="c").reindex(index=pc.index, columns=pc.columns)
    f["d"] = pd.to_datetime((f.t - 1) // DAY_MS * DAY_MS, unit="ms")
    fund = f.groupby(["d", "symbol"]).rate.sum().unstack().reindex(index=pc.index, columns=pc.columns).fillna(0.0)
    return Data(pc, ph, qv, sc, fund)


def scen_funding(d: Data, sc: Scenario) -> pd.DataFrame:
    return d.fund * sc.funding_mult + sc.funding_shift_day


# ------------------------------------------------------------------ result container

@dataclass
class Result:
    name: str
    daily: pd.Series          # net daily P/L as a fraction of capital
    gross: pd.Series          # before costs
    costs: pd.Series          # costs (positive number = paid)
    util: pd.Series           # capital utilisation (fraction of capital encumbered)
    exposure: pd.Series       # gross market exposure (fraction of capital)
    trades: int               # rebalance / order events
    extra: dict


# ------------------------------------------------------------------ TREND (frozen production rule)

def trend_weights(pc: pd.DataFrame, assets, lookbacks=(20, 50), vt=0.40, vw=30, max_scale=2.0) -> pd.DataFrame:
    ret = pc.pct_change(fill_method=None)
    w = pd.DataFrame(0.0, index=pc.index, columns=list(assets))
    for a in assets:
        sig = sum(np.sign(pc[a] / pc[a].shift(lb) - 1) for lb in lookbacks) / len(lookbacks)
        sig = sig.clip(lower=0)
        vol = ret[a].rolling(vw, min_periods=20).std() * np.sqrt(365)
        w[a] = (sig * (vt / vol).clip(upper=max_scale) / len(assets)).fillna(0.0)
    return w


def apply_band(w: pd.DataFrame, band: float) -> pd.DataFrame:
    """Production rule: skip a change smaller than `band` x equity, but always execute a move to zero."""
    out = np.zeros_like(w.to_numpy())
    cur = np.zeros(w.shape[1])
    for i, row in enumerate(w.to_numpy()):
        for j, x in enumerate(row):
            if (x == 0 and cur[j] != 0) or abs(x - cur[j]) >= band:
                cur[j] = x
        out[i] = cur
    return pd.DataFrame(out, index=w.index, columns=w.columns)


def run_trend(d: Data, sc: Scenario, assets=MAJORS, band: float = 0.05, name: str = "TREND") -> Result:
    w = apply_band(trend_weights(d.pc, assets), band)
    ret = d.pc[list(assets)].pct_change(fill_method=None).fillna(0.0)
    fund = scen_funding(d, sc)[list(assets)]
    held = w.shift(1).fillna(0.0)
    gross = (held * ret).sum(axis=1) - (held * fund).sum(axis=1)
    turn = (w - w.shift(1).fillna(0.0)).abs()
    major = [a in MAJORS for a in assets]
    rate = pd.Series([leg_cost(sc, m, False) for m in major], index=list(assets))
    costs = (turn * rate).sum(axis=1).shift(1).fillna(0.0)
    exposure = held.sum(axis=1)
    util = exposure / sc.trend_margin_lev
    trades = int((turn.sum(axis=1) > 0).sum())
    return Result(name, gross - costs, gross, costs, util, exposure, trades, {"weights": w})


# ------------------------------------------------------------------ CARRY

@dataclass(frozen=True)
class CarrySpec:
    name: str
    universe: str          # "MAJORS" | "TOP5" | "TOP10"
    entry_apr: float       # enter when trailing-7d mean funding x 365 > entry_apr
    exit_apr: float        # exit when it falls below exit_apr


CARRY_SPECS = [
    CarrySpec(f"CARRY_{u}_T{int(t * 100)}", u, t, (t / 2 if t > 0 else 0.0))
    for u in ("MAJORS", "TOP5", "TOP10") for t in (0.0, 0.05, 0.10)
]
CARRY_PRIMARY = "CARRY_MAJORS_T0"


def carry_universe(d: Data, kind: str) -> pd.DataFrame:
    """Boolean (day x symbol) eligibility, point in time."""
    cols = d.pc.columns
    paired = (d.sc / d.pc - 1).abs() <= 0.02  # data quality: spot and perp must be the same asset
    if kind == "MAJORS":
        return pd.DataFrame({c: (c in MAJORS) for c in cols}, index=d.pc.index) & d.sc.notna() & d.pc.notna() & paired
    n = 5 if kind == "TOP5" else 10
    age = d.pc.notna().cumsum()
    ok = (age >= 90) & d.sc.notna() & d.pc.notna()
    ok = ok & pd.DataFrame({c: c not in STABLE_LIKE for c in cols}, index=d.pc.index)
    liq = d.qv.rolling(90, min_periods=60).mean().where(ok)
    # re-ranked on the first day of each month, held for the month (fewer universe flips)
    month_start = d.pc.index.to_series().dt.is_month_start
    rank = liq.rank(axis=1, ascending=False).where(month_start).ffill()
    return (rank <= n) & ok & paired


def run_carry(d: Data, sc: Scenario, spec: CarrySpec, allowed: pd.Series | None = None,
              cap_frac: pd.Series | None = None, name: str | None = None) -> Result:
    """Delta-neutral cash-and-carry.

    Per held coin: long Q spot + short Q perp (same coin units). Notional per
    slot = slot capital / (1 + 1/perp_lev). Daily P/L = Q*(dSpot - dPerp) +
    Q*perp_close*funding_day (a short RECEIVES positive funding, PAYS negative).
    Rebalance both legs to target when price moved > 10 % since the last
    rebalance (hedge / margin maintenance). Liquidation if the perp HIGH
    rises > 0.9/perp_lev above the last rebalance price.
    `allowed` (bool per day) and `cap_frac` (fraction of capital usable) let the
    Trend+Carry combinations gate / size the carry.
    """
    uni = carry_universe(d, spec.universe)
    fund = scen_funding(d, sc)
    apr7 = fund.rolling(7, min_periods=7).mean() * 365  # signal sees the scenario's funding (the bot reacts to it)
    gap = (d.sc / d.pc - 1).abs().to_numpy()
    syms = list(d.pc.columns)
    P, S, H, F = d.pc.to_numpy(), d.sc.to_numpy(), d.ph.to_numpy(), fund.to_numpy()
    A, U = apr7.to_numpy(), uni.to_numpy()
    T = len(d.pc)
    n_slots = {"MAJORS": 2, "TOP5": 5, "TOP10": 10}[spec.universe]
    capn = 1 + 1 / sc.carry_perp_lev
    daily, gross, costs, util, expo = (np.zeros(T) for _ in range(5))
    held: dict[int, dict] = {}  # j -> {"q": qty per unit capital, "ref": last rebalance perp price}
    trades = liquidations = 0
    funding_earned = basis_pnl = 0.0
    al = allowed.reindex(d.pc.index).fillna(False).to_numpy() if allowed is not None else np.ones(T, bool)
    cf = cap_frac.reindex(d.pc.index).fillna(0.0).to_numpy() if cap_frac is not None else np.ones(T)

    def cost_leg(j: int, notional: float, exit_: bool) -> float:
        major = syms[j] in MAJORS
        c = notional * (leg_cost(sc, major, True) + leg_cost(sc, major, False))
        if exit_:
            c += notional * (sc.basis_exit_major if major else sc.basis_exit_alt)
        return c

    for t in range(T - 1):
        # 1) P/L of positions held over day t+1
        g = c_ = 0.0
        for j in list(held):
            pos = held[j]
            if not (np.isfinite(P[t + 1, j]) and np.isfinite(S[t + 1, j]) and np.isfinite(P[t, j]) and np.isfinite(S[t, j])):
                c_ += cost_leg(j, pos["q"] * (P[t, j] if np.isfinite(P[t, j]) else pos["ref"]), True)
                del held[j]
                trades += 1
                continue
            q = pos["q"]
            if gap[t + 1, j] > 0.05:  # spot and perp no longer move together: unhedgeable -> exit
                g += q * ((S[t + 1, j] - S[t, j]) - (P[t + 1, j] - P[t, j]))
                c_ += cost_leg(j, q * P[t + 1, j], True)
                del held[j]
                trades += 1
                continue
            if np.isfinite(H[t + 1, j]) and H[t + 1, j] >= pos["ref"] * (1 + 0.9 / sc.carry_perp_lev):
                liq_px = pos["ref"] * (1 + 0.9 / sc.carry_perp_lev)
                g += q * (S[t + 1, j] - S[t, j]) - q * (liq_px - P[t, j])
                c_ += q * liq_px * 0.01 + cost_leg(j, q * S[t + 1, j], True)
                liquidations += 1
                trades += 1
                del held[j]
                continue
            basis = q * ((S[t + 1, j] - S[t, j]) - (P[t + 1, j] - P[t, j]))
            fpay = q * P[t + 1, j] * F[t + 1, j]
            g += basis + fpay
            basis_pnl += basis
            funding_earned += fpay
            # hedge maintenance: rebalance when price moved > 10 % since the last rebalance
            if abs(P[t + 1, j] / pos["ref"] - 1) > 0.10:
                target_q = pos["target_cap"] / capn / P[t + 1, j]
                c_ += cost_leg(j, abs(target_q - q) * P[t + 1, j], True)
                pos["q"], pos["ref"] = target_q, P[t + 1, j]
                trades += 1
        # 2) decisions at the close of t+1 for day t+2
        a = A[t + 1]
        slot_cap = cf[t + 1] / n_slots
        for j in list(held):
            if not al[t + 1] or not U[t + 1, j] or not (np.isfinite(a[j]) and a[j] >= spec.exit_apr) or slot_cap <= 0:
                c_ += cost_leg(j, held[j]["q"] * P[t + 1, j], True)
                del held[j]
                trades += 1
        if al[t + 1] and slot_cap > 0:
            free = n_slots - len(held)
            cands = [(a[j], j) for j in np.flatnonzero(U[t + 1]) if j not in held and np.isfinite(a[j])
                     and a[j] > spec.entry_apr and np.isfinite(P[t + 1, j]) and np.isfinite(S[t + 1, j])]
            for _, j in sorted(cands, reverse=True)[:max(free, 0)]:
                q = slot_cap / capn / P[t + 1, j]
                held[j] = {"q": q, "ref": P[t + 1, j], "target_cap": slot_cap}
                c_ += cost_leg(j, q * P[t + 1, j], False)
                trades += 1
            # resize held slots when the usable capital changed by > 25 % (combination C-C only)
            for j, pos in held.items():
                if pos["target_cap"] > 0 and abs(slot_cap / pos["target_cap"] - 1) > 0.25:
                    target_q = slot_cap / capn / P[t + 1, j]
                    c_ += cost_leg(j, abs(target_q - pos["q"]) * P[t + 1, j], abs(target_q) < abs(pos["q"]))
                    pos.update(q=target_q, ref=P[t + 1, j], target_cap=slot_cap)
                    trades += 1
        daily[t + 1] = g - c_
        gross[t + 1] = g
        costs[t + 1] = c_
        notional = sum(pos["q"] * P[t + 1, j] for j, pos in held.items())
        util[t + 1] = notional * capn
        expo[t + 1] = 0.0  # delta-neutral by construction (basis risk is in the P/L)
    idx = d.pc.index
    return Result(name or spec.name, pd.Series(daily, idx), pd.Series(gross, idx), pd.Series(costs, idx),
                  pd.Series(util, idx), pd.Series(expo, idx), trades,
                  {"liquidations": liquidations, "funding": funding_earned, "basis": basis_pnl})


# ------------------------------------------------------------------ combinations and baselines

def combo_switch(d: Data, sc: Scenario, trend: Result) -> tuple[Result, Result]:
    """C-A: carry (BTC+ETH, T0) runs with ALL capital on days the trend holds nothing."""
    w = trend.extra["weights"]
    flat = (w.sum(axis=1) == 0)  # decided at close d -> carry may hold over d+1
    carry = run_carry(d, sc, next(s for s in CARRY_SPECS if s.name == CARRY_PRIMARY), allowed=flat,
                      name="carry part of C-A")
    daily = trend.daily + carry.daily
    res = Result("TREND+CARRY (C-A switch)", daily, trend.gross + carry.gross, trend.costs + carry.costs,
                 trend.util + carry.util, trend.exposure, trend.trades + carry.trades,
                 {"trend_part": trend.daily, "carry_part": carry.daily, "liquidations": carry.extra["liquidations"]})
    return res, carry


def combo_overlay(d: Data, sc: Scenario, trend: Result) -> tuple[Result, Result]:
    """C-C: trend at full size; carry uses the capital trend margin does not encumber, minus a 20 % buffer."""
    w = trend.extra["weights"]
    frac = (0.8 - w.sum(axis=1) / sc.trend_margin_lev).clip(lower=0.0)
    carry = run_carry(d, sc, next(s for s in CARRY_SPECS if s.name == CARRY_PRIMARY), cap_frac=frac,
                      name="carry part of C-C")
    daily = trend.daily + carry.daily
    res = Result("TREND+CARRY (C-C overlay)", daily, trend.gross + carry.gross, trend.costs + carry.costs,
                 trend.util + carry.util, trend.exposure, trend.trades + carry.trades,
                 {"trend_part": trend.daily, "carry_part": carry.daily, "liquidations": carry.extra["liquidations"]})
    return res, carry


def buy_hold(d: Data, sc: Scenario, scale: float = 1.0, name: str = "BUY&HOLD BTC/ETH 50/50") -> Result:
    """50/50 BTC/ETH perps at `scale` x capital, rebalanced monthly; pays funding; entry cost once."""
    ret = d.pc[list(MAJORS)].pct_change(fill_method=None).fillna(0.0)
    fund = scen_funding(d, sc)[list(MAJORS)]
    w = pd.DataFrame(scale / 2, index=d.pc.index, columns=list(MAJORS))
    held = w.shift(1).fillna(0.0)
    gross = (held * ret).sum(axis=1) - (held * fund).sum(axis=1)
    # monthly rebalance back to 50/50: approximate turnover = drift between the two assets over the month
    month_start = d.pc.index.to_series().dt.is_month_start
    drift = (ret["BTCUSDT"] - ret["ETHUSDT"]).abs().groupby(month_start.cumsum()).transform("sum") * scale / 4
    costs = (drift.where(month_start, 0.0) * leg_cost(sc, True, False)).shift(1).fillna(0.0)
    return Result(name, gross - costs, gross, costs, held.sum(axis=1) / sc.trend_margin_lev, held.sum(axis=1),
                  int(month_start.sum()), {})


def cash(d: Data) -> Result:
    z = pd.Series(0.0, index=d.pc.index)
    return Result("CASH", z, z, z, z, z, 0, {})


# ------------------------------------------------------------------ metrics

def block_p(x: np.ndarray, block: int = 20, n_boot: int = 4000, seed: int = 11) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 2 * block or x.std() == 0:
        return 1.0
    rng = np.random.default_rng(seed)
    xc = x - x.mean()
    nb = int(np.ceil(len(x) / block))
    st = rng.integers(0, len(x), size=(n_boot, nb))
    idx = (st[:, :, None] + np.arange(block)).reshape(n_boot, -1)[:, :len(x)] % len(x)
    return float((xc[idx].mean(axis=1) >= x.mean()).mean())


def block_ci(x: np.ndarray, block: int = 20, n_boot: int = 4000, seed: int = 12) -> tuple[float, float]:
    x = x[np.isfinite(x)]
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(x) / block))
    st = rng.integers(0, len(x), size=(n_boot, nb))
    idx = (st[:, :, None] + np.arange(block)).reshape(n_boot, -1)[:, :len(x)] % len(x)
    m = x[idx].mean(axis=1)
    return float(np.quantile(m, 0.025)), float(np.quantile(m, 0.975))


def bh(p: dict[str, float], q: float = 0.10) -> set[str]:
    items = sorted(p.items(), key=lambda kv: kv[1])
    k = 0
    for i, (_, pv) in enumerate(items, 1):
        if pv <= q * i / len(items):
            k = i
    return {n for n, _ in items[:k]}


def metrics(r: Result, a: str, b: str) -> dict:
    x = r.daily[a:b]
    eq = (1 + x).cumprod()
    under = eq < eq.cummax()
    longest = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    pos, neg = x[x > 0], x[x < 0]
    n = len(x)
    return {
        "days": n,
        "net_per_1000": 1000 * (eq.iloc[-1] - 1) if n else 0.0,
        "gross_per_1000_simple": 1000 * r.gross[a:b].sum(),
        "costs_per_1000_simple": 1000 * r.costs[a:b].sum(),
        "net_per_1000_simple": 1000 * x.sum(),
        "ann_ret": float(eq.iloc[-1] ** (365 / n) - 1) if n else 0.0,
        "sharpe": float(x.mean() / x.std() * np.sqrt(365)) if x.std() > 0 else 0.0,
        "pos_days": int((x > 0).sum()),
        "pos_share": float((x > 0).mean()),
        "pos_share_active": float((x > 0).sum() / max((x != 0).sum(), 1)),
        "avg_pos_day": 1000 * float(pos.mean()) if len(pos) else 0.0,
        "avg_neg_day": 1000 * float(neg.mean()) if len(neg) else 0.0,
        "median_day": 1000 * float(x.median()),
        "best_day": 1000 * float(x.max()),
        "worst_day": 1000 * float(x.min()),
        "mdd": float((eq / eq.cummax() - 1).min()),
        "dd_days": int(longest),
        "util_avg": float(r.util[a:b].mean()),
        "exposure_avg": float(r.exposure[a:b].mean()),
        "p_mean_gt0": block_p(x.to_numpy()),
    }


def regimes(d: Data) -> pd.Series:
    """Descriptive regime of each day from the BTC trailing 60-day return known at the previous close."""
    r60 = (d.pc["BTCUSDT"] / d.pc["BTCUSDT"].shift(60) - 1).shift(1)
    return pd.Series(np.where(r60 > 0.15, "UP", np.where(r60 < -0.15, "DOWN", "SIDEWAYS")), index=d.pc.index)


# ------------------------------------------------------------------ main

def main() -> None:
    d = load()
    base = SCENARIOS[0]
    out: dict = {"periods": PERIODS, "scenarios": {}}
    reg = regimes(d)

    # ---- base scenario: all strategies
    trend = run_trend(d, base)
    ca, ca_carry = combo_switch(d, base, trend)
    cc, cc_carry = combo_overlay(d, base, trend)
    bh_avg_exposure = float(trend.exposure[FULL[0]:FULL[1]].mean())
    strategies = {
        "CASH": cash(d),
        "BUY&HOLD 1x": buy_hold(d, base),
        f"BUY&HOLD {bh_avg_exposure:.2f}x (trend's avg exposure)": buy_hold(d, base, bh_avg_exposure, "BUY&HOLD exposure-matched"),
        "TREND": trend,
        "TREND+CARRY C-A (switch)": ca,
        "TREND+CARRY C-C (overlay)": cc,
    }
    carries = {s.name: run_carry(d, base, s) for s in CARRY_SPECS}
    strategies.update(carries)
    # generalisation of the frozen trend rule: point-in-time top-10 alts by 90 d volume, monthly
    alt_uni = carry_universe(d, "TOP10")  # same point-in-time liquidity ranking, spot not needed for trend
    alts = [c for c in d.pc.columns if c not in MAJORS and c not in STABLE_LIKE]
    tw = trend_weights(d.pc, alts)
    mask = alt_uni[alts]
    tw = tw.where(mask, 0.0) * len(alts) / 10.0  # each name gets the 1/10 share instead of 1/len(alts)
    tw = tw.clip(upper=2.0 / 10.0)
    tw_band = apply_band(tw, 0.05 / 10)
    ret = d.pc[alts].pct_change(fill_method=None).fillna(0.0)
    held = tw_band.shift(1).fillna(0.0)
    g = (held * ret).sum(axis=1) - (held * d.fund[alts]).sum(axis=1)
    c = ((tw_band - tw_band.shift(1).fillna(0.0)).abs() * leg_cost(base, False, False)).sum(axis=1).shift(1).fillna(0.0)
    strategies["TREND rule on alts (generalisation)"] = Result("TREND alts", g - c, g, c, held.sum(axis=1) / 3,
                                                               held.sum(axis=1), int(((tw_band.diff().abs().sum(axis=1)) > 0).sum()), {})

    table = {}
    for name, r in strategies.items():
        table[name] = {"FULL": metrics(r, *FULL), **{p: metrics(r, a, b) for p, (a, b) in PERIODS.items()},
                       "trades": r.trades, "extra": {k: v for k, v in r.extra.items() if isinstance(v, (int, float))}}
        by_reg = {}
        x = r.daily[FULL[0]:FULL[1]]
        for rg in ("UP", "SIDEWAYS", "DOWN"):
            xr = x[reg[FULL[0]:FULL[1]] == rg]
            by_reg[rg] = {"days": int(len(xr)), "mean_per_1000": 1000 * float(xr.mean()),
                          "pos_share": float((xr > 0).mean()), "sum_per_1000": 1000 * float(xr.sum())}
        table[name]["regimes"] = by_reg
        yrs = r.daily[FULL[0]:FULL[1]]
        table[name]["years"] = {str(y): float((1 + v).prod() - 1) for y, v in yrs.groupby(yrs.index.year)}
    out["base"] = table

    # ---- carry hypotheses: BH on TRAIN+VALID
    psel = {n: block_p(r.daily[PERIODS["TRAIN"][0]:PERIODS["VALID"][1]].to_numpy()) for n, r in carries.items()}
    out["carry_bh_pass"] = sorted(bh(psel))
    out["carry_p_trainvalid"] = psel

    # ---- trend + carry decomposition (C-A and C-C) vs trend
    decomp = {}
    for label, combo, part in (("C-A", ca, ca_carry), ("C-C", cc, cc_carry)):
        diff = (combo.daily - trend.daily)
        res = {}
        for p, (a, b) in {"FULL": FULL, **PERIODS}.items():
            dx = diff[a:b].to_numpy()
            lo, hi = block_ci(dx)
            mt, mc = metrics(trend, a, b), metrics(combo, a, b)
            res[p] = {"trend_contrib_per_1000": 1000 * float(trend.daily[a:b].sum()),
                      "carry_contrib_per_1000": 1000 * float(part.daily[a:b].sum()),
                      "carry_gross_per_1000": 1000 * float(part.gross[a:b].sum()),
                      "carry_costs_per_1000": 1000 * float(part.costs[a:b].sum()),
                      "interaction_per_1000": 1000 * float(combo.daily[a:b].sum() - trend.daily[a:b].sum() - part.daily[a:b].sum()),
                      "delta_mean_day_per_1000": 1000 * float(dx.mean()), "delta_ci95": (1000 * lo, 1000 * hi),
                      "delta_sharpe": mc["sharpe"] - mt["sharpe"], "delta_mdd_pts": 100 * (mc["mdd"] - mt["mdd"]),
                      "delta_pos_share_pts": 100 * (mc["pos_share"] - mt["pos_share"]),
                      "corr_trend_carry_days": float(np.corrcoef(trend.daily[a:b], part.daily[a:b])[0, 1])
                      if part.daily[a:b].std() > 0 else 0.0}
        decomp[label] = res
    out["decomposition"] = decomp

    # ---- sensitivity: every scenario, key strategies
    sens = {}
    for scn in SCENARIOS:
        tr = run_trend(d, scn)
        c_a, _ = combo_switch(d, scn, tr)
        cr = run_carry(d, scn, next(s for s in CARRY_SPECS if s.name == CARRY_PRIMARY))
        sens[scn.name] = {n: {p: {k: metrics(r, a, b)[k] for k in ("net_per_1000", "ann_ret", "pos_share", "mdd", "sharpe")}
                              for p, (a, b) in {"FULL": FULL, "TEST": PERIODS["TEST"], "HOLDOUT": PERIODS["HOLDOUT"]}.items()}
                          for n, r in (("TREND", tr), ("CARRY_MAJORS_T0", cr), ("TREND+CARRY C-A", c_a))}
        sens[scn.name]["CARRY_MAJORS_T0"]["liquidations"] = cr.extra["liquidations"]
    out["sensitivity"] = sens

    # ---- carry mechanics facts (base, primary)
    prim = carries[CARRY_PRIMARY]
    f7 = d.fund[list(MAJORS)].rolling(7, min_periods=7).mean() * 365
    basis = (d.sc[list(MAJORS)] / d.pc[list(MAJORS)] - 1)
    out["carry_facts"] = {
        "share_days_7d_apr_below_0": {a: float((f7[a][FULL[0]:FULL[1]] < 0).mean()) for a in MAJORS},
        "share_days_7d_apr_below_5pct": {a: float((f7[a][FULL[0]:FULL[1]] < 0.05).mean()) for a in MAJORS},
        "mean_apr_by_year": {a: {str(y): float(v.mean()) for y, v in (d.fund[a][FULL[0]:FULL[1]] * 365).groupby(d.fund[a][FULL[0]:FULL[1]].index.year)} for a in MAJORS},
        "basis_daily_change_bp": {a: {"p50_abs": float(basis[a].diff().abs().median() * 1e4),
                                      "p99_abs": float(basis[a].diff().abs().quantile(0.99) * 1e4),
                                      "max_abs": float(basis[a].diff().abs().max() * 1e4)} for a in MAJORS},
        "worst_day_per_1000": 1000 * float(prim.daily[FULL[0]:FULL[1]].min()),
        "trades": prim.trades, "liquidations": prim.extra["liquidations"],
        "active_share": float((prim.util[FULL[0]:FULL[1]] > 0).mean()),
    }
    OUT_JSON.write_text(json.dumps(out, default=lambda o: o if not isinstance(o, (np.floating, np.integer)) else o.item(),
                                   indent=1))
    print("written", OUT_JSON)


if __name__ == "__main__":
    main()
