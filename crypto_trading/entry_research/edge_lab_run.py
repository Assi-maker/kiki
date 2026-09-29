"""Edge lab analysis run (2026-09-29) - read-only.

    python -m crypto_trading.entry_research.edge_lab_run

Needs data/entry_research/universe.pkl (built by edge_lab.build_universe +
add_regimes) and dataset.json (entry research). Writes edge_lab.json.
Every statistical claim: time split with purge (edge_lab.split), clustered
SEs, Benjamini-Hochberg at every stage, and the pre-registered class rules
of entry_research.patterns.
"""
from __future__ import annotations

import json
import math
import pickle
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from crypto_trading.entry_research import edge_lab as el
from crypto_trading.entry_research import patterns as pt
from crypto_trading.entry_research.dataset import TEST_FROM, VALID_FROM, period_of
from crypto_trading.entry_research.stats import (
    bh,
    block_of,
    cluster_bootstrap_ci,
    p_diff_negative,
    p_mean_positive,
)

OUT = Path("data/entry_research")
PERIODS = ("TRAIN", "VALID", "TEST")
FAMILIES = ("rsi", "volz", "move", "breakout", "rangeexp")
AGE_BUCKETS = ((0, 5, "0-5m"), (5, 15, "5-15m"), (15, 30, "15-30m"), (30, 60, "30-60m"), (60, 10**9, "60m+"))
NUMERIC = ["rsi", "volz", "move30", "ret_2h", "ret_4h", "accel", "atr30_pct", "range_exp", "pos_20",
           "signal_age_min", "breadth_4h", "dispersion_4h", "market_vol", "btc_ret_4h", "btc_atr30"]
BOOLEAN = ["E_rsi", "E_volz", "E_move", "E_breakout", "E_rangeexp", "S_rsi", "S_volz", "S_move", "S_breakout",
           "S_rangeexp", "trend_up", "btc_trend_up"]
COST_PER_ANALYSIS_USD = 0.133


def _stats(rs: list[float], ts: list[datetime]) -> dict:
    if not rs:
        return {"n": 0}
    cl = [block_of(t) for t in ts]
    ci = cluster_bootstrap_ci(rs, cl) if len(rs) >= 10 else None
    return {"n": len(rs), "mean_r": round(st.mean(rs), 4), "win": round(sum(r > 0 for r in rs) / len(rs), 3),
            "ci": [round(x, 3) for x in ci] if ci else None, "p_pos": round(p_mean_positive(rs, cl), 4)}


def _r(rows, key):
    return [r[key]["r"] for r in rows], [el.utc(r["T"]) for r in rows]


# ------------------------------------------------------------------ 1. event vs state + age

def event_vs_state(parts: dict) -> dict:
    out = {"baseline": {}, "families": {}}
    for per, rows in parts.items():
        base = [r for r in rows if not r["is_signal"]]
        out["baseline"][per] = {k: _stats(*_r(base, k)) for k in ("out_fast", "out_slow")}
    tests = []
    for fam in FAMILIES:
        fam_out = {}
        for per, rows in parts.items():
            sig = [r for r in rows if r["feat"][f"S_{fam}"]]
            ages = {}
            for lo, hi, name in AGE_BUCKETS:
                grp = [r for r in sig if _age(r, fam) is not None and lo <= _age(r, fam) < hi]
                ages[name] = {k: _stats(*_r(grp, k)) for k in ("out_fast", "out_slow")}
            event = [r for r in sig if r["feat"][f"E_{fam}"]]
            state = [r for r in sig if not r["feat"][f"E_{fam}"]]
            ev_r, ev_t = _r(event, "out_slow")
            st_r, st_t = _r(state, "out_slow")
            p_event_worse = p_diff_negative(ev_r, [block_of(t) for t in ev_t], st_r, [block_of(t) for t in st_t])
            p_state_worse = p_diff_negative(st_r, [block_of(t) for t in st_t], ev_r, [block_of(t) for t in ev_t])
            fam_out[per] = {"ages": ages, "event": _stats(ev_r, ev_t), "state": _stats(st_r, st_t),
                            "p_event_better": round(p_state_worse, 4), "p_state_better": round(p_event_worse, 4)}
            tests.append((fam, per, p_state_worse))
        out["families"][fam] = fam_out
    for (fam, per, _), q in zip(tests, bh([t[2] for t in tests]), strict=True):
        out["families"][fam][per]["q_event_better"] = round(q, 4)
    return out


