"""High-Frequency Opportunity Engine V2 - event research (2026-09-29).

    python -m crypto_trading.entry_research.event_engine

Read-only research on cached history. Nothing in the bot imports this module
(enforced by tests/crypto_trading/entry_research/test_event_engine.py); it can
never open, size or veto a LIVE trade.

Why: today's candidate pool has negative OOS expectancy (TEST -0.49 R), so a
better RANKING of it cannot help. This module generates candidates a
different way - point-in-time EVENTS on a 5-minute grid over the whole
cached universe - and measures every event type on its own.

Point-in-time contract
- 5m bars are built from 1m klines; bar k covers [start_k, start_k + 5 min)
  and is only used at T >= start_k + 5 min. Every feature at T reads bars
  <= k only (prefix sums / sparse tables over indices <= k).
- Open interest (Binance 5m history, a cross-venue PROXY - BingX has no OI
  history) is used at T only if stamped <= T - 5 min. Funding (BingX settled
  rates) only if settled <= T.
- An EVENT is a condition that is true at T and was false at T - 5 min
  (both points known). Nothing is tuned: every threshold below is fixed
  before looking at any outcome.

Outcome (identical barrier to all earlier research): stop 2 x ATR15, target
3 x ATR15, 6 h, 0.10 % fees + 0.15 % stop slippage, R after costs.
Primary entry = first 1m open >= T + 5 min (a deterministic engine);
sensitivity = T + 23 min (today's AI path).

Protocol: TRAIN < 13/9, VALID 13/9-26/9, TEST >= 26/9 (purged); one event per
symbol/type/side per 60 min; 4 h-block clustered p-values, cluster bootstrap
CIs, Benjamini-Hochberg over every hypothesis; classes EDGE / WEAK_EDGE /
HYPOTHESIS / REGIME_DEPENDENT / DECAYING_EDGE / FAILURE / FAILURE_HYPOTHESIS /
NOISE / INSUFFICIENT_DATA. The combined engine may only use TRAIN + VALID to
choose its members, ranking and vetoes; TEST is touched once, at the end.
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
import statistics as st
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research import edge_lab as el
from crypto_trading.entry_research.dataset import TEST_FROM, VALID_FROM, period_of
from crypto_trading.entry_research.stats import (
    bh,
    block_of,
    cluster_bootstrap_ci,
    p_diff_negative,
    p_mean_positive,
)

OUT = Path("data/entry_research")
BAR_S = 300
HIST = 320  # 5m bars of clean history needed (~26.7 h)
LAT_ENGINE_S = 5 * 60
LAT_AI_S = el.LATENCY_S
THIN_MIN = 60
MIN_N = 30
EQUITY_REF = 389.0  # LIVE equity at the time of the study
NOTIONAL = 1000.0  # 100 USDT margin x 10
KERNEL_COST_PCT = 0.7  # entry buffer 0.3 + stop slippage 0.3 + fees 0.1 (safety kernel)
REF_SYMBOL = "BTC-USDT"


# ------------------------------------------------------------------ 5m bars


@dataclass
class Series5:
    start: int  # start of bar 0
    o: list[float | None]
    h: list[float | None]
    l: list[float | None]  # noqa: E741
    c: list[float | None]
    v: list[float | None]

    def close_time(self, k: int) -> int:
        return self.start + BAR_S * (k + 1)

    def index_closing_at(self, T: int) -> int:
        return (T - self.start) // BAR_S - 1


def bars5(m: el.Minute) -> Series5:
    """Dense 5m bars; a bar with fewer than 4 of its 5 minutes is None."""
    groups: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(m.ts):
        groups[t - t % BAR_S].append(i)
    first, last = min(groups), max(groups)
    n = (last - first) // BAR_S + 1
    o: list = [None] * n
    h: list = [None] * n
    l: list = [None] * n  # noqa: E741
    c: list = [None] * n
    v: list = [None] * n
    for b, idx in groups.items():
        if len(idx) < 4:
            continue
        k = (b - first) // BAR_S
        o[k], c[k] = m.o[idx[0]], m.c[idx[-1]]
        h[k], l[k] = max(m.h[i] for i in idx), min(m.l[i] for i in idx)
        v[k] = sum(m.v[i] for i in idx) * 5 / len(idx)
    return Series5(first, o, h, l, c, v)


class _Sparse:
    """O(1) range max/min over [a, b]; a query reads only indices a..b."""

    def __init__(self, xs: list[float], fn):
        self.fn = fn
        self.t = [xs[:]]
        j = 1
        while (1 << j) <= len(xs):
            prev, half = self.t[-1], 1 << (j - 1)
            self.t.append([fn(prev[i], prev[i + half]) for i in range(len(xs) - (1 << j) + 1)])
            j += 1

    def q(self, a: int, b: int) -> float:
        j = (b - a + 1).bit_length() - 1
        return self.fn(self.t[j][a], self.t[j][b - (1 << j) + 1])


class Prepared:
    """Prefix sums + sparse tables. Entry k of every prefix depends only on
    bars <= k, so any window query ending at k is point-in-time."""

    def __init__(self, s: Series5):
        self.s = s
        n = len(s.c)
        self.M = [0] * (n + 1)
        self.LR = [0.0] * (n + 1)
        self.LR2 = [0.0] * (n + 1)
        self.V = [0.0] * (n + 1)
        self.V2 = [0.0] * (n + 1)
        self.RNG = [0.0] * (n + 1)
        self.C = [0.0] * (n + 1)
        self.lr = [0.0] * n
        for k in range(n):
            ok = s.c[k] is not None
            self.M[k + 1] = self.M[k] + (0 if ok else 1)
            lr = math.log(s.c[k] / s.c[k - 1]) if ok and k and s.c[k - 1] is not None else 0.0
            self.lr[k] = lr
            self.LR[k + 1] = self.LR[k] + lr
            self.LR2[k + 1] = self.LR2[k] + lr * lr
            vv = s.v[k] if ok else 0.0
            self.V[k + 1] = self.V[k] + vv
            self.V2[k + 1] = self.V2[k] + vv * vv
            self.RNG[k + 1] = self.RNG[k] + ((s.h[k] - s.l[k]) / s.c[k] if ok else 0.0)
            self.C[k + 1] = self.C[k] + (s.c[k] if ok else 0.0)
        self.hmax = _Sparse([x if x is not None else -math.inf for x in s.h], max)
        self.lmin = _Sparse([x if x is not None else math.inf for x in s.l], min)
        ratio = [math.inf] * n
        for k in range(288, n):
            if self.clean(k - 288, k):
                atr_l = self.wsum(self.RNG, k - 287, k) / 288
                if atr_l > 0:
                    ratio[k] = (self.wsum(self.RNG, k - 11, k) / 12) / atr_l
        self.ratio = ratio
        self.rmin = _Sparse(ratio, min)

    @staticmethod
    def wsum(P: list[float], a: int, b: int) -> float:
        return P[b + 1] - P[a]

    def clean(self, a: int, b: int) -> bool:
        return a >= 0 and self.M[b + 1] - self.M[a] == 0


def features_at(p: Prepared, k: int) -> dict | None:
    """Features of the 5m view at T = close of bar k. Reads bars <= k only."""
    if k < HIST or k >= len(p.s.c) or not p.clean(k - HIST, k):
        return None
    s = p.s
    c = s.c
    n288 = 288
    mean_lr = p.wsum(p.LR, k - 287, k) / n288
    var = max(p.wsum(p.LR2, k - 287, k) / n288 - mean_lr * mean_lr, 1e-14)
    sig = math.sqrt(var)
    vm = p.wsum(p.V, k - 288, k - 1) / n288
    vsd = math.sqrt(max(p.wsum(p.V2, k - 288, k - 1) / n288 - vm * vm, 0.0))
    atr_l = p.wsum(p.RNG, k - 287, k) / n288
    cp_recent = p.wsum(p.LR, k - 5, k) / 6
    cp_before = p.wsum(p.LR, k - 29, k - 6) / 24

    def ret(n: int) -> float:
        return (c[k] / c[k - n] - 1) * 100

    return {
        "o": s.o[k],
        "h": s.h[k],
        "l": s.l[k],
        "c": c[k],
        "ret1": ret(1),
        "ret3": ret(3),
        "ret6": ret(6),
        "ret12": ret(12),
        "ret48": ret(48),
        "ret288": ret(288),
        "z1": p.lr[k] / sig,
        "z3": math.log(c[k] / c[k - 3]) / (sig * math.sqrt(3)),
        "z12": math.log(c[k] / c[k - 12]) / (sig * math.sqrt(12)),
        "hh48": p.hmax.q(k - 48, k - 1),
        "ll48": p.lmin.q(k - 48, k - 1),
        "hh288": p.hmax.q(k - 288, k - 1),
        "ll288": p.lmin.q(k - 288, k - 1),
        "vz": (s.v[k] - vm) / vsd if vsd > 0 else 0.0,
        "ratio": p.ratio[k],
        "ratio_min24": p.rmin.q(k - 24, k - 1),
        "sma60": p.wsum(p.C, k - 59, k) / 60,
        "sma150": p.wsum(p.C, k - 149, k) / 150,
        "cp": (cp_recent - cp_before) / (sig * math.sqrt(1 / 6 + 1 / 24)),
        "atr_l": atr_l * 100,
    }


# ------------------------------------------------------------------ derivatives (point-in-time)


class Derivs:
    def __init__(
        self,
        funding: dict[str, tuple[list[int], list[float]]] | None = None,
        oi: dict[str, tuple[list[int], list[float]]] | None = None,
    ):
        self.funding = funding or {}
        self.oi = oi or {}

    @classmethod
    def load(cls, path: str) -> Derivs:
        if not Path(path).exists():
            return cls()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        f: dict = defaultdict(lambda: ([], []))
        for sym, t, r in conn.execute("SELECT symbol, t, rate FROM funding ORDER BY symbol, t"):
            f[sym][0].append(t)
            f[sym][1].append(r)
        o: dict = defaultdict(lambda: ([], []))
        for sym, t, v in conn.execute("SELECT symbol, t, oi_value FROM oi ORDER BY symbol, t"):
            o[sym][0].append(t)
            o[sym][1].append(v)
        return cls(dict(f), dict(o))

    def funding_at(self, sym: str, T: int) -> float | None:
        ts, vs = self.funding.get(sym, ([], []))
        i = bisect.bisect_right(ts, T) - 1  # settled at or before T
        return vs[i] if i >= 0 and T - ts[i] <= 9 * 3600 else None

    def oi_at(self, sym: str, T: int) -> float | None:
        ts, vs = self.oi.get(sym, ([], []))
        cutoff = T - BAR_S  # one extra period of lag
        i = bisect.bisect_right(ts, cutoff) - 1
        return vs[i] if i >= 0 and cutoff - ts[i] <= 600 else None

    def oi_change(self, sym: str, T: int, seconds: int) -> float | None:
        a, b = self.oi_at(sym, T), self.oi_at(sym, T - seconds)
        return a / b - 1 if a and b else None


# ------------------------------------------------------------------ events


def _brk_up(f, n):
    return f["c"] > f[f"hh{n}"]


def _brk_dn(f, n):
    return f["c"] < f[f"ll{n}"]


def _g(f, key):
    x = f.get(key)
    if x is None:
        raise KeyError(key)
    return x


# name -> (LONG condition, SHORT condition). Fixed before any outcome was seen.
CONDITIONS = {
    "BRK_4H": (lambda f: _brk_up(f, 48), lambda f: _brk_dn(f, 48)),
    "BRK_24H": (lambda f: _brk_up(f, 288), lambda f: _brk_dn(f, 288)),
    "EARLY_BRK": (
        lambda f: _brk_up(f, 48) and f["ret48"] < 2 and f["ret288"] < 5,
        lambda f: _brk_dn(f, 48) and f["ret48"] > -2 and f["ret288"] > -5,
    ),
    "SQUEEZE_EXP": (
        lambda f: f["ratio"] > 1.3 and f["ratio_min24"] < 0.75 and f["ret6"] > 0,
        lambda f: f["ratio"] > 1.3 and f["ratio_min24"] < 0.75 and f["ret6"] < 0,
    ),
    "ACCEL": (lambda f: f["z3"] > 3, lambda f: f["z3"] < -3),
    "CHANGE_POINT": (lambda f: f["cp"] > 3, lambda f: f["cp"] < -3),
    "VOL_PRICE": (lambda f: f["vz"] > 3 and f["z1"] > 2, lambda f: f["vz"] > 3 and f["z1"] < -2),
    "TREND_TRANS": (lambda f: f["sma60"] > f["sma150"], lambda f: f["sma60"] < f["sma150"]),
    "PULLBACK_CONT": (
        lambda f: (
            f["sma60"] > f["sma150"]
            and f["ret48"] > 0
            and f["l"] <= f["sma60"] < f["c"]
            and f["c"] > f["o"]
        ),
        lambda f: (
            f["sma60"] < f["sma150"]
            and f["ret48"] < 0
            and f["h"] >= f["sma60"] > f["c"]
            and f["c"] < f["o"]
        ),
    ),
    "SWEEP_RECLAIM": (
        lambda f: f["l"] < f["ll288"] < f["c"],
        lambda f: f["h"] > f["hh288"] > f["c"],
    ),
    "CAPITULATION_REV": (
        lambda f: f["z12"] < -3 and f["c"] > f["o"] and f["vz"] > 2,
        lambda f: f["z12"] > 3 and f["c"] < f["o"] and f["vz"] > 2,
    ),
    "MTF_BRK": (
        lambda f: _brk_up(f, 48) and f["sma60"] > f["sma150"] and f["ret288"] > 0,
        lambda f: _brk_dn(f, 48) and f["sma60"] < f["sma150"] and f["ret288"] < 0,
    ),
    "FUNDING_SQUEEZE": (
        lambda f: _g(f, "fund") <= -0.0003 and _brk_up(f, 48),
        lambda f: _g(f, "fund") >= 0.0005 and _brk_dn(f, 48),
    ),
    "OI_PRICE": (
        lambda f: _g(f, "oi1h") > 0.03 and f["z12"] > 1.5,
        lambda f: _g(f, "oi1h") > 0.03 and f["z12"] < -1.5,
    ),
    "OI_COVER": (
        lambda f: _g(f, "oi1h") < -0.03 and f["z12"] > 1.5,
        lambda f: _g(f, "oi1h") < -0.03 and f["z12"] < -1.5,
    ),
    "OI_BUILD_BRK": (
        lambda f: _g(f, "oi4h") > 0.05 and _brk_up(f, 48),
        lambda f: _g(f, "oi4h") > 0.05 and _brk_dn(f, 48),
    ),
}
CROSS_SECTIONAL = ("RS_LEADER",)  # rank of market-relative 1 h return; see cross_section()
RS_TOP, RS_MAX_RET48 = 0.97, 3.0
ALL_TYPES = tuple(CONDITIONS) + CROSS_SECTIONAL


def _holds(cond, f) -> bool:
    try:
        return bool(cond(f))
    except KeyError:
        return False  # the data needed is not known at T - never an event


def events_at(f: dict, fp: dict | None) -> list[tuple[str, str]]:
    """(type, side) for every condition true now and false 5 min earlier."""
    if fp is None:
        return []
    out = []
    for name, (lc, sc) in CONDITIONS.items():
        for side, cond in (("LONG", lc), ("SHORT", sc)):
            if _holds(cond, f) and not _holds(cond, fp):
                out.append((name, side))
    return out


def with_derivs(f: dict, sym: str, T: int, d: Derivs) -> dict:
    f = dict(f)
    f["fund"] = d.funding_at(sym, T)
    f["oi1h"] = d.oi_change(sym, T, 3600)
    f["oi4h"] = d.oi_change(sym, T, 4 * 3600)
    return f


SNAP = (
    "ret12",
    "ret48",
    "ret288",
    "vz",
    "z3",
    "z12",
    "ratio",
    "atr_l",
    "cp",
    "fund",
    "oi1h",
    "oi4h",
)


def scan_symbol(sym: str, m: el.Minute, d: Derivs) -> tuple[list[dict], list[tuple], list[dict]]:
    """(events, cross-section points (T, ret12, ret48, atr_l), hourly baseline points)."""
    s = bars5(m)
    p = Prepared(s)
    events, xs, base = [], [], []
    prev = None
    for k in range(HIST, len(s.c)):
        f = features_at(p, k)
        T = s.close_time(k)
        if f is None:
            prev = None
            continue
        f = with_derivs(f, sym, T, d)
        xs.append((T, f["ret12"], f["ret48"], f["atr_l"]))
        for name, side in events_at(f, prev):
            events.append(
                {
                    "symbol": sym,
                    "T": T,
                    "type": name,
                    "side": side,
                    "f": {key: f.get(key) for key in SNAP},
                }
            )
        if T % 3600 == 0:
            for side in ("LONG", "SHORT"):
                base.append(
                    {
                        "symbol": sym,
                        "T": T,
                        "type": "BASELINE",
                        "side": side,
                        "f": {key: f.get(key) for key in SNAP},
                    }
                )
        prev = f
    return events, xs, base


def cross_section(xs_by_T: dict[int, list[tuple]]) -> tuple[list[dict], dict[int, dict]]:
    """RS_LEADER events + per-T market regime. Only same-T data of other
    symbols is used (all known at T)."""
    events, regime = [], {}
    last: dict[str, tuple[int, float]] = {}
    for T in sorted(xs_by_T):
        pts = xs_by_T[T]
        if len(pts) < 20:
            continue
        med12 = st.median(x[1] for x in pts)
        ranked = sorted(pts, key=lambda x: x[1])
        n = len(ranked)
        regime[T] = {
            "breadth": sum(x[2] > 0 for x in pts) / n,
            "mvol": st.median(x[3] for x in pts),
            "ref_ret48": next(
                (x[2] for x in pts if x[0] == REF_SYMBOL), st.median(x[2] for x in pts)
            ),
        }
        for i, (sym, r12, r48, _a) in enumerate(ranked):
            pct = i / (n - 1)
            prev = last.get(sym)
            fresh = prev is not None and prev[0] == T - BAR_S
            if fresh and pct >= RS_TOP > prev[1] and r48 < RS_MAX_RET48:
                events.append(
                    {
                        "symbol": sym,
                        "T": T,
                        "type": "RS_LEADER",
                        "side": "LONG",
                        "f": {"ret12": r12, "ret48": r48, "rel12": r12 - med12},
                    }
                )
            if fresh and pct <= 1 - RS_TOP < prev[1] and r48 > -RS_MAX_RET48:
                events.append(
                    {
                        "symbol": sym,
                        "T": T,
                        "type": "RS_LEADER",
                        "side": "SHORT",
                        "f": {"ret12": r12, "ret48": r48, "rel12": r12 - med12},
                    }
                )
            last[sym] = (T, pct)
    return events, regime


# ------------------------------------------------------------------ outcomes


def kernel_feasible(atr15_pct: float, group_cap: float, equity: float = EQUITY_REF) -> bool:
    """Would the fixed 1000-notional order pass the kernel's group cap ALONE?
    worst case = stop distance (2 x ATR15) + kernel costs, on 1000 notional."""
    worst_usdt = (el.STOP_ATR * atr15_pct + KERNEL_COST_PCT) / 100 * NOTIONAL
    return worst_usdt <= group_cap * equity


def attach_outcomes(rows: list[dict], minutes: dict[str, el.Minute]) -> list[dict]:
    atr_cache: dict[tuple, float | None] = {}
    out_cache: dict[tuple, dict | None] = {}
    kept = []
    for r in rows:
        m = minutes[r["symbol"]]
        key = (r["symbol"], r["T"])
        if key not in atr_cache:
            atr_cache[key] = el.atr15_pct(m, r["T"])
        a = atr_cache[key]
        if not a:
            continue
        res = {}
        for lat in (LAT_ENGINE_S, LAT_AI_S):
            ok = (r["symbol"], r["T"], r["side"], lat)
            if ok not in out_cache:
                out_cache[ok] = el.outcome(m, r["T"], lat, a, r["side"])
            res[lat] = out_cache[ok]
        if res[LAT_ENGINE_S] is None:
            continue
        o = res[LAT_ENGINE_S]
        r.update(
            {
                "atr15": a,
                "r": o["r"],
                "reason": o["reason"],
                "mfe": o["mfe_pct"],
                "mae": o["mae_pct"],
                "entry_ts": o["entry_ts"],
                "exit_ts": o["exit_ts"],
                "r_ai": res[LAT_AI_S]["r"] if res[LAT_AI_S] else None,
                "feas5": kernel_feasible(a, 0.05),
                "feas10": kernel_feasible(a, 0.10),
            }
        )
        r.pop("f", None)  # the snapshot is not evaluated; keeps ~300k rows in memory
        kept.append(r)
    return kept


def thin(rows: list[dict], minutes_apart: int = THIN_MIN) -> list[dict]:
    last: dict[tuple, int] = {}
    out = []
    for r in sorted(rows, key=lambda x: x["T"]):
        key = (r["symbol"], r["type"], r["side"])
        if r["T"] - last.get(key, -(10**12)) >= minutes_apart * 60:
            out.append(r)
            last[key] = r["T"]
    return out


# ------------------------------------------------------------------ evaluation


def period(T: int) -> str | None:
    """Purged split: drop rows whose outcome window reaches the next period."""
    t = el.utc(T)
    reach = LAT_AI_S + el.HORIZON_S
    p = period_of(t)
    nxt = VALID_FROM if p == "TRAIN" else TEST_FROM if p == "VALID" else None
    if nxt is not None and T + reach >= nxt.timestamp():
        return None
    return p


def add_regime(rows: list[dict], regime: dict[int, dict], vol_split: float) -> None:
    for r in rows:
        g = regime.get(r["T"])
        r["reg"] = (
            None
            if g is None
            else {
                "btc": "up" if g["ref_ret48"] > 0 else "down",
                "vol": "hi" if g["mvol"] > vol_split else "lo",
                "breadth": "hi" if g["breadth"] > 0.5 else "lo",
            }
        )


def _quartiles(xs: list[float]) -> list[float] | None:
    if len(xs) < 4:
        return None
    return [round(x, 3) for x in st.quantiles(xs, n=4)]


def _stats(rows: list[dict], days: float) -> dict:
    if not rows:
        return {"n": 0, "mean": None}
    rs = [r["r"] for r in rows]
    cl = [block_of(el.utc(r["T"])) for r in rows]
    ci = cluster_bootstrap_ci(rs, cl) if len(rs) >= 10 else None
    ai = [r["r_ai"] for r in rows if r["r_ai"] is not None]
    f5 = [r["r"] for r in rows if r["feas5"]]
    reg = {}
    for dim in ("btc", "vol", "breadth"):
        for val in ("up", "down") if dim == "btc" else ("hi", "lo"):
            sub = [r["r"] for r in rows if r.get("reg") and r["reg"][dim] == val]
            reg[f"{dim}_{val}"] = {"n": len(sub), "mean": round(st.mean(sub), 4) if sub else None}
    return {
        "n": len(rs),
        "per_day": round(len(rs) / days, 2) if days else None,
        "mean": round(st.mean(rs), 4),
        "ci": [round(x, 3) for x in ci] if ci else None,
        "p_pos": round(p_mean_positive(rs, cl), 5),
        "precision": round(sum(x > 0 for x in rs) / len(rs), 3),
        "fp_rate": round(sum(x <= 0 for x in rs) / len(rs), 3),
        "sl_rate": round(sum(r["reason"] == "SL" for r in rows) / len(rows), 3),
        "tp_rate": round(sum(r["reason"] == "TP" for r in rows) / len(rows), 3),
        "mfe": round(st.mean(r["mfe"] for r in rows), 3),
        "mae": round(st.mean(r["mae"] for r in rows), 3),
        "mean_r_ai_latency": round(st.mean(ai), 4) if ai else None,
        "feasible5_share": round(len(f5) / len(rs), 3),
        "mean_feasible5": round(st.mean(f5), 4) if f5 else None,
        "feasible10_share": round(sum(r["feas10"] for r in rows) / len(rs), 3),
        "stop_pct_p25_p50_p75": _quartiles([el.STOP_ATR * r["atr15"] for r in rows]),
        "mean_wo_top5pct": round(st.mean(sorted(rs)[: max(1, int(len(rs) * 0.95))]), 4),
        "regimes": reg,
        "_rs": rs,
        "_cl": cl,
    }


def classify(
    tr: dict, va: dict, te: dict, base: dict, test_q: float | None, fail_q: float | None
) -> str:
    if min(tr["n"], va["n"], te["n"]) < MIN_N:
        return "INSUFFICIENT_DATA"
    pos_all = all(s["mean"] > 0 for s in (tr, va, te))
    if pos_all:
        both = all(
            (te["regimes"][k]["mean"] or 0) > 0 or te["regimes"][k]["n"] < 20
            for k in ("btc_up", "btc_down")
        )
        if (
            te["n"] >= 100
            and test_q is not None
            and test_q < 0.05
            and te["ci"]
            and te["ci"][0] > 0
            and te["mean_wo_top5pct"] > 0
            and both
        ):
            return "EDGE"
        return "WEAK_EDGE" if test_q is not None and test_q < 0.20 else "HYPOTHESIS"
    below = all(
        s["mean"] < base[p]["mean"]
        for p, s in zip(("TRAIN", "VALID", "TEST"), (tr, va, te), strict=True)
    )
    if below and te["mean"] < 0:
        return "FAILURE" if fail_q is not None and fail_q < 0.05 else "FAILURE_HYPOTHESIS"
    for k in tr["regimes"]:
        subs = [s["regimes"][k] for s in (tr, va, te)]
        if all(x["n"] >= 20 and (x["mean"] or 0) > 0 for x in subs):
            return "REGIME_DEPENDENT"
    if tr["p_pos"] < 0.05 and te["mean"] <= 0:
        return "DECAYING_EDGE"
    return "NOISE"


def evaluate(rows: list[dict], days: dict[str, float]) -> dict:
    groups: dict[tuple, dict[str, list]] = defaultdict(
        lambda: {"TRAIN": [], "VALID": [], "TEST": []}
    )
    for r in rows:
        p = r.get("period")
        if p:
            groups[(r["type"], r["side"])][p].append(r)
    stats = {k: {p: _stats(v[p], days[p]) for p in v} for k, v in groups.items()}
    base = {side: stats.get(("BASELINE", side)) for side in ("LONG", "SHORT")}
    hyps = [k for k in stats if k[0] in ALL_TYPES]
    p_test = [stats[k]["TEST"]["p_pos"] if stats[k]["TEST"]["n"] >= 10 else 1.0 for k in hyps]
    p_fail = []
    for k in hyps:
        te, b = stats[k]["TEST"], base[k[1]]["TEST"] if base[k[1]] else None
        p_fail.append(
            p_diff_negative(te["_rs"], te["_cl"], b["_rs"], b["_cl"])
            if te["n"] >= 10 and b
            else 1.0
        )
    q_test, q_fail = bh(p_test), bh(p_fail)
    result = {}
    for k, qt, qf in zip(hyps, q_test, q_fail, strict=True):
        s = stats[k]
        b = base[k[1]]
        cls = classify(s["TRAIN"], s["VALID"], s["TEST"], b, qt, qf) if b else "INSUFFICIENT_DATA"
        result[f"{k[0]}:{k[1]}"] = {
            "class": cls,
            "test_q": round(qt, 4),
            "fail_q": round(qf, 4),
            **{p: _public(s[p]) for p in ("TRAIN", "VALID", "TEST")},
        }
    for k in stats:
        if k[0] not in ALL_TYPES:
            result[f"{k[0]}:{k[1]}"] = {p: _public(stats[k][p]) for p in ("TRAIN", "VALID", "TEST")}
    return result


def _public(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


# ------------------------------------------------------------------ combined engine V2


def choose_members(ev: dict) -> tuple[dict[str, float], set[str], dict[str, set]]:
    """TRAIN + VALID only. Members: positive in both and above the baseline
    in VALID; quality = VALID mean R. Vetoes: below the baseline and negative
    in both. Per-member no-trade regimes: BTC regime with TRAIN+VALID mean < 0."""
    members, vetoes, no_trade = {}, set(), {}
    for key, s in ev.items():
        if key.split(":")[0] not in ALL_TYPES:
            continue
        tr, va = s["TRAIN"], s["VALID"]
        side = key.split(":")[1]
        bv = ev[f"BASELINE:{side}"]["VALID"]["mean"]
        bt = ev[f"BASELINE:{side}"]["TRAIN"]["mean"]
        if tr["n"] < MIN_N or va["n"] < MIN_N:
            continue
        if tr["mean"] > 0 and va["mean"] > 0 and va["mean"] > bv:
            members[key] = va["mean"]
            bad = set()
            for val in ("up", "down"):
                a, b = tr["regimes"][f"btc_{val}"], va["regimes"][f"btc_{val}"]
                n = a["n"] + b["n"]
                if n >= 40 and ((a["mean"] or 0) * a["n"] + (b["mean"] or 0) * b["n"]) / n < 0:
                    bad.add(val)
            no_trade[key] = bad
        elif tr["mean"] < min(0, bt) and va["mean"] < min(0, bv):
            vetoes.add(key)
    return members, vetoes, no_trade


def combined_stream(
    rows: list[dict],
    members: dict[str, float],
    vetoes: set[str],
    no_trade: dict[str, set],
    per: str,
) -> list[dict]:
    """One opportunity per symbol per 60 min: the best-documented member
    event at that T, unless a veto event of the same symbol+side fired in the
    last 60 min or the member's no-trade regime is active."""
    by_T = defaultdict(list)
    for r in rows:
        if r.get("period") == per:
            by_T[r["T"]].append(r)
    last_pick: dict[str, int] = {}
    last_veto: dict[tuple, int] = {}
    out = []
    for T in sorted(by_T):
        rs = by_T[T]
        for r in rs:
            if f"{r['type']}:{r['side']}" in vetoes:
                last_veto[(r["symbol"], r["side"])] = T
        cands = []
        for r in rs:
            key = f"{r['type']}:{r['side']}"
            if key not in members:
                continue
            if r.get("reg") and r["reg"]["btc"] in no_trade.get(key, set()):
                continue
            if T - last_veto.get((r["symbol"], r["side"]), -(10**12)) <= 3600:
                continue
            if T - last_pick.get(r["symbol"], -(10**12)) < THIN_MIN * 60:
                continue
            cands.append((members[key], r))
        chosen: dict[str, tuple] = {}
        for q, r in cands:
            if r["symbol"] not in chosen or q > chosen[r["symbol"]][0]:
                chosen[r["symbol"]] = (q, r)
        for sym, (q, r) in chosen.items():
            last_pick[sym] = T
            out.append({**r, "quality": q})
    return out


