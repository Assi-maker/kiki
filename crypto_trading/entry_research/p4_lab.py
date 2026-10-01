"""P4 strategy-redesign research: fundamentally different market hypotheses.

    python -m crypto_trading.entry_research.p4_lab

Read-only research on the cached Binance archive (p4_data) and the free
Tardis liquidation sample days (p4_liq_fill). Nothing in the bot imports this.

PRE-REGISTERED (written before any P4 outcome was computed)
===========================================================
Every hypothesis is an ENTRY rule with a stated mechanism. Its entry edge is
measured in isolation: fixed holding periods, no stop, no target, no exit
logic. A cell = (family, parameters, side/leg, horizon).

F1 XS_REL - cross-sectional relative strength, rebalanced every hour.
   Mechanism MOM: slow diffusion of information / attention herding makes
   relative winners keep winning (documented for crypto at days-weeks).
   Mechanism REV: uninformed flow pushes single coins too far relative to the
   market; liquidity providers are paid when it reverts.
   Rank past return over L in {1h,4h,24h,72h,7d}; long/short the top/bottom
   quintile of the eligible universe (>= 40 symbols, >= 7 d history).
   Legs LONG, SHORT and LS (both legs). Horizons 30m..24h.
F2 IDIO_SHOCK - idiosyncratic 15m shock (return minus the market median,
   z vs its own trailing 7 d std, |z| >= {3,4,6}, 15m volume >= 3x its 7 d
   mean, first bar of the excursion, 4 h cooldown). Split by the same-window
   OI change: OI_DOWN (<= -1 %: positions forced/closed -> liquidity demand
   -> overshoot -> FADE should win), OI_UP (>= +1 %: new positioning ->
   information -> FOLLOW should win), ALL. Both FADE and FOLLOW are tested.
F3 MKT_CASCADE - market-wide forced deleveraging: median 1h return of the
   universe <= -X (or >= +X), X in {1.5,2.5,4} %, breadth >= 80 % in the
   same direction, aggregate OI value 1h change <= -1 % (OI_DOWN) or ALL;
   12 h cooldown. Trade = every eligible symbol (the basket). FADE / FOLLOW.
F4 BREAKOUT - structural breakout of the N-day range, N in {3,7,14}
   (level = extreme of the range ending 1 h before; first close beyond it
   after 24 h of closes inside). Confirmation NONE (enter on the break),
   HOLD (all closes of the next hour stay beyond the level and that hour's
   volume >= 1.5x the 7 d mean), RETEST (within 24 h price comes back to
   within 0.3 % of the level and closes beyond it; a close 1 % back inside
   first = invalidated). Regime ALL or BTC_ALIGNED (BTC 72h return has the
   breakout's sign). FOLLOW for all; FADE (false break) for NONE only.
   Mechanism: stop orders and trend followers cluster beyond range extremes;
   confirmation separates real repricing from stop runs.
F5 CROWDING - cross-sectional positioning at every funding settlement
   (00/08/16 UTC): last settled funding rate, top-trader position L/S ratio,
   all-account L/S ratio, 24h OI change signed by the 24h price change.
   CONTRA (fade the crowded side) and FOLLOW; legs LONG/SHORT/LS.
   Mechanism: crowded leveraged positioning pays carry (funding) and is the
   side that gets squeezed. Funding P/L is included in every trade.
F6 REAL_LIQ - REAL Binance liquidations (Tardis free first-of-month days,
   14 days, 2025-08..2026-09). Event: one side's liquidated notional in a 5m
   bar >= theta x the symbol's trailing-24h mean 5m quote volume, theta in
   {1,3,10} %, and >= 10 000 USDT; 1 h cooldown. FADE (buy after longs were
   liquidated) / FOLLOW. Mechanism: forced orders are price-insensitive
   liquidity demand; the overshoot should revert. Clusters = hours (only 14
   days exist), flagged as weaker evidence than the other families.

Data/timing: bar k is known at its END. Signal on bar k; entry at the close
of bar k + 1 (5 min latency, a deterministic 5-min engine; sensitivity
k + 3 = 15 min). OI/LS metrics stamped at a bar's start (one bar lag).
Horizons 5m 15m 30m 1h 2h 4h 8h 24h (XS families: 30m..24h, F5 1h..24h).
Costs: taker 0.05 % + slippage 0.03 % per side = 0.16 % round trip, plus the
real funding paid/received over the hold; stress 0.25 % round trip.
Periods (entry time): TRAIN 2025-08..2026-01, VALID 02..04, TEST 05..07,
HOLDOUT 2026-08..09-29; entries whose 24 h window reaches the next period are
purged. Clusters: UTC day (horizon < 8 h) or ISO week (>= 8 h); F6 hour.

SELECTION (TRAIN + VALID only), then frozen with a sha256:
  S1 TRAIN: one-sided cluster-t p for NET mean > 0; Benjamini-Hochberg over
     ALL cells of ALL families, q < 0.10; cells with < 30 trades or < 20
     clusters get p = 1 (still counted).
  S2 VALID: NET mean > 0 and one-sided p < 0.05.
SURVIVING EDGE additionally requires:
  S3 TEST and HOLDOUT NET mean > 0, pooled TEST+HOLDOUT one-sided p < 0.05;
  S4 NET mean > 0 in all four periods;
  S5 >= 1 trade/day on average and top-5 symbols <= 50 % of the net P/L;
  S6 latency: NET mean > 0 at 15 min latency in TRAIN+VALID pooled;
  S7 cost stress: NET at 0.25 % round trip > 0 in TEST+HOLDOUT pooled;
  S8 a mechanism (stated above for every family).
If nothing survives: "NO ROBUST EDGE FOUND", and nothing is built.
Exit models are only tested for a surviving entry (separately reported).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps
from scipy.ndimage import maximum_filter1d, minimum_filter1d

from crypto_trading.entry_research import p4_data as D

OUT = Path("data/entry_research/p4_results.json")
SEL = Path("data/entry_research/p4_selection.json")
LIQ_DB = Path("data/entry_research/p4_liq.db")
H_BARS = {"5m": 1, "15m": 3, "30m": 6, "1h": 12, "2h": 24, "4h": 48, "8h": 96, "24h": 288}
XS_H = ("30m", "1h", "2h", "4h", "8h", "24h")
CROWD_H = ("1h", "2h", "4h", "8h", "24h")
COST, COST_STRESS = 0.0016, 0.0025
LAT, LAT_SLOW = 1, 3
MAXH = 288
AGE_MIN = 2016  # 7 days of history before a symbol is eligible
PERIOD_NAMES = ("TRAIN", "VALID", "TEST", "HOLDOUT")
PERIOD_START = [0] + [
    (int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000) - D.T0) // D.STEP
    for y, m in ((2026, 2), (2026, 5), (2026, 8))
]
PERIOD_END = PERIOD_START[1:] + [D.N - MAXH - LAT_SLOW - 1]
PERIOD_DAYS = [(b - a) / 288 for a, b in zip(PERIOD_START, PERIOD_END)]
LIQ_DAYS_PER_PERIOD = [6, 3, 3, 2]


@dataclass
class TradeSet:
    family: str
    params: dict
    sym: np.ndarray  # int32
    k: np.ndarray  # int32 signal bar
    per_h: dict = field(default_factory=dict)


@dataclass
class Cell:
    family: str
    params: dict
    side_label: str
    legs: list  # [(set_index, side)]
    horizons: tuple


# --------------------------------------------------------------------------- helpers


def period_of(e: np.ndarray) -> np.ndarray:
    """Period id per entry bar, -1 when purged (24 h window reaches the next period)."""
    out = np.full(e.shape, -1, dtype=np.int8)
    for i, (a, b) in enumerate(zip(PERIOD_START, PERIOD_END)):
        out[(e >= a) & (e + MAXH < b + (MAXH if i == 3 else 0)) & (e < b)] = i
    return out


def cluster_p(v: np.ndarray, g: np.ndarray) -> tuple[float, float, int]:
    """(mean, one-sided p for mean > 0, clusters) with cluster-robust SE."""
    n = len(v)
    if n < 2:
        return (float(v.mean()) if n else 0.0), 1.0, 0
    m = float(v.mean())
    _, inv = np.unique(g, return_inverse=True)
    s = np.bincount(inv, weights=v - m)
    G = len(s)
    if G < 2:
        return m, 1.0, G
    var = float((s * s).sum()) / (n * n) * G / (G - 1)
    if var <= 0:
        return m, 1.0, G
    t = m / math.sqrt(var)
    return m, float(sps.t.sf(t, G - 1)), G


def bh(p: np.ndarray) -> np.ndarray:
    m = len(p)
    order = np.argsort(p)
    q = np.empty(m)
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, p[i] * m / rank)
        q[i] = prev
    return q


def cooldown(k: np.ndarray, gap: int) -> np.ndarray:
    """Keep events at least `gap` bars after the previous kept one (k sorted)."""
    keep, last = [], -(10**9)
    for x in k:
        if x - last >= gap:
            keep.append(x)
            last = x
    return np.array(keep, dtype=np.int32)


def roll(a: np.ndarray, fn: str, w: int, minp: int | None = None) -> np.ndarray:
    df = pd.DataFrame(a.T)
    r = getattr(df.rolling(w, min_periods=minp or w), fn)()
    return r.values.T.astype("f4")


def shift(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    out[:, n:] = a[:, :-n]
    return out


# --------------------------------------------------------------------------- families


def xs_sets(family: str, score: np.ndarray, rebal: np.ndarray, age_ok: np.ndarray, params: dict) -> list[TradeSet]:
    top_s, top_k, bot_s, bot_k = [], [], [], []
    for k in rebal:
        v = score[:, k]
        ok = np.isfinite(v) & age_ok[:, k]
        idx = np.nonzero(ok)[0]
        if len(idx) < 40:
            continue
        q = len(idx) // 5
        o = idx[np.argsort(v[idx], kind="stable")]
        bot_s.append(o[:q]); bot_k.append(np.full(q, k))
        top_s.append(o[-q:]); top_k.append(np.full(q, k))
    cat = lambda xs: np.concatenate(xs).astype(np.int32) if xs else np.zeros(0, np.int32)  # noqa: E731
    return [
        TradeSet(family, {**params, "q": "TOP"}, cat(top_s), cat(top_k)),
        TradeSet(family, {**params, "q": "BOT"}, cat(bot_s), cat(bot_k)),
    ]


def xs_cells(family: str, i_top: int, i_bot: int, params: dict, variants: tuple, horizons: tuple) -> list[Cell]:
    cells = []
    for var in variants:
        # "high score" side: MOM/FOLLOW long the top; REV/CONTRA long the bottom
        if var in ("MOM", "FOLLOW"):
            long_leg, short_leg = (i_top, 1), (i_bot, -1)
        else:
            long_leg, short_leg = (i_bot, 1), (i_top, -1)
        p = {**params, "variant": var}
        cells += [
            Cell(family, p, "LONG", [long_leg], horizons),
            Cell(family, p, "SHORT", [short_leg], horizons),
            Cell(family, p, "LS", [long_leg, short_leg], horizons),
        ]
    return cells


def build_f1(C: np.ndarray, age_ok: np.ndarray, sets: list, cells: list) -> None:
    rebal = np.arange(11, D.N, 12)
    for L, Lb in (("1h", 12), ("4h", 48), ("24h", 288), ("72h", 864), ("7d", 2016)):
        score = C / shift(C, Lb) - 1
        i0 = len(sets)
        sets += xs_sets("F1_XS_REL", score, rebal, age_ok, {"L": L})
        cells += xs_cells("F1_XS_REL", i0, i0 + 1, {"L": L}, ("MOM", "REV"), XS_H)
        del score


def build_f2(C, QV, OIV, age_ok, sets, cells) -> None:
    r15 = C / shift(C, 3) - 1
    mkt = np.nanmedian(r15, axis=0)
    res = r15 - mkt[None, :]
    del r15
    sd = shift(roll(res, "std", 2016, 1000), 1)
    z = res / sd
    del res, sd
    qv15 = roll(np.nan_to_num(QV), "sum", 3)
    base = shift(roll(QV, "mean", 2016, 1000), 3) * 3
    vr = qv15 / base
    del qv15, base
    doi = OIV / shift(OIV, 3) - 1
    zprev = shift(z, 1)
    for thr in (3.0, 4.0, 6.0):
        for sign in (1, -1):
            hit = (sign * z >= thr) & ~(sign * zprev >= thr) & (vr >= 3) & age_ok
            ev_s, ev_k = [], []
            for s in range(C.shape[0]):
                ks = cooldown(np.nonzero(hit[s])[0], 48)
                ev_s.append(np.full(len(ks), s, np.int32)); ev_k.append(ks)
            es, ek = np.concatenate(ev_s), np.concatenate(ev_k)
            d = doi[es, ek]
            for oi, m in (("ALL", np.ones(len(es), bool)), ("OI_DOWN", d <= -0.01), ("OI_UP", d >= 0.01)):
                p = {"z": thr, "shock": "UP" if sign > 0 else "DOWN", "oi": oi}
                sets.append(TradeSet("F2_IDIO_SHOCK", p, es[m], ek[m]))
                i = len(sets) - 1
                cells.append(Cell("F2_IDIO_SHOCK", {**p, "dir": "FADE"}, "LONG" if sign < 0 else "SHORT", [(i, -sign)], tuple(H_BARS)))
                cells.append(Cell("F2_IDIO_SHOCK", {**p, "dir": "FOLLOW"}, "LONG" if sign > 0 else "SHORT", [(i, sign)], tuple(H_BARS)))
    del z, zprev, vr, doi


def build_f3(C, OIV, age_ok, sets, cells, meta) -> None:
    r60 = C / shift(C, 12) - 1
    ok = np.isfinite(r60) & age_ok
    cnt = ok.sum(0)
    m60 = np.nanmedian(np.where(ok, r60, np.nan), axis=0)
    up = np.where(ok, r60 > 0, False).sum(0) / np.maximum(cnt, 1)
    O0 = shift(OIV, 12)
    both = np.isfinite(OIV) & np.isfinite(O0)
    doiA = np.where(both, OIV, 0).sum(0) / np.maximum(np.where(both, O0, 0).sum(0), 1) - 1
    meta["f3_events"] = {}
    for X in (0.015, 0.025, 0.04):
        for sign in (1, -1):
            cond = (cnt >= 40) & (sign * m60 >= X) & ((up if sign > 0 else 1 - up) >= 0.8)
            for oi in ("ALL", "OI_DOWN"):
                c2 = cond & (doiA <= -0.01) if oi == "OI_DOWN" else cond
                start = np.nonzero(c2 & ~np.r_[False, c2[:-1]])[0]
                ks = cooldown(start, 144)
                es = [np.nonzero(ok[:, k])[0] for k in ks]
                s_arr = np.concatenate(es).astype(np.int32) if es else np.zeros(0, np.int32)
                k_arr = np.concatenate([np.full(len(e), k) for e, k in zip(es, ks)]).astype(np.int32) if es else np.zeros(0, np.int32)
                p = {"X": X, "shock": "UP" if sign > 0 else "DOWN", "oi": oi}
                meta["f3_events"][json.dumps(p)] = [datetime.fromtimestamp((D.T0 + (int(k) + 1) * D.STEP) / 1000, UTC).isoformat() for k in ks]
                sets.append(TradeSet("F3_MKT_CASCADE", p, s_arr, k_arr))
                i = len(sets) - 1
                cells.append(Cell("F3_MKT_CASCADE", {**p, "dir": "FADE"}, "LONG" if sign < 0 else "SHORT", [(i, -sign)], tuple(H_BARS)))
                cells.append(Cell("F3_MKT_CASCADE", {**p, "dir": "FOLLOW"}, "LONG" if sign > 0 else "SHORT", [(i, sign)], tuple(H_BARS)))
    del r60, O0


def build_f4(C, H, L, QV, age_ok, btc, sets, cells) -> None:
    qvm = roll(QV, "mean", 2016, 1000)
    b72 = C[btc] / np.r_[np.full(864, np.nan), C[btc][:-864]] - 1
    for Nd in (3, 7, 14):
        w = Nd * 288
        out = {}  # (dir, conf) -> list of (s, k)
        for s in range(C.shape[0]):
            c, h, l = pd.Series(C[s]), pd.Series(H[s]), pd.Series(L[s])
            hi = h.rolling(w, min_periods=w).max().shift(12).values
            lo = l.rolling(w, min_periods=w).min().shift(12).values
            cmax = c.rolling(288, min_periods=288).max().shift(1).values
            cmin = c.rolling(288, min_periods=288).min().shift(1).values
            cv = C[s]
            for dr, lev, brk, inside in ((1, hi, cv > hi, cmax <= hi), (-1, lo, cv < lo, cmin >= lo)):
                ks = cooldown(np.nonzero(brk & inside & age_ok[s])[0], 288)
                for k in ks:
                    level = lev[k]
                    out.setdefault((dr, "NONE"), []).append((s, k))
                    k2 = k + 12
                    if k2 < D.N and np.all(dr * (cv[k : k2 + 1] - level) > 0) and np.nansum(QV[s, k + 1 : k2 + 1]) >= 1.5 * 12 * qvm[s, k]:
                        out.setdefault((dr, "HOLD"), []).append((s, k2))
                    for j in range(k + 13, min(k + 289, D.N)):
                        if not np.isfinite(cv[j]):
                            continue
                        if dr * (cv[j] - level * (1 - dr * 0.01)) < 0:
                            break  # closed 1 % back inside: invalidated
                        touch = (L[s, j] <= level * 1.003) if dr > 0 else (H[s, j] >= level * 0.997)
                        if touch and dr * (cv[j] - level) > 0:
                            out.setdefault((dr, "RETEST"), []).append((s, j))
                            break
        for (dr, conf), lst in out.items():
            arr = np.array(lst, dtype=np.int64)
            sides = "UP" if dr > 0 else "DOWN"
            for reg in ("ALL", "BTC_ALIGNED"):
                m = np.ones(len(arr), bool) if reg == "ALL" else (np.sign(b72[arr[:, 1]]) == dr)
                p = {"N_days": Nd, "break": sides, "conf": conf, "regime": reg}
                sets.append(TradeSet("F4_BREAKOUT", p, arr[m, 0].astype(np.int32), arr[m, 1].astype(np.int32)))
                i = len(sets) - 1
                cells.append(Cell("F4_BREAKOUT", {**p, "dir": "FOLLOW"}, "LONG" if dr > 0 else "SHORT", [(i, dr)], tuple(H_BARS)))
                if conf == "NONE":
                    cells.append(Cell("F4_BREAKOUT", {**p, "dir": "FADE"}, "SHORT" if dr > 0 else "LONG", [(i, -dr)], tuple(H_BARS)))
    del qvm


def build_f5(C, OIV, age_ok, sets, cells) -> None:
    rebal = np.arange(95, D.N, 96)
    ret24 = C / shift(C, 288) - 1
    scores = {
        "FUNDING": np.asarray(D.load("fr_last"), dtype="f4"),
        "TOP_LS": np.asarray(D.load("tlsp"), dtype="f4"),
        "ACCT_LS": np.asarray(D.load("lsa"), dtype="f4"),
        "OI_BUILD": (OIV / shift(OIV, 288) - 1) * np.sign(ret24),
    }
    for name, sc in scores.items():
        i0 = len(sets)
        sets += xs_sets("F5_CROWDING", sc, rebal, age_ok, {"signal": name})
        cells += xs_cells("F5_CROWDING", i0, i0 + 1, {"signal": name}, ("CONTRA", "FOLLOW"), CROWD_H)
    del scores, ret24


def build_f6(QV, syms, sets, cells, meta) -> None:
    db = sqlite3.connect(f"file:{LIQ_DB}?mode=ro", uri=True)
    sidx = {s: i for i, s in enumerate(syms)}
    rows = np.array(
        [(sidx[s], (ts - D.T0) // D.STEP, 1 if side == "sell" else -1, usd) for s, ts, side, usd in db.execute("SELECT symbol, ts, side, usd FROM liq") if s in sidx],
        dtype="f8",
    )
    base = shift(roll(QV, "mean", 288, 144), 1)
    meta["f6_rows"] = int(len(rows))
    for liq_side in (1, -1):  # 1 = longs liquidated (forced SELL), -1 = shorts liquidated
        r = rows[rows[:, 2] == liq_side]
        key = r[:, 0].astype(np.int64) * D.N + r[:, 1].astype(np.int64)
        uk, inv = np.unique(key, return_inverse=True)
        usd = np.bincount(inv, weights=r[:, 3])
        s_arr, k_arr = (uk // D.N).astype(np.int32), (uk % D.N).astype(np.int32)
        for theta in (0.01, 0.03, 0.10):
            m = (usd >= 10_000) & (usd >= theta * base[s_arr, k_arr])
            es, ek = [], []
            for s in np.unique(s_arr[m]):
                ks = cooldown(np.sort(k_arr[m & (s_arr == s)]), 12)
                es.append(np.full(len(ks), s, np.int32)); ek.append(ks)
            es = np.concatenate(es) if es else np.zeros(0, np.int32)
            ek = np.concatenate(ek) if ek else np.zeros(0, np.int32)
            p = {"theta": theta, "liquidated": "LONGS" if liq_side > 0 else "SHORTS"}
            sets.append(TradeSet("F6_REAL_LIQ", p, es, ek))
            i = len(sets) - 1
            # longs liquidated = forced selling -> FADE = buy
            cells.append(Cell("F6_REAL_LIQ", {**p, "dir": "FADE"}, "LONG" if liq_side > 0 else "SHORT", [(i, liq_side)], tuple(H_BARS)))
            cells.append(Cell("F6_REAL_LIQ", {**p, "dir": "FOLLOW"}, "SHORT" if liq_side > 0 else "LONG", [(i, -liq_side)], tuple(H_BARS)))


# --------------------------------------------------------------------------- evaluation


def fwd_extreme(A: np.ndarray, hb: int, fn) -> np.ndarray:
    """out[:, j] = extreme of A[:, j+1 : j+hb+1]."""
    fill = -np.inf if fn is maximum_filter1d else np.inf
    Cc = fn(np.where(np.isfinite(A), A, fill), size=hb, axis=1, mode="nearest")
    out = np.full_like(A, np.nan)
    off = 1 + hb // 2
    out[:, : D.N - off] = Cc[:, off:]
    out[~np.isfinite(out)] = np.nan
    return out


def evaluate_horizon(sets: list, hname: str, C, H, L, CF) -> None:
    """Fill ts.per_h with ONLY this horizon (memory: one horizon at a time)."""
    hb = H_BARS[hname]
    hmax = fwd_extreme(H, hb, maximum_filter1d)
    lmin = fwd_extreme(L, hb, minimum_filter1d)
    for ts in sets:
        res = {}
        for lat in (LAT, LAT_SLOW):
            e = ts.k.astype(np.int64) + lat
            x = e + hb
            ok = x < D.N
            e, x, s = e[ok], x[ok], ts.sym[ok]
            ce = C[s, e].astype("f8")
            r = {"idx": np.nonzero(ok)[0], "ret": C[s, x] / ce - 1, "fund": CF[s, x] - CF[s, e], "e": e}
            if lat == LAT:
                r["up"] = hmax[s, e] / ce - 1
                r["dn"] = lmin[s, e] / ce - 1
            res[lat] = r
        ts.per_h = {hname: res}
    del hmax, lmin


def cell_values(cell: Cell, sets: list, hname: str, lat: int):
    parts = []
    for si, side in cell.legs:
        ts = sets[si]
        r = ts.per_h[hname][lat]
        ok = np.isfinite(r["ret"]) & np.isfinite(r["fund"])
        gross = side * r["ret"][ok]
        net_nofund = gross - COST
        net = net_nofund - side * r["fund"][ok]
        d = {"gross": gross, "net": net, "fund": -side * r["fund"][ok], "e": r["e"][ok], "s": ts.sym[r["idx"][ok]]}
        if lat == LAT:
            up, dn = r["up"][ok], r["dn"][ok]
            d["mfe"] = up if side > 0 else -dn
            d["mae"] = dn if side > 0 else -up
        parts.append(d)
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


def clusters(cell: Cell, hname: str, e: np.ndarray) -> np.ndarray:
    if cell.family == "F6_REAL_LIQ":
        return e // 12
    return e // (2016 if H_BARS[hname] >= 96 else 288)


def max_dd(net: np.ndarray, e: np.ndarray) -> float:
    if len(net) == 0:
        return 0.0
    eq = np.cumsum(net[np.argsort(e, kind="stable")])
    return float((np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]).max())


def stats_for(cell: Cell, sets: list, hname: str) -> dict:
    v = cell_values(cell, sets, hname, LAT)
    vs = cell_values(cell, sets, hname, LAT_SLOW)
    per = period_of(v["e"])
    pers = period_of(vs["e"])
    out = {}
    for pi, pn in enumerate(PERIOD_NAMES):
        m = per == pi
        net, gross, e = v["net"][m], v["gross"][m], v["e"][m]
        g = clusters(cell, hname, e)
        mean, p, G = cluster_p(net, g)
        gm, gp, _ = cluster_p(gross, g)
        days = LIQ_DAYS_PER_PERIOD[pi] if cell.family == "F6_REAL_LIQ" else PERIOD_DAYS[pi]
        sym_pl = np.bincount(v["s"][m], weights=net, minlength=1)
        top5 = float(np.sort(sym_pl)[-5:].sum() / sym_pl.sum()) if len(net) and sym_pl.sum() > 0 else None
        out[pn] = {
            "n": int(len(net)),
            "clusters": G,
            "per_day": round(len(net) / days, 2),
            "gross": gm,
            "p_gross": gp,
            "net": mean,
            "p": p,
            "net_stress": float((net - (COST_STRESS - COST)).mean()) if len(net) else 0.0,
            "fund": float(v["fund"][m].mean()) if len(net) else 0.0,
            "hit": float((net > 0).mean()) if len(net) else 0.0,
            "mfe": float(np.nanmean(v["mfe"][m])) if len(net) else 0.0,
            "mae": float(np.nanmean(v["mae"][m])) if len(net) else 0.0,
            "mdd": max_dd(net, e),
            "top5_share": top5,
            "net_lat15": float(vs["net"][pers == pi].mean()) if (pers == pi).any() else 0.0,
        }
    # pooled views used by the verdict
    for name, ids in (("TV", (0, 1)), ("OOS", (2, 3))):
        m = np.isin(per, ids)
        g = clusters(cell, hname, v["e"][m])
        mean, p, G = cluster_p(v["net"][m], g)
        ms = np.isin(pers, ids)
        out[name] = {
            "n": int(m.sum()),
            "net": mean,
            "p": p,
            "net_stress": float((v["net"][m] - (COST_STRESS - COST)).mean()) if m.any() else 0.0,
            "net_lat15": float(vs["net"][ms].mean()) if ms.any() else 0.0,
            "top5_share": None,
        }
        sym_pl = np.bincount(v["s"][m], weights=v["net"][m], minlength=1)
        if m.any() and sym_pl.sum() > 0:
            out[name]["top5_share"] = float(np.sort(sym_pl)[-5:].sum() / sym_pl.sum())
    return out


def verdict(rows: list) -> dict:
    p_train = np.array([r["TRAIN"]["p"] if (r["TRAIN"]["n"] >= 30 and r["TRAIN"]["clusters"] >= 20) else 1.0 for r in rows])
    q = bh(p_train)
    bonf = np.minimum(p_train * len(rows), 1.0)
    p_train_gross = np.array([r["TRAIN"]["p_gross"] if r["TRAIN"]["n"] >= 30 else 1.0 for r in rows])
    q_gross = bh(p_train_gross)
    for r, qi, bi, qg in zip(rows, q, bonf, q_gross):
        r["q_train"], r["bonf_train"], r["q_train_gross"] = float(qi), float(bi), float(qg)
        r["S1"] = bool(qi < 0.10)
        r["S2"] = r["S1"] and r["VALID"]["net"] > 0 and r["VALID"]["p"] < 0.05
    selected = [r["id"] for r in rows if r["S2"]]
    digest = hashlib.sha256(json.dumps(sorted(selected)).encode()).hexdigest()
    SEL.write_text(json.dumps({"selected": selected, "sha256": digest, "frozen_at": datetime.now(UTC).isoformat()}, indent=1))
    for r in rows:
        days = sum(PERIOD_DAYS)
        n_all = sum(r[p]["n"] for p in PERIOD_NAMES)
        per_day = n_all / (sum(LIQ_DAYS_PER_PERIOD) if r["family"] == "F6_REAL_LIQ" else days)
        r["S3"] = r["S2"] and r["TEST"]["net"] > 0 and r["HOLDOUT"]["net"] > 0 and r["OOS"]["p"] < 0.05
        r["S4"] = r["S3"] and all(r[p]["net"] > 0 for p in PERIOD_NAMES)
        top5 = r["OOS"]["top5_share"]
        r["S5"] = r["S4"] and per_day >= 1 and top5 is not None and top5 <= 0.5
        r["S6"] = r["S5"] and r["TV"]["net_lat15"] > 0
        r["S7"] = r["S6"] and r["OOS"]["net_stress"] > 0
        r["SURVIVING_EDGE"] = r["S7"]
    return {"selected": selected, "sha256": digest}


def main() -> None:
    t0 = time.time()
    syms = D.symbols()
    C = np.asarray(D.load("c"), dtype="f4")
    Hh = np.asarray(D.load("h"), dtype="f4")
    Ll = np.asarray(D.load("l"), dtype="f4")
    QV = np.asarray(D.load("qv"), dtype="f4")
    OIV = np.asarray(D.load("oiv"), dtype="f4")
    CF = D.load("cf")
    first = np.argmax(np.isfinite(C), axis=1)
    age_ok = (np.arange(D.N)[None, :] - first[:, None]) >= AGE_MIN
    age_ok &= np.isfinite(C)
    btc = syms.index("BTCUSDT")
    sets: list[TradeSet] = []
    cells: list[Cell] = []
    meta: dict = {}
    for name, fn in (
        ("F1", lambda: build_f1(C, age_ok, sets, cells)),
        ("F2", lambda: build_f2(C, QV, OIV, age_ok, sets, cells)),
        ("F3", lambda: build_f3(C, OIV, age_ok, sets, cells, meta)),
        ("F4", lambda: build_f4(C, Hh, Ll, QV, age_ok, btc, sets, cells)),
        ("F5", lambda: build_f5(C, OIV, age_ok, sets, cells)),
        ("F6", lambda: build_f6(QV, syms, sets, cells, meta)),
    ):
        fn()
        print(f"{name} built: {len(sets)} sets, {len(cells)} cells, {time.time() - t0:.0f}s", flush=True)
    meta["set_sizes"] = [{"family": s.family, "params": s.params, "n": int(len(s.k))} for s in sets]
    rows = []
    for hname in H_BARS:
        evaluate_horizon(sets, hname, C, Hh, Ll, CF)
        for ci, cell in enumerate(cells):
            if hname in cell.horizons:
                r = {"id": f"{ci}:{hname}", "family": cell.family, "params": cell.params, "side": cell.side_label, "h": hname}
                r.update(stats_for(cell, sets, hname))
                rows.append(r)
        print(f"horizon {hname} done, {len(rows)} rows, {time.time() - t0:.0f}s", flush=True)
    for ts in sets:
        ts.per_h = {}
    sel = verdict(rows)
    summary = {}
    for fam in sorted({r["family"] for r in rows}):
        fr = [r for r in rows if r["family"] == fam]
        summary[fam] = {
            "cells": len(fr),
            "train_net_pos": sum(r["TRAIN"]["net"] > 0 for r in fr),
            "train_p05": sum(r["TRAIN"]["p"] < 0.05 for r in fr),
            "S1_bh": sum(r["S1"] for r in fr),
            "S2_valid": sum(r["S2"] for r in fr),
            "S3_oos": sum(r["S3"] for r in fr),
            "S4_all_periods": sum(r["S4"] for r in fr),
            "S7_surviving": sum(r["SURVIVING_EDGE"] for r in fr),
            "gross_bh": sum(r["q_train_gross"] < 0.10 for r in fr),
            "net_pos_all4": sum(all(r[p]["net"] > 0 for p in PERIOD_NAMES) for r in fr),
            "gross_pos_all4": sum(all(r[p]["gross"] > 0 for p in PERIOD_NAMES) for r in fr),
        }
    OUT.write_text(json.dumps({"summary": summary, "selection": sel, "meta": meta, "period_days": PERIOD_DAYS, "rows": rows}, default=float))
    print(json.dumps(summary, indent=1))
    print(f"done {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