def _age(r: dict, fam: str) -> float | None:
    return r["feat"].get(f"age_{fam}")


# ------------------------------------------------------------------ 2. interactions (+ negative side)

def interactions(parts: dict) -> dict:
    thinned = {p: el.as_pattern_rows(el.thin([r for r in rows if r["is_signal"]], 60)) for p, rows in parts.items()}
    cuts = pt.tertile_cuts(thinned["TRAIN"], numeric=NUMERIC)
    saved = (pt.NUMERIC, pt.BOOLEAN)
    try:
        pt.NUMERIC, pt.BOOLEAN = NUMERIC, BOOLEAN
        selected, n_tested = pt.discover(thinned["TRAIN"], cuts, boolean=BOOLEAN)
        items = pt.evaluate(selected, thinned["VALID"], thinned["TEST"])
    finally:
        pt.NUMERIC, pt.BOOLEAN = saved
    res = []
    for i in items:
        def s(part, i=i):
            rs = [r["outcomes"]["primary"] for r in i[part]["rows"]]
            if not rs:
                return {"n": 0}
            return {"n": len(rs), "mean_r": round(st.mean(x["r"] for x in rs), 3),
                    "sl_rate": round(sum(x["reason"] == "SL" for x in rs) / len(rs), 3),
                    "time_rate": round(sum(x["reason"] == "TIME" for x in rs) / len(rs), 3),
                    "low_mfe_rate": round(sum(x["low_mfe"] for x in rs) / len(rs), 3),
                    "fast_decay_rate": round(sum(x["fast_decay"] for x in rs) / len(rs), 3)}
        res.append({"pattern": pt.label(i["pattern"]), "side": i["side"], "class": i["class"],
                    "train": s("train"), "valid": s("valid"), "test": s("test"),
                    "rest_mean": [round(i[p]["rest_mean"], 3) if i[p]["rest_mean"] is not None else None
                                  for p in ("train", "valid", "test")],
                    "train_family_q": round(i["train_family_q"], 3), "valid_q": round(i["valid_q"], 3),
                    "test_q": round(i["test_q"], 3) if i["test_q"] is not None else None,
                    "regimes_oos": _regime_split(i["valid"]["rows"] + i["test"]["rows"], cuts)})
    return {"n_rows": {p: len(v) for p, v in thinned.items()}, "patterns_tested_train": n_tested,
            "cuts": cuts, "patterns": res}


def _regime_split(rows: list[dict], cuts: dict) -> dict:
    out = {}
    for name, f in (("btc_trend", "btc_trend_up"), ("breadth", "breadth_4h"), ("dispersion", "dispersion_4h"),
                    ("market_vol", "market_vol")):
        groups = defaultdict(list)
        for r in rows:
            v = r["feat"].get(f)
            if v is None:
                continue
            if isinstance(v, bool):
                key = "up" if v else "down"
            else:
                lo, hi = cuts.get(f, (None, None))
                if lo is None:
                    continue
                key = "low" if v <= lo else "high" if v > hi else "mid"
            groups[key].append(r["outcomes"]["primary"]["r"])
        out[name] = {k: {"n": len(v), "mean_r": round(st.mean(v), 3)} for k, v in sorted(groups.items())}
    return out


# ------------------------------------------------------------------ 3. regimes on all signals