def capacity_sim(
    stream: list[dict],
    group_cap: float,
    total_cap: float = 0.10,
    max_open: int = 4,
    equity: float = EQUITY_REF,
) -> dict:
    """LIVE-like: fixed 1000 notional, APPROVE/REJECT on worst-case risk (all
    alts one group), max 4 open, 1 per symbol, best quality first at equal T."""
    open_: list[dict] = []
    taken = []
    for r in sorted(stream, key=lambda x: (x["T"], -x["quality"])):
        open_ = [o for o in open_ if o["exit_ts"] > r["entry_ts"]]
        if len(open_) >= max_open or any(o["symbol"] == r["symbol"] for o in open_):
            continue
        worst = (el.STOP_ATR * r["atr15"] + KERNEL_COST_PCT) / 100 * NOTIONAL
        used = sum(o["_worst"] for o in open_)
        if used + worst > group_cap * equity or used + worst > total_cap * equity:
            continue
        open_.append({**r, "_worst": worst})
        taken.append({**r, "_worst": worst})
    return taken


def summarize_trades(taken: list[dict], days: float) -> dict:
    if not taken:
        return {"n": 0, "per_day": 0.0}
    rs = [t["r"] for t in taken]
    pnl = [t["r"] * el.STOP_ATR * t["atr15"] / 100 * NOTIONAL for t in taken]
    cl = [block_of(el.utc(t["T"])) for t in taken]
    ci = cluster_bootstrap_ci(rs, cl) if len(rs) >= 10 else None
    return {
        "n": len(rs),
        "per_day": round(len(rs) / days, 2),
        "mean_r": round(st.mean(rs), 4),
        "ci": [round(x, 3) for x in ci] if ci else None,
        "precision": round(sum(x > 0 for x in rs) / len(rs), 3),
        "fp_rate": round(sum(x <= 0 for x in rs) / len(rs), 3),
        "sl_rate": round(sum(t["reason"] == "SL" for t in taken) / len(taken), 3),
        "usdt_total": round(sum(pnl), 2),
        "usdt_per_day": round(sum(pnl) / days, 2),
    }


