"""Entry edge research run - read-only.

    python -m crypto_trading.entry_research.run [--db PATH] [--offline]

Reads a DB (default: a fresh read-only snapshot of the production DB),
public 1m klines (cached in data/entry_research/klines.db), and writes
data/entry_research/results.json + dataset.json. Never writes the trading DB.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research import counterfactual, dataset, experience, patterns
from crypto_trading.entry_research.klines import KlineCache
from crypto_trading.entry_research.stats import bh, block_of, p_mean_positive, summarize

OUT = Path("data/entry_research")
COHORTS = ["LIVE", "CONFIRMED_NOT_LIVE", "P1_BLOCKED", "AI_NOT_CONFIRMED", "NOT_ANALYSED"]


def _rec(row, key="primary"):
    o = row["outcomes"][key]
    return {**o, "t0": row["t0"]}


def _summ(rows, key="primary"):
    recs = [_rec(r, key) for r in rows if r.get("outcomes", {}).get(key)]
    return summarize(recs)


def _own_summ(rows):
    recs = [{**r["own"], "t0": r["t0"], "minutes_to_mfe": 0, "risk_pct": None}
            for r in rows if r.get("own")]
    return summarize(recs) if recs else {"n": 0}


def _fwd(rows, h=360):
    xs = [r["fwd"].get(f"fwd_{h}m_pct") for r in rows if r.get("fwd")]
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "mean_pct": st.mean(xs) if xs else None,
            "median_pct": st.median(xs) if xs else None}


def cohort_table(rows):
    out = {}
    for c in COHORTS:
        grp = [r for r in rows if r["cohort"] == c]
        ind = [r for r in grp if r["independent"]]
        live = [r["live"]["r_actual"] for r in grp if r.get("live") and r["live"]["r_actual"] is not None]
        live_net = [r["live"]["net_usdt"] for r in grp if r.get("live") and r["live"]["net_usdt"] is not None]
        out[c] = {
            "n_all": len(grp), "n_independent": len(ind),
            "std_primary": _summ(ind), "std_fast": _summ(ind, "fast"),
            "own_bracket": _own_summ(ind), "fwd_6h": _fwd(ind),
            "live": {"n": len(live), "mean_r": st.mean(live) if live else None,
                     "sum_net_usdt": sum(live_net) if live_net else None,
                     "fees_usdt": sum(r["live"]["fees_usdt"] or 0 for r in grp if r.get("live")),
                     "funding_usdt": sum(r["live"]["funding_usdt"] or 0 for r in grp if r.get("live"))},
        }
    return out


def p1_table(rows):
    out = {}
    blocked = [r for r in rows if r["p1_would_block"]]
    for reason in sorted({x for r in blocked for x in r["p1_reasons"]}):
        grp = [r for r in blocked if reason in r["p1_reasons"]]
        out[reason] = {"n": len(grp), "own_bracket": _own_summ(grp), "std_primary": _summ(grp),
                       "live_n": sum(1 for r in grp if r.get("live"))}
    passed = [r for r in rows if r["cohort"] in ("LIVE", "CONFIRMED_NOT_LIVE") and not r["p1_would_block"]]
    out["_PASSED_CONFIRMED"] = {"n": len(passed), "own_bracket": _own_summ(passed),
                                "std_primary": _summ(passed)}
    out["_ALL_BLOCKED"] = {"n": len(blocked), "own_bracket": _own_summ(blocked), "std_primary": _summ(blocked)}
    return out


def per_period_baseline(rows):
    return {p: _summ([r for r in rows if dataset.period_of(r["t0"]) == p]) for p in ("TRAIN", "VALID", "TEST")}


def pattern_results(items):
    out = []
    for i in items:
        allrows = i["train"]["rows"] + i["valid"]["rows"] + i["test"]["rows"]
        oos = i["valid"]["rows"] + i["test"]["rows"]
        out.append({
            "pattern": patterns.label(i["pattern"]), "side": i["side"], "class": i["class"],
            "conditions": [list(c) for c in i["pattern"]],
            "k": len(i["pattern"]),
            "train": _summ(i["train"]["rows"]), "valid": _summ(i["valid"]["rows"]),
            "test": _summ(i["test"]["rows"]), "oos": _summ(oos), "all": _summ(allrows),
            "train_rest_mean": i["train"]["rest_mean"], "valid_rest_mean": i["valid"]["rest_mean"],
            "test_rest_mean": i["test"]["rest_mean"],
            "train_p": i["train"]["p_pos"] if i["side"] == "edge" else i["train"]["p_below_rest"],
            "train_family_q": i["train_family_q"], "valid_q": i["valid_q"], "test_q": i["test_q"],
            "fast_oos": _summ(oos, "fast"),
        })
    return out


def walk_forward_features(rows, cuts):
    """Single-feature tertiles, day by day: is any single feature's sign
    stable across days? (descriptive)"""
    days = sorted({r["t0"].date() for r in rows})
    out = {}
    for f, (lo, hi) in cuts.items():
        for side in ("LOW", "HIGH"):
            means = []
            for d in days:
                vs = [r["outcomes"]["primary"]["r"] for r in rows if r["t0"].date() == d
                      and r["feat"].get(f) is not None
                      and ((r["feat"][f] <= lo) if side == "LOW" else (r["feat"][f] > hi))]
                if len(vs) >= 5:
                    means.append(st.mean(vs))
            if len(means) >= 4:
                out[f"{f}:{side}"] = {"days": len(means), "positive_days": sum(m > 0 for m in means)}
    return out


def experience_chain(rows):
    """GODFATHER chain for every analysed candidate with a forecast."""
    out = []
    for r in rows:
        c = experience.chain_record(r)
        if c["expected"] is not None and c["prediction_error"] is not None:
            out.append({"candidate_id": r["candidate_id"], "t0": r["t0"], "cohort": r["cohort"], **c})
    return out


def chain_summary(chain):
    if not chain:
        return {}
    climatology = {k: sum(1 for c in chain if c["actual"]["scenario"] == k) / len(chain)
                   for k in ("bullish", "bearish", "neutral")}
    clim_brier = st.mean(sum((climatology[k] - (1.0 if k == c["actual"]["scenario"] else 0.0)) ** 2
                             for k in climatology) for c in chain)
    r_err = [c["prediction_error"]["r_error"] for c in chain if c["prediction_error"]["r_error"] is not None]
    exp_r = [c["expected"]["expected_r"] for c in chain if c["expected"]["expected_r"] is not None
             and c["actual"]["own_r"] is not None]
    act_r = [c["actual"]["own_r"] for c in chain if c["expected"]["expected_r"] is not None
             and c["actual"]["own_r"] is not None]
    return {
        "n": len(chain), "realized_mix": climatology,
        "forecast_brier": st.mean(c["prediction_error"]["brier"] for c in chain),
        "climatology_brier": clim_brier,
        "bullish_call_but_bearish_share": st.mean(1.0 if c["prediction_error"]["direction_wrong"] else 0.0
                                                  for c in chain),
        "expected_r_mean": st.mean(exp_r) if exp_r else None, "actual_r_mean": st.mean(act_r) if act_r else None,
        "r_error_mean": st.mean(r_err) if r_err else None, "n_r": len(r_err),
        "corr_expected_actual": _corr(exp_r, act_r),
    }


def _corr(a, b):
    if len(a) < 3:
        return None
    ma, mb = st.mean(a), st.mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if not va or not vb:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5


def ai_feature_tests(rows):
    """For analysed candidates only: does any AI quantity separate R?
    (single-feature tertiles, BH across all)."""
    analysed = [r for r in rows if r["feat"].get("fc_bull") is not None]
    cuts = patterns.tertile_cuts(analysed)
    ai = ["fc_bull_minus_bear", "fc_entropy", "opp_score", "rr", "stop_dist_pct", "signal_age_min", "gf_eq_score"]
    tests = []
    for f in ai:
        if f not in cuts:
            continue
        lo, hi = cuts[f]
        for side in ("LOW", "HIGH"):
            g = [r for r in analysed if r["feat"].get(f) is not None
                 and ((r["feat"][f] <= lo) if side == "LOW" else (r["feat"][f] > hi))]
            vals = [r["outcomes"]["primary"]["r"] for r in g]
            cl = [block_of(r["t0"]) for r in g]
            if len(vals) < 10:
                continue
            tests.append({"feature": f, "side": side, "cut": lo if side == "LOW" else hi, "summary": _summ(g),
                          "p_pos": p_mean_positive(vals, cl)})
    for t, q in zip(tests, bh([t["p_pos"] for t in tests])):
        t["q"] = q
    return {"n_analysed_independent": len(analysed), "tests": tests}


def rule_significance(cf: dict, live_rows: list[dict], cuts: dict, usable: list[dict]) -> dict:
    """Is a counterfactual reject rule better than blocking the same NUMBER
    of LIVE trades at random? (permutation, BH across the 8 rules) - and does
    the same rule hold on the whole independent population per period?"""
    import random

    rs = [r["live"]["r_actual"] for r in live_rows]
    rng = random.Random(7)
    out, ps = {}, []
    for name, rule in counterfactual.REJECT_RULES.items():
        t = cf["rules"][name]
        k, observed = t["blocked"], t["net_effect_r"]
        if k == 0:
            continue
        hits = 0
        for _ in range(5000):
            if -sum(rng.sample(rs, k)) >= observed - 1e-12:
                hits += 1
        p = (hits + 1) / 5001
        f, side = rule
        per_period = {}
        if f in cuts:
            lo, hi = cuts[f]
            for per in ("TRAIN", "VALID", "TEST"):
                grp = [r for r in usable if dataset.period_of(r["t0"]) == per and r["feat"].get(f) is not None
                       and ((r["feat"][f] <= lo) if side == "LOW" else (r["feat"][f] > hi))]
                rest = [r for r in usable if dataset.period_of(r["t0"]) == per and r not in grp]
                per_period[per] = {"blocked": _summ(grp), "kept": _summ(rest)}
        out[name] = {"k": k, "net_effect_r": observed, "random_expectation_r": -k * st.mean(rs),
                     "p_perm": p, "population": per_period}
        ps.append((name, p))
    for (name, _), q in zip(ps, bh([p for _, p in ps])):
        out[name]["q"] = q
    return out


def bracket_sensitivity(usable: list[dict], cache) -> dict:
    """NOT used for selection - only to show the baseline conclusion does
    not hinge on the pre-registered 2/3 ATR bracket."""
    from datetime import timedelta

    from crypto_trading.shadow.evaluation import simulate_trade

    out = {}
    for sl, tp in ((1.5, 3.0), (2.0, 3.0), (3.0, 4.5), (3.0, 6.0)):
        per = {}
        for p in ("TRAIN", "VALID", "TEST"):
            vals = []
            for r in usable:
                if dataset.period_of(r["t0"]) != p or not r["feat"].get("atr15_pct"):
                    continue
                t_entry = r["t0"] + timedelta(minutes=dataset.PRIMARY_LATENCY_MIN)
                bars = cache.bars(r["symbol"], t_entry, t_entry + timedelta(minutes=dataset.HORIZON_MIN + 5))
                if not bars:
                    continue
                e, a = bars[0].o, r["feat"]["atr15_pct"] / 100
                res = simulate_trade(bars, e, e * (1 - sl * a), e * (1 + tp * a), dataset.FEE_RT, dataset.STOP_SLIP)
                if res:
                    vals.append(res.r)
            per[p] = {"n": len(vals), "mean_r": st.mean(vals) if vals else None}
        out[f"SL{sl}_TP{tp}"] = per
    return out


def build(db: str, offline: bool) -> list[dict]:
    connector = None
    if not offline:
        from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector

        connector = BingXMarketDataConnector(base_url="https://open-api.bingx.com", timeout_seconds=10.0,
                                             max_retries=3, requests_per_second=2, cache_ttl_seconds=0)
    cache = KlineCache(OUT / "klines.db", connector)
    rows = dataset.load_rows(db)
    dataset.mark_independent(rows)
    for r in rows:
        dataset.enrich(r, cache)
    return rows, cache


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(OUT / "snapshot.db"))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--snapshot", action="store_true", help="refresh the read-only DB snapshot first")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.snapshot:
        src = sqlite3.connect("file:data/crypto_trading.db?mode=ro", uri=True)
        dst = sqlite3.connect(args.db)
        src.backup(dst)
        dst.close()
        src.close()
    rows, cache = build(args.db, args.offline)
    usable = [r for r in rows if r["independent"] and r["bars_ok"] and r["outcomes"].get("primary")]
    train = [r for r in usable if dataset.period_of(r["t0"]) == "TRAIN"]
    valid = [r for r in usable if dataset.period_of(r["t0"]) == "VALID"]
    test = [r for r in usable if dataset.period_of(r["t0"]) == "TEST"]
    cuts = patterns.tertile_cuts(train)
    selected, n_tested = patterns.discover(train, cuts)
    items = patterns.evaluate(selected, valid, test)
    live_rows = [r for r in rows if r.get("live") and r["live"]["r_actual"] is not None]
    cf = counterfactual.analyse(live_rows, cuts, cache)
    chain = experience_chain([r for r in rows if r["independent"] and r["bars_ok"]])
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "design": {"primary_latency_min": dataset.PRIMARY_LATENCY_MIN, "stop_atr": dataset.STOP_ATR,
                   "target_atr": dataset.TARGET_ATR, "horizon_min": dataset.HORIZON_MIN,
                   "fee_rt": dataset.FEE_RT, "stop_slip": dataset.STOP_SLIP,
                   "valid_from": dataset.VALID_FROM.isoformat(), "test_from": dataset.TEST_FROM.isoformat()},
        "counts": {"candidates": len(rows), "independent": sum(r["independent"] for r in rows),
                   "usable": len(usable), "train": len(train), "valid": len(valid), "test": len(test),
                   "no_bars": sum(1 for r in rows if not r["bars_ok"])},
        "cuts": cuts, "baseline": per_period_baseline(usable), "baseline_fast": {
            p: _summ([r for r in usable if dataset.period_of(r["t0"]) == p], "fast") for p in ("TRAIN", "VALID", "TEST")},
        "cohorts": cohort_table([r for r in rows if r["bars_ok"]]),
        "p1": p1_table([r for r in rows if r["bars_ok"]]),
        "patterns_tested_train": n_tested, "patterns": pattern_results(items),
        "walk_forward_single": walk_forward_features(usable, cuts),
        "ai_features": ai_feature_tests(usable),
        "counterfactual": cf, "experience": chain_summary(chain),
        "rule_significance": rule_significance(cf, live_rows, cuts, usable),
        "bracket_sensitivity": bracket_sensitivity(usable, cache),
        "fwd_by_period": {p: _fwd([r for r in usable if dataset.period_of(r["t0"]) == p]) for p in
                          ("TRAIN", "VALID", "TEST")},
    }
    (OUT / "results.json").write_text(json.dumps(results, default=_js, indent=1), encoding="utf-8")
    (OUT / "dataset.json").write_text(json.dumps(
        [{k: v for k, v in r.items() if k != "row"} for r in rows], default=_js), encoding="utf-8")
    (OUT / "experience_chain.json").write_text(json.dumps(chain, default=_js), encoding="utf-8")
    print(json.dumps(results["counts"]), "patterns tested:", n_tested, "selected:", len(items))


def _js(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, float) and o != o:
        return None
    if isinstance(o, set):
        return sorted(o)
    return str(o)


if __name__ == "__main__":
    main()