def regimes(parts: dict) -> dict:
    train_sig = el.as_pattern_rows([r for r in parts["TRAIN"] if r["is_signal"]])
    cuts = pt.tertile_cuts(train_sig, numeric=["breadth_4h", "dispersion_4h", "market_vol", "btc_atr30", "btc_ret_4h"])
    out = {}
    for per, rows in parts.items():
        sig = el.as_pattern_rows([r for r in rows if r["is_signal"]])
        out[per] = _regime_split(sig, cuts) | {"btc_ret_4h": _tertile_split(sig, cuts, "btc_ret_4h"),
                                               "btc_atr30": _tertile_split(sig, cuts, "btc_atr30")}
    return {"cuts_train": cuts, "by_period": out}


def _tertile_split(rows, cuts, f):
    lo, hi = cuts.get(f, (None, None))
    if lo is None:
        return {}
    g = defaultdict(list)
    for r in rows:
        v = r["feat"].get(f)
        if v is not None:
            g["low" if v <= lo else "high" if v > hi else "mid"].append(r["outcomes"]["primary"]["r"])
    return {k: {"n": len(v), "mean_r": round(st.mean(v), 3)} for k, v in sorted(g.items())}


# ------------------------------------------------------------------ 4. meta-labeling

META_FEATURES = ["candidate_score", "rsi_30m", "rsi_1h", "chg_30m", "chg_1h", "volz_30m", "volz_1h", "funding_pct",
                 "ret_15m", "ret_1h", "ret_4h", "atr15_pct", "pos_4h", "btc_ret_1h", "btc_ret_4h", "btc_atr15_pct",
                 "run_heat", "drift_to_fill_pct", "n_triggers"]


def _fit_logistic(X, y, l2=1.0, iters=400, lr=0.1):
    w = [0.0] * (len(X[0]) + 1)
    for _ in range(iters):
        g = [0.0] * len(w)
        for xi, yi in zip(X, y, strict=True):
            z = w[0] + sum(a * b for a, b in zip(w[1:], xi, strict=True))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            e = p - yi
            g[0] += e
            for k, a in enumerate(xi):
                g[k + 1] += e * a
        n = len(X)
        w = [w[0] - lr * g[0] / n] + [wk - lr * (gk / n + l2 * wk / n) for wk, gk in zip(w[1:], g[1:], strict=True)]
    return w


