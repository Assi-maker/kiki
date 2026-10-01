"""P3 analysis: baseline, LONG vs SHORT, incremental edge of new information.

PRE-REGISTERED (written before any P3 result was computed):

Hypothesis h = (feature, horizon, event direction UP/DOWN). Families:
  ORDER_FLOW, LIQ_PROXY (from p3_lab), SPOT_PERP (p3_spot features).
Statistic: partial correlation between the feature and the forward gross
  LONG return, after BOTH were residualised (OLS, per period and direction)
  on the controls today's signal already sees: ret30, ret60, ret240, ret1440,
  |ret30|, rsi30, volz30, funding, oi60, atr15, btc240. p-values: cluster-t
  over the per-event products, clusters = UTC days.
Selection uses TRAIN and VALID only:
  1. TRAIN: Benjamini-Hochberg over ALL hypotheses of ALL families, q < 0.10;
  2. VALID: same sign, p < 0.05.
Survivor of selection = candidate. A candidate is a NEW EDGE only if
  3. TEST and HOLDOUT both have the same sign (the numbers are only looked at
     for candidates);
  4. its economic rule beats the baseline NET of costs in EVERY period, for
     the side it favours, with >= 80 % of the baseline's trades kept. Rule:
     drop the quintile (edges fixed on TRAIN) with the worst expected return
     for that side; the bot's bracket (SL 4.3 / TP 5 / 6 h) and the horizon
     return are both reported;
  5. the latency study (p3_leadlag / horizon decay) shows the information is
     still there at the bot's real latency.
Costs: taker 0.05 % per side + 0.03 % slippage per side on horizon returns
  (0.16 % round trip); the bracket adds 0.10 % stop slippage and real funding.
A result positive only in TEST is selection bias and is rejected.

    python -m crypto_trading.entry_research.p3_analysis
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research.p3_lab import EVENTS_DB, HOLD_S, HORIZONS, LAT, SL_PCT
from crypto_trading.entry_research.regime_lab import PERIODS
from crypto_trading.entry_research.stats import bh, cluster_t, t_sf

OUT = Path("data/entry_research/p3_results.json")
SPOT_DB = Path("data/entry_research/p3_spot_features.db")
RT_COST = 2 * 0.0005 + 2 * 0.0003
CONTROLS = (
    "ret30",
    "ret60",
    "ret240",
    "ret1440",
    "abs_ret30",
    "rsi30",
    "volz30",
    "funding",
    "oi60",
    "atr15",
    "btc240",
)
FAMILIES = {
    "ORDER_FLOW": (
        "ti5",
        "ti15",
        "ti30",
        "ti60",
        "dti15",
        "acc_ti",
        "persist",
        "flowz",
        "impact",
        "diverge",
    ),
    "LIQ_PROXY": ("doi5", "doi15", "oiz15", "liq_long", "liq_short", "doi5_acc"),
    "SPOT_PERP": (
        "spot_lead5",
        "spot_lead15",
        "spot_lead30",
        "spot_ti15",
        "ti_gap15",
        "basis",
        "dbasis60",
    ),
}
PERIOD_NAMES = ("TRAIN", "VALID", "TEST", "HOLDOUT")


def period_of(T: int) -> str | None:
    for i, (name, a, b) in enumerate(PERIODS):
        if a <= T < b:
            nxt = PERIODS[i + 1][1] if i + 1 < len(PERIODS) else None
            if nxt is not None and T + LAT + HOLD_S >= nxt:
                return None  # purged: outcome reaches into the next period
            return name
    return None


def load_events() -> list[dict]:
    db = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM events")]
    spot = {}
    if SPOT_DB.exists():
        sdb = sqlite3.connect(f"file:{SPOT_DB}?mode=ro", uri=True)
        sdb.row_factory = sqlite3.Row
        spot = {(r["symbol"], r["T"]): dict(r) for r in sdb.execute("SELECT * FROM spot_features")}
    out = []
    for r in rows:
        p = period_of(r["T"])
        if p is None:
            continue
        r["period"] = p
        r["day"] = datetime.fromtimestamp(r["T"], UTC).strftime("%Y-%m-%d")
        r["abs_ret30"] = abs(r["ret30"]) if r["ret30"] is not None else None
        r.update(
            {
                k: v
                for k, v in spot.get((r["symbol"], r["T"]), {}).items()
                if k not in ("symbol", "T")
            }
        )
        out.append(r)
    return out


def days_in(period: str) -> float:
    a, b = next((a, b) for n, a, b in PERIODS if n == period)
    return (b - a) / 86400


# ------------------------------------------------------------------ baseline


def _summ(vals: list[float], days: float) -> dict:
    if not vals:
        return {"n": 0}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v <= 0]
    eq = peak = dd = 0.0
    for v in vals:
        eq += v
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gl = -sum(losses)
    return {
        "n": len(vals),
        "mean_pct": st.mean(vals) * 100,
        "win": len(wins) / len(vals),
        "avg_win_pct": st.mean(wins) * 100 if wins else 0.0,
        "avg_loss_pct": st.mean(losses) * 100 if losses else 0.0,
        "pf": sum(wins) / gl if gl > 0 else None,
        "max_dd_pct": dd * 100,
        "per_day": len(vals) / days,
        "total_usdt_1000": sum(vals) * 1000,
    }


def baseline(rows: list[dict]) -> dict:
    out: dict = {}
    for period in (*PERIOD_NAMES, "ALL"):
        sel = sorted(
            (r for r in rows if period == "ALL" or r["period"] == period), key=lambda r: r["T"]
        )
        days = sum(days_in(p) for p in PERIOD_NAMES) if period == "ALL" else days_in(period)
        res: dict = {}
        for side, key in (("LONG", "bl"), ("SHORT", "bs")):
            br = [r[f"{key}_ret"] for r in sel]
            res[f"{side}_bracket"] = _summ(br, days)
            res[f"{side}_bracket"]["mean_R"] = st.mean(br) / (SL_PCT / 100) if br else None
            res[f"{side}_bracket"]["mfe_pct"] = st.mean(r[f"{key}_mfe"] for r in sel) * 100
            res[f"{side}_bracket"]["mae_pct"] = st.mean(r[f"{key}_mae"] for r in sel) * 100
            res[f"{side}_bracket"]["exits"] = {
                x: sum(r[f"{key}_reason"] == x for r in sel) for x in ("TP", "SL", "TIME")
            }
            sgn = 1 if side == "LONG" else -1
            for hz in HORIZONS:
                vals = [sgn * r[f"r{hz}"] - RT_COST for r in sel if r[f"r{hz}"] is not None]
                gross = [sgn * r[f"r{hz}"] for r in sel if r[f"r{hz}"] is not None]
                res[f"{side}_{hz}m"] = {
                    "net_mean_pct": st.mean(vals) * 100,
                    "gross_mean_pct": st.mean(gross) * 100,
                    "win": sum(v > 0 for v in vals) / len(vals),
                    "n": len(vals),
                }
        for d, name in ((1, "UP"), (-1, "DOWN")):
            sub = [r for r in sel if r["dir"] == d]
            res[f"dir_{name}"] = {
                "n": len(sub),
                "LONG_bracket_pct": st.mean(r["bl_ret"] for r in sub) * 100,
                "SHORT_bracket_pct": st.mean(r["bs_ret"] for r in sub) * 100,
                "LONG_60m_net_pct": st.mean(r["r60"] - RT_COST for r in sub if r["r60"] is not None)
                * 100,
            }
        for reg, cond in (
            ("BTC_UP", lambda x: x > 0.005),
            ("BTC_DOWN", lambda x: x < -0.005),
            ("BTC_FLAT", lambda x: -0.005 <= x <= 0.005),
        ):
            sub = [r for r in sel if r["btc240"] is not None and cond(r["btc240"])]
            if sub:
                res[f"regime_{reg}"] = {
                    "n": len(sub),
                    "LONG_bracket_pct": st.mean(r["bl_ret"] for r in sub) * 100,
                    "SHORT_bracket_pct": st.mean(r["bs_ret"] for r in sub) * 100,
                }
        res["triggers"] = {
            name: sum(1 for r in sel if r["trig"] & bit)
            for bit, name in ((1, "price"), (2, "rsi"), (4, "volume"), (8, "funding"))
        }
        out[period] = res
    return out


# ---------------------------------------------------------- residualisation


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            continue
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


class Residualizer:
    """OLS on [1, controls] fitted once per subset; residual of any vector."""

    def __init__(self, X: list[list[float]]):
        self.X = X
        k = len(X[0])
        self.xtx = [[sum(r[i] * r[j] for r in X) for j in range(k)] for i in range(k)]

    def resid(self, y: list[float]) -> list[float]:
        k = len(self.X[0])
        xty = [sum(r[i] * yy for r, yy in zip(self.X, y, strict=True)) for i in range(k)]
        beta = _solve(self.xtx, xty)
        return [
            yy - sum(b * x for b, x in zip(beta, r, strict=True))
            for r, yy in zip(self.X, y, strict=True)
        ]


def _winsor(xs: list[float], q: float = 0.005) -> list[float]:
    s = sorted(xs)
    lo, hi = s[int(q * (len(s) - 1))], s[int((1 - q) * (len(s) - 1))]
    return [min(max(x, lo), hi) for x in xs]


def partial_corr(f: list[float], y: list[float], days: list[str]) -> tuple[float, float]:
    """(rho, two-sided cluster p) for residual vectors f, y."""
    n = len(f)
    if n < 50:
        return 0.0, 1.0
    mf, my = sum(f) / n, sum(y) / n
    sf = math.sqrt(sum((a - mf) ** 2 for a in f) / n)
    sy = math.sqrt(sum((b - my) ** 2 for b in y) / n)
    if sf == 0 or sy == 0:
        return 0.0, 1.0
    prods = [(a - mf) / sf * (b - my) / sy for a, b in zip(f, y, strict=True)]
    t, g = cluster_t(prods, days)
    p = 2 * t_sf(abs(t), max(g - 1, 1)) if g > 1 else 1.0
    return sum(prods) / n, min(1.0, p)


# ------------------------------------------------------------ hypotheses


def run_family(rows: list[dict], family: str) -> list[dict]:
    feats = FAMILIES[family]
    results = []
    for d, dname in ((1, "UP"), (-1, "DOWN")):
        per_period: dict[str, dict] = {}
        for period in PERIOD_NAMES:
            sub = [
                r
                for r in rows
                if r["period"] == period
                and r["dir"] == d
                and all(r.get(c) is not None for c in CONTROLS)
                and all(r.get(f) is not None for f in feats)
                and all(r.get(f"r{h}") is not None for h in HORIZONS)
            ]
            if len(sub) < 200:
                per_period[period] = None
                continue
            X = [[1.0] + [float(r[c]) for c in CONTROLS] for r in sub]
            for j in range(1, len(X[0])):  # winsorise controls column-wise
                col = _winsor([x[j] for x in X])
                for x, v in zip(X, col, strict=True):
                    x[j] = v
            res = Residualizer(X)
            days = [r["day"] for r in sub]
            fres = {f: res.resid(_winsor([float(r[f]) for r in sub])) for f in feats}
            yres = {h: res.resid(_winsor([r[f"r{h}"] for r in sub])) for h in HORIZONS}
            per_period[period] = {"n": len(sub), "f": fres, "y": yres, "days": days, "rows": sub}
        for f in feats:
            for h in HORIZONS:
                rec = {"family": family, "feature": f, "horizon": h, "dir": dname, "periods": {}}
                for period in PERIOD_NAMES:
                    pp = per_period[period]
                    if pp is None:
                        rec["periods"][period] = None
                        continue
                    rho, p = partial_corr(pp["f"][f], pp["y"][h], pp["days"])
                    rec["periods"][period] = {"n": pp["n"], "rho": rho, "p": p}
                results.append(rec)
        # keep the raw rows for the economic test of survivors
        for rec in results:
            if rec["family"] == family and rec["dir"] == dname:
                rec["_rows"] = {p: (per_period[p] or {}).get("rows") for p in PERIOD_NAMES}
    return results


def economic(rec: dict) -> dict:
    """Pre-registered rule: drop the TRAIN-defined quintile with the worst
    expected return for the side the feature favours; baseline = all events."""
    f, h = rec["feature"], rec["horizon"]
    sign = 1 if rec["periods"]["TRAIN"]["rho"] > 0 else -1
    train = rec["_rows"]["TRAIN"]
    cuts = sorted(r[f] for r in train)
    q20, q80 = cuts[int(0.2 * (len(cuts) - 1))], cuts[int(0.8 * (len(cuts) - 1))]
    out = {}
    for side, sgn, key in (("LONG", 1, "bl"), ("SHORT", -1, "bs")):
        # LONG wants high expected return: drop the low-expectation tail
        drop_low = (sign * sgn) > 0
        per = {}
        for period in PERIOD_NAMES:
            rows = rec["_rows"][period] or []
            keep = [r for r in rows if (r[f] > q20 if drop_low else r[f] < q80)]
            base_h = [sgn * r[f"r{h}"] - RT_COST for r in rows]
            cand_h = [sgn * r[f"r{h}"] - RT_COST for r in keep]
            base_b = [r[f"{key}_ret"] for r in rows]
            cand_b = [r[f"{key}_ret"] for r in keep]
            per[period] = {
                "retention": len(keep) / len(rows) if rows else None,
                "base_h_pct": st.mean(base_h) * 100 if base_h else None,
                "cand_h_pct": st.mean(cand_h) * 100 if cand_h else None,
                "base_bracket_pct": st.mean(base_b) * 100 if base_b else None,
                "cand_bracket_pct": st.mean(cand_b) * 100 if cand_b else None,
                "base_total_usdt": sum(base_b) * 1000,
                "cand_total_usdt": sum(cand_b) * 1000,
            }
        out[side] = per
    return out


def evaluate(rows: list[dict]) -> dict:
    hyps: list[dict] = []
    for family in FAMILIES:
        if family == "SPOT_PERP" and not any(r.get("spot_lead5") is not None for r in rows):
            continue
        hyps.extend(run_family(rows, family))
    testable = [h for h in hyps if h["periods"]["TRAIN"] is not None]
    qs = bh([h["periods"]["TRAIN"]["p"] for h in testable])
    for h, q in zip(testable, qs, strict=True):
        h["q_train"] = q
    survivors, candidates = [], []
    for h in testable:
        tr, va = h["periods"]["TRAIN"], h["periods"]["VALID"]
        h["selected"] = (
            h["q_train"] < 0.10
            and va is not None
            and va["p"] < 0.05
            and (va["rho"] > 0) == (tr["rho"] > 0)
        )
        if not h["selected"]:
            continue
        candidates.append(h)
        te, ho = h["periods"]["TEST"], h["periods"]["HOLDOUT"]
        same = all(x is not None and (x["rho"] > 0) == (tr["rho"] > 0) for x in (te, ho))
        h["oos_same_sign"] = same
        h["economic"] = economic(h)
        ok_sides = []
        for side, per in h["economic"].items():
            if all(
                per[p]["retention"]
                and per[p]["retention"] >= 0.8
                and per[p]["cand_bracket_pct"] > per[p]["base_bracket_pct"]
                and per[p]["cand_h_pct"] > per[p]["base_h_pct"]
                for p in PERIOD_NAMES
            ):
                ok_sides.append(side)
        h["economic_ok_sides"] = ok_sides
        if same and ok_sides:
            survivors.append(h)
    by_family = defaultdict(
        lambda: {
            "hypotheses": 0,
            "train_p05": 0,
            "train_q10": 0,
            "selected": 0,
            "oos_same_sign": 0,
            "survivors": 0,
        }
    )
    for h in testable:
        b = by_family[h["family"]]
        b["hypotheses"] += 1
        b["train_p05"] += h["periods"]["TRAIN"]["p"] < 0.05
        b["train_q10"] += h["q_train"] < 0.10
        b["selected"] += h["selected"]
        b["oos_same_sign"] += bool(h.get("oos_same_sign"))
        b["survivors"] += h in survivors
    top = sorted(testable, key=lambda h: h["periods"]["TRAIN"]["p"])[:25]
    strip = lambda h: {k: v for k, v in h.items() if k != "_rows"}  # noqa: E731
    return {
        "n_hypotheses": len(testable),
        "by_family": dict(by_family),
        "candidates": [strip(h) for h in candidates],
        "survivors": [strip(h) for h in survivors],
        "top_by_train_p": [strip(h) for h in top],
    }


def main() -> None:
    rows = load_events()
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "n_events": len(rows),
        "per_period": {p: sum(r["period"] == p for r in rows) for p in PERIOD_NAMES},
        "baseline": baseline(rows),
        "incremental": evaluate(rows),
    }
    OUT.write_text(json.dumps(result, default=str))
    inc = result["incremental"]
    print("events", len(rows), result["per_period"])
    print("hypotheses", inc["n_hypotheses"], json.dumps(inc["by_family"]))
    print("survivors", len(inc["survivors"]))


if __name__ == "__main__":
    main()
