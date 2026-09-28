"""Statistics for entry research - pure Python.

Trades that open in the same 4 h block share the market move, so they are not
independent: every p-value and CI here is clustered on 4 h blocks
(cluster-robust t with G-1 df, and a cluster bootstrap for CIs).
"""
from __future__ import annotations

import math
import random
import statistics as st
from datetime import datetime

BLOCK_HOURS = 4


def block_of(t: datetime) -> str:
    return f"{t.date().isoformat()}#{t.hour // BLOCK_HOURS}"


# ---------------------------------------------------------------- Student t

def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(c) > 1e-30 else 1e-30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(c) > 1e-30 else 1e-30
        de = d * c
        h *= de
        if abs(de - 1) < 1e-12:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: float) -> float:
    """P(T > t) for Student t."""
    x = df / (df + t * t)
    tail = 0.5 * _betai(df / 2, 0.5, x)
    return tail if t > 0 else 1 - tail


# ---------------------------------------------------------------- clustered tests

def cluster_t(values: list[float], clusters: list[str], mu0: float = 0.0) -> tuple[float, int]:
    """(t, G) for H0: mean == mu0 with cluster-robust SE."""
    n = len(values)
    if n < 2:
        return 0.0, 0
    m = sum(values) / n
    sums: dict[str, float] = {}
    for v, g in zip(values, clusters):
        sums[g] = sums.get(g, 0.0) + (v - m)
    G = len(sums)
    if G < 2:
        return 0.0, G
    var = sum(s * s for s in sums.values()) / (n * n) * G / (G - 1)
    if var <= 0:
        return 0.0, G
    return (m - mu0) / math.sqrt(var), G


def p_mean_positive(values: list[float], clusters: list[str]) -> float:
    t, G = cluster_t(values, clusters)
    return 1.0 if G < 2 else t_sf(t, G - 1)


def p_mean_negative(values: list[float], clusters: list[str]) -> float:
    t, G = cluster_t(values, clusters)
    return 1.0 if G < 2 else t_sf(-t, G - 1)


def p_diff_negative(group: list[float], gcl: list[str], rest: list[float], rcl: list[str]) -> float:
    """One-sided H1: mean(group) < mean(rest), Welch-style with clustered SEs."""
    if len(group) < 2 or len(rest) < 2:
        return 1.0
    tg, Gg = cluster_t(group, gcl)
    tr, Gr = cluster_t(rest, rcl)
    mg, mr = st.mean(group), st.mean(rest)
    se_g = abs(mg / tg) if tg else _plain_se(group)
    se_r = abs(mr / tr) if tr else _plain_se(rest)
    se = math.sqrt(se_g ** 2 + se_r ** 2)
    if se == 0 or min(Gg, Gr) < 2:
        return 1.0
    return t_sf(-(mg - mr) / se, min(Gg, Gr) - 1)


def _plain_se(xs: list[float]) -> float:
    return st.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0.0


def bh(pvalues: list[float]) -> list[float]:
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvalues[i] * m / rank)
        q[i] = prev
    return q


def cluster_bootstrap_ci(values: list[float], clusters: list[str], n: int = 2000,
                         seed: int = 11) -> tuple[float, float] | None:
    groups: dict[str, list[float]] = {}
    for v, g in zip(values, clusters):
        groups.setdefault(g, []).append(v)
    keys = list(groups)
    if len(keys) < 3:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pick = [groups[k] for k in rng.choices(keys, k=len(keys))]
        flat = [v for g in pick for v in g]
        means.append(sum(flat) / len(flat))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


# ---------------------------------------------------------------- trade metrics

def max_drawdown_r(values_in_time_order: list[float]) -> float:
    peak = cum = dd = 0.0
    for v in values_in_time_order:
        cum += v
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def profit_factor(values: list[float]) -> float | None:
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if losses == 0:
        return None if gains == 0 else math.inf
    return gains / losses


def mean_without_top(values: list[float], share: float = 0.05) -> float | None:
    """Mean after removing the best max(1, 5 %) trades - is the result carried
    by a handful of outliers?"""
    if len(values) < 3:
        return None
    k = max(1, int(len(values) * share))
    return st.mean(sorted(values)[:-k])


def summarize(records: list[dict]) -> dict:
    """records: dicts with r, t0, mfe_pct, mae_pct, minutes_to_mfe, risk_pct."""
    recs = sorted(records, key=lambda r: r["t0"])
    rs = [r["r"] for r in recs]
    n = len(rs)
    if n == 0:
        return {"n": 0}
    cl = [block_of(r["t0"]) for r in recs]
    ci = cluster_bootstrap_ci(rs, cl) if n >= 5 else None
    return {
        "n": n, "blocks": len(set(cl)),
        "win_rate": sum(1 for v in rs if v > 0) / n,
        "mean_r": st.mean(rs), "median_r": st.median(rs),
        "pf": profit_factor(rs), "max_dd_r": max_drawdown_r(rs),
        "sum_r": sum(rs),
        "ci": ci, "p_pos": p_mean_positive(rs, cl), "p_neg": p_mean_negative(rs, cl),
        "mean_r_wo_top5pct": mean_without_top(rs),
        "mfe_pct": st.mean(r["mfe_pct"] for r in recs),
        "mae_pct": st.mean(r["mae_pct"] for r in recs),
        "min_to_mfe": st.median(r["minutes_to_mfe"] for r in recs),
        # round-trip fee expressed in R (cost drag per trade, before slippage)
        "cost_r": (st.mean(0.10 / r["risk_pct"] for r in recs if r.get("risk_pct"))
                   if any(r.get("risk_pct") for r in recs) else None),
    }