def meta_labeling() -> dict:
    """Layer 1 = our candidate (screener said worth_deeper_analysis).
    Layer 2 = is THIS one worth taking? Trained on TRAIN only, strictly
    point-in-time features, label = standardized outcome R > 0. Compared on
    VALID/TEST with the CURRENT ranking (candidate_score) at the same
    selection rate (top third)."""
    raw = json.loads((OUT / "dataset.json").read_text(encoding="utf-8"))
    rows = [r for r in raw if r.get("independent") and r.get("bars_ok") and (r.get("outcomes") or {}).get("primary")]
    for r in rows:
        r["t0"] = datetime.fromisoformat(r["t0"])
    by = {p: [r for r in rows if period_of(r["t0"]) == p] for p in PERIODS}
    feats = [f for f in META_FEATURES if sum(r["feat"].get(f) is not None for r in by["TRAIN"]) > 0.8 * len(by["TRAIN"])]
    mu = {f: st.mean(r["feat"][f] for r in by["TRAIN"] if r["feat"].get(f) is not None) for f in feats}
    sd = {f: (st.pstdev([r["feat"][f] for r in by["TRAIN"] if r["feat"].get(f) is not None]) or 1.0) for f in feats}

    def x(r):
        return [max(-5, min(5, ((r["feat"].get(f) if r["feat"].get(f) is not None else mu[f]) - mu[f]) / sd[f]))
                for f in feats]
    w = _fit_logistic([x(r) for r in by["TRAIN"]], [1 if r["outcomes"]["primary"]["r"] > 0 else 0 for r in by["TRAIN"]])

    def score(r):
        return w[0] + sum(a * b for a, b in zip(w[1:], x(r), strict=True))
    out = {"features": feats, "weights": {f: round(v, 3) for f, v in zip(feats, w[1:], strict=True)}}
    for per in PERIODS:
        rs = by[per]
        k = max(1, len(rs) // 3)
        meta = sorted(rs, key=score, reverse=True)[:k]
        current = sorted(rs, key=lambda r: r["feat"].get("candidate_score") or 0, reverse=True)[:k]

        def m(sel):
            return _stats([r["outcomes"]["primary"]["r"] for r in sel], [r["t0"] for r in sel])
        mr = [r["outcomes"]["primary"]["r"] for r in meta]
        cr = [r["outcomes"]["primary"]["r"] for r in current]
        out[per] = {"all": m(rs), "meta_top_third": m(meta), "current_rank_top_third": m(current),
                    "p_meta_not_better": round(p_diff_negative(cr, [block_of(r["t0"]) for r in current],
                                                               mr, [block_of(r["t0"]) for r in meta]), 4)}
    return out


# ------------------------------------------------------------------ 5. AI value

def ai_value() -> dict:
    raw = json.loads((OUT / "dataset.json").read_text(encoding="utf-8"))
    rows = [r for r in raw if r.get("independent") and r.get("bars_ok") and (r.get("outcomes") or {}).get("primary")]
    for r in rows:
        r["t0"] = datetime.fromisoformat(r["t0"])
    out = {}
    tests = []
    for per in PERIODS:
        p_rows = [r for r in rows if period_of(r["t0"]) == per]
        groups = {
            "AI_CONFIRMED": [r for r in p_rows if r["status"] == "CONFIRMED"],
            "AI_REJECTED_QA": [r for r in p_rows if r["status"] == "REJECTED"],
            "AI_NO_TRADE": [r for r in p_rows if r["status"] == "NO_TRADE"],
            "NOT_ANALYSED": [r for r in p_rows if r["cohort"] == "NOT_ANALYSED"],
        }
        res = {k: _stats([r["outcomes"]["primary"]["r"] for r in v], [r["t0"] for r in v]) for k, v in groups.items()}
        conf = groups["AI_CONFIRMED"]
        rej = groups["AI_REJECTED_QA"] + groups["AI_NO_TRADE"]
        cr, rr = [r["outcomes"]["primary"]["r"] for r in conf], [r["outcomes"]["primary"]["r"] for r in rej]
        p = p_diff_negative(rr, [block_of(r["t0"]) for r in rej], cr, [block_of(r["t0"]) for r in conf])
        res["confirmed_minus_rejected_r"] = round(st.mean(cr) - st.mean(rr), 4) if cr and rr else None
        res["p_ai_selection_adds_value"] = round(p, 4)
        tests.append((per, p))
        out[per] = res
    for (per, _), q in zip(tests, bh([t[1] for t in tests]), strict=True):
        out[per]["q_ai_selection_adds_value"] = round(q, 4)
    roles = {"forecast (bull-bear)": "fc_bull_minus_bear", "forecast (entropy)": "fc_entropy",
             "screener (opp_score)": "opp_score", "risk (R:R)": "rr", "risk (stop distance)": "stop_dist_pct",
             "bear (counterarguments)": "bear_counterargs", "qa (violations)": "qa_violations",
             "godfather EQ score": "gf_eq_score"}
    corr = {}
    ps = []
    analysed = [r for r in rows if r["cohort"] != "NOT_ANALYSED"]
    for name, f in roles.items():
        pairs = [(r["feat"][f], r["outcomes"]["primary"]["r"]) for r in analysed if r["feat"].get(f) is not None]
        if len(pairs) < 30:
            corr[name] = {"n": len(pairs), "spearman": None}
            continue
        c = _spearman(pairs)
        # permutation-free large-sample p for |rho|
        z = abs(c) * math.sqrt(len(pairs) - 1)
        p = math.erfc(z / math.sqrt(2))
        corr[name] = {"n": len(pairs), "spearman": round(c, 3), "p": round(p, 4)}
        ps.append((name, p))
    for (name, _), q in zip(ps, bh([p for _, p in ps]), strict=True):
        corr[name]["q"] = round(q, 4)
    n_analysed = len([r for r in raw if r["cohort"] != "NOT_ANALYSED"])
    n_live = len([r for r in raw if r["cohort"] == "LIVE"])
    out["roles_information"] = corr
    out["cost"] = {"analyses": n_analysed, "cost_usd_at_0.133": round(n_analysed * COST_PER_ANALYSIS_USD, 2),
                   "live_trades_in_dataset": n_live,
                   "cost_per_live_trade_usd": round(n_analysed * COST_PER_ANALYSIS_USD / n_live, 2) if n_live else None}
    return out


def _spearman(pairs):
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        for rank, i in enumerate(order):
            r[i] = rank
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


def shorts(parts: dict) -> dict:
    """SHORT side (never tested before 2026-09-29): the mirrored triple
    barrier on the same universe. Per-period baseline, fade of each bullish
    state, and the full pre-registered pattern protocol on short outcomes.
    TEST (26-28/9) was a falling alt market, so a short result counts only if
    it also holds in TRAIN and VALID and beats the short baseline."""
    have = {p: [r for r in rows if r.get("short_slow") and r.get("short_fast")] for p, rows in parts.items()}
    out = {"baseline": {}, "fade_state": {}}
    for per, rows in have.items():
        base = [r for r in rows if not r["is_signal"]]
        out["baseline"][per] = {k: _stats(*_r(base, k)) for k in ("short_fast", "short_slow")}
        out["fade_state"][per] = {
            fam: _stats(*_r([r for r in rows if r["feat"][f"S_{fam}"]], "short_slow")) for fam in FAMILIES}
    thinned = {p: el.as_pattern_rows(el.thin([r for r in rows if r["is_signal"]], 60), key="short_slow")
               for p, rows in have.items()}
    cuts = pt.tertile_cuts(thinned["TRAIN"], numeric=NUMERIC)
    saved = (pt.NUMERIC, pt.BOOLEAN)
    try:
        pt.NUMERIC, pt.BOOLEAN = NUMERIC, BOOLEAN
        selected, n_tested = pt.discover(thinned["TRAIN"], cuts, boolean=BOOLEAN)
        items = pt.evaluate(selected, thinned["VALID"], thinned["TEST"])
    finally:
        pt.NUMERIC, pt.BOOLEAN = saved

    def s_(part, i):
        rs = [r["outcomes"]["primary"]["r"] for r in i[part]["rows"]]
        return {"n": len(rs), "mean_r": round(st.mean(rs), 3) if rs else None}
    out["patterns_tested_train"] = n_tested
    out["patterns"] = [{"pattern": pt.label(i["pattern"]), "side": i["side"], "class": i["class"],
                        "train": s_("train", i), "valid": s_("valid", i), "test": s_("test", i),
                        "rest_mean": [round(i[x]["rest_mean"], 3) if i[x]["rest_mean"] is not None else None
                                      for x in ("train", "valid", "test")],
                        "train_family_q": round(i["train_family_q"], 3), "valid_q": round(i["valid_q"], 3),
                        "test_q": round(i["test_q"], 3) if i["test_q"] is not None else None} for i in items]
    return out


def main() -> None:
    rows = pickle.load(open(OUT / "universe.pkl", "rb"))  # noqa: SIM115 - one-shot read
    parts = el.split(rows, VALID_FROM, TEST_FROM)
    result = {
        "generated_at": datetime.now().isoformat(),
        "counts": {p: {"rows": len(v), "signal_rows": sum(r["is_signal"] for r in v),
                       "symbols": len({r["symbol"] for r in v})} for p, v in parts.items()},
        "event_vs_state": event_vs_state(parts),
        "interactions": interactions(parts),
        "regimes": regimes(parts),
        "meta_labeling": meta_labeling(),
        "ai_value": ai_value(),
        "shorts": shorts(parts),
    }
    (OUT / "edge_lab.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