# ------------------------------------------------------------------ main


def load_symbol(conn: sqlite3.Connection, sym: str) -> el.Minute:
    rows = conn.execute(
        "SELECT t, o, h, l, c, v FROM klines WHERE symbol = ? ORDER BY t", (sym,)
    ).fetchall()
    return el.Minute(
        [r[0] for r in rows],
        [r[1] for r in rows],
        [r[2] for r in rows],
        [r[3] for r in rows],
        [r[4] for r in rows],
        [r[5] or 0.0 for r in rows],
    )


def old_candidate_rows() -> dict[str, list[dict]]:
    """Today's candidate pool (entry-research dataset), LONG, same outcome."""
    out: dict[str, list[dict]] = defaultdict(list)
    for d in json.loads((OUT / "dataset.json").read_text(encoding="utf-8")):
        T = int(datetime.fromisoformat(d["t0"]).timestamp())
        out[d["symbol"]].append(
            {"symbol": d["symbol"], "T": T, "type": "OLD_CANDIDATES", "side": "LONG", "f": {}}
        )
    return {s: thin(v) for s, v in out.items()}


def run() -> dict:
    """One symbol in memory at a time (the 1m history of ~200 symbols does
    not fit comfortably as Python lists)."""
    conn = sqlite3.connect(f"file:{OUT / 'klines.db'}?mode=ro", uri=True)
    symbols = [
        s
        for (s,) in conn.execute("SELECT DISTINCT symbol FROM klines ORDER BY symbol")
        if s.endswith("-USDT")
    ]
    d = Derivs.load(str(OUT / "derivs.db"))
    olds = old_candidate_rows()
    rows: list[dict] = []
    raw_counts: dict[str, int] = defaultdict(int)
    xs_by_T: dict[int, list] = defaultdict(list)
    scanned = 0
    for sym in symbols:
        m = load_symbol(conn, sym)
        if len(m.ts) < HIST * 5:
            continue
        scanned += 1
        e, xs, b = scan_symbol(sym, m, d)
        for x in e:
            raw_counts[f"{x['type']}:{x['side']}"] += 1
        rows += attach_outcomes(thin(e) + b + olds.get(sym, []), {sym: m})
        for T, r12, r48, a in xs:
            xs_by_T[T].append((sym, r12, r48, a))
    rs_events, regime = cross_section(xs_by_T)
    by_sym = defaultdict(list)
    for x in rs_events:
        raw_counts[f"{x['type']}:{x['side']}"] += 1
        by_sym[x["symbol"]].append(x)
    for sym, evs in by_sym.items():
        rows += attach_outcomes(thin(evs), {sym: load_symbol(conn, sym)})
    for r in rows:
        r["period"] = period(r["T"])
    days = {
        p: len({T for T in xs_by_T if period(T) == p}) / 288 for p in ("TRAIN", "VALID", "TEST")
    }
    train_vols = [g["mvol"] for T, g in regime.items() if period(T) == "TRAIN"]
    vol_split = st.median(train_vols) if train_vols else 0.0
    add_regime(rows, regime, vol_split)
    ev = evaluate(rows, days)
    members, vetoes, no_trade = choose_members(ev)
    combined = {}
    for per in ("VALID", "TEST"):
        stream = combined_stream(rows, members, vetoes, no_trade, per)
        combined[per] = {
            "uncapped": summarize_trades(stream, days[per]),
            "live_like_group5": summarize_trades(capacity_sim(stream, 0.05), days[per]),
            "live_like_group10": summarize_trades(capacity_sim(stream, 0.10), days[per]),
            "by_member": {
                k: sum(1 for r in stream if f"{r['type']}:{r['side']}" == k) for k in members
            },
        }
        old = [r for r in rows if r["type"] == "OLD_CANDIDATES" and r["period"] == per]
        for r in old:
            r["quality"] = 0.0
        combined[per]["old_candidates_live_like_group5"] = summarize_trades(
            capacity_sim(old, 0.05), days[per]
        )
        combined[per]["old_candidates_live_like_group10"] = summarize_trades(
            capacity_sim(old, 0.10), days[per]
        )
    derivs_cov = {"funding_symbols": len(d.funding), "oi_symbols": len(d.oi)}
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "symbols": scanned,
        "days": days,
        "vol_split": vol_split,
        "derivs": derivs_cov,
        "raw_event_counts": dict(raw_counts),
        "hypotheses": ev,
        "members": members,
        "vetoes": sorted(vetoes),
        "no_trade_regimes": {k: sorted(v) for k, v in no_trade.items()},
        "combined": combined,
    }
    (OUT / "event_engine.json").write_text(
        json.dumps(result, indent=1, default=str), encoding="utf-8"
    )
    # compact per-row dump for follow-up analyses without a full rerun
    keys = (
        "symbol",
        "T",
        "type",
        "side",
        "period",
        "r",
        "r_ai",
        "reason",
        "mfe",
        "mae",
        "atr15",
        "entry_ts",
        "exit_ts",
        "reg",
    )
    (OUT / "event_engine_rows.json").write_text(
        json.dumps([{k: r.get(k) for k in keys} for r in rows]), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    r = run()
    print(json.dumps({"members": r["members"], "combined_TEST": r["combined"]["TEST"]}, indent=1))
