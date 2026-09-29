"""Signal lifecycle + pre-AI risk-compatibility analysis (2026-09-29) - read-only.

    python -m crypto_trading.entry_research.lifecycle_run

1. Signal lifecycle. An EPISODE is a run of candidates on the same symbol
   whose consecutive gaps are < 2 h (pre-registered). Episode index 1 = NEW,
   2 = ACTIVE, 3+ = AGING; age = minutes since the episode's first signal.
   Outcome = the entry-research standardized outcome (entry at +23 min, 2/3
   ATR bracket, real costs) - the same for every candidate, so repeated and
   fresh signals are compared on equal terms.
2. Pre-AI risk compatibility. The Risk Agent's stop is unknown before the
   AI chain; a deterministic proxy is k x ATR15 with k = the TRAIN median of
   stop_distance / ATR15. Predicted worst-case risk of a 1000-notional
   position vs the 5 % group cap is compared with what the kernel actually
   concluded, per period (k frozen on TRAIN).
Writes data/entry_research/lifecycle.json.
"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from crypto_trading.entry_research.dataset import period_of
from crypto_trading.entry_research.stats import block_of, cluster_bootstrap_ci, p_diff_negative

OUT = Path("data/entry_research")
EPISODE_GAP = timedelta(hours=2)
COST_PER_ANALYSIS_USD = 0.133   # measured 2026-09-29: $5.31 / 40 analysed candidates
GROUP_CAP_USDT = 420 * 0.05
NOTIONAL = 1000.0
COST_BUFFER_PCT = 0.3 + 0.1 + 0.3   # entry buffer + stop slippage + fees, as the kernel


def episodes(rows: list[dict]) -> None:
    by_symbol = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)
    for rs in by_symbol.values():
        rs.sort(key=lambda r: r["t0"])
        start, idx, prev = None, 0, None
        for r in rs:
            if prev is None or r["t0"] - prev >= EPISODE_GAP:
                start, idx = r["t0"], 0
            idx += 1
            r["episode_index"] = idx
            r["episode_age_min"] = (r["t0"] - start).total_seconds() / 60
            prev = r["t0"]


def _summ(rows):
    rs = [r["outcomes"]["primary"]["r"] for r in rows]
    cl = [block_of(r["t0"]) for r in rows]
    return {"n": len(rs), "mean_r": round(st.mean(rs), 3) if rs else None,
            "win": round(sum(x > 0 for x in rs) / len(rs), 3) if rs else None,
            "ci": [round(x, 3) for x in (cluster_bootstrap_ci(rs, cl) or [])] or None}


def main() -> None:
    raw = json.loads((OUT / "dataset.json").read_text(encoding="utf-8"))
    rows = []
    for r in raw:
        r["t0"] = datetime.fromisoformat(r["t0"])
        rows.append(r)
    episodes(rows)
    usable = [r for r in rows if r.get("bars_ok") and (r.get("outcomes") or {}).get("primary")]
    out = {"episode_gap_hours": 2}

    def bucket_idx(r):
        return "1_NEW" if r["episode_index"] == 1 else "2_ACTIVE" if r["episode_index"] == 2 else "3+_AGING"

    def bucket_age(r):
        a = r["episode_age_min"]
        return "0" if a == 0 else "<60m" if a < 60 else "60-180m" if a < 180 else ">180m"

    for name, fn in (("by_episode_index", bucket_idx), ("by_episode_age", bucket_age)):
        res = {}
        for per in ("TRAIN", "VALID", "TEST", "ALL"):
            grp = defaultdict(list)
            for r in usable:
                if per == "ALL" or period_of(r["t0"]) == per:
                    grp[fn(r)].append(r)
            res[per] = {k: _summ(v) for k, v in sorted(grp.items())}
        out[name] = res
    # repeat vs new, clustered one-sided test (repeat worse?) per period
    tests = {}
    for per in ("TRAIN", "VALID", "TEST", "ALL"):
        sel = [r for r in usable if per == "ALL" or period_of(r["t0"]) == per]
        new = [r for r in sel if r["episode_index"] == 1]
        rep = [r for r in sel if r["episode_index"] > 1]
        tests[per] = {
            "new_mean_r": round(st.mean(r["outcomes"]["primary"]["r"] for r in new), 3),
            "repeat_mean_r": round(st.mean(r["outcomes"]["primary"]["r"] for r in rep), 3),
            "p_repeat_worse": round(p_diff_negative(
                [r["outcomes"]["primary"]["r"] for r in rep], [block_of(r["t0"]) for r in rep],
                [r["outcomes"]["primary"]["r"] for r in new], [block_of(r["t0"]) for r in new]), 3),
            "n_new": len(new), "n_repeat": len(rep)}
    out["repeat_vs_new"] = tests
    analysed = [r for r in rows if r["cohort"] in ("LIVE", "CONFIRMED_NOT_LIVE", "P1_BLOCKED", "AI_NOT_CONFIRMED")]
    rep_an = [r for r in analysed if r["episode_index"] > 1]
    out["ai_on_repeats"] = {"analysed": len(analysed), "analysed_repeats": len(rep_an),
                            "share": round(len(rep_an) / len(analysed), 3),
                            "cost_usd_at_0.133": round(len(rep_an) * COST_PER_ANALYSIS_USD, 2)}

    # 2. pre-AI risk compatibility
    with_stop = [r for r in rows if r["feat"].get("stop_dist_pct") and r["feat"].get("atr15_pct")]
    train = [r for r in with_stop if period_of(r["t0"]) == "TRAIN"]
    k = st.median(r["feat"]["stop_dist_pct"] / r["feat"]["atr15_pct"] for r in train)

    def predicted_risk(r):
        return NOTIONAL * (k * r["feat"]["atr15_pct"] + COST_BUFFER_PCT) / 100

    def actual_risk(r):
        return NOTIONAL * (r["feat"]["stop_dist_pct"] + COST_BUFFER_PCT) / 100

    pre = {"k_train_median_stop_over_atr": round(k, 2)}
    for per in ("TRAIN", "VALID", "TEST"):
        sel = [r for r in with_stop if period_of(r["t0"]) == per]
        act_fit = [r for r in sel if actual_risk(r) <= GROUP_CAP_USDT]
        pred_block = [r for r in sel if predicted_risk(r) > GROUP_CAP_USDT]
        wrongly = [r for r in pred_block if actual_risk(r) <= GROUP_CAP_USDT]
        xs = [r["feat"]["atr15_pct"] for r in sel]
        ys = [r["feat"]["stop_dist_pct"] for r in sel]
        mx, my = st.mean(xs), st.mean(ys)
        corr = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True)) / (
            (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5)
        pre[per] = {"analysed_with_plan": len(sel), "actually_fit_group_cap": len(act_fit),
                    "predicted_incompatible": len(pred_block),
                    "wrongly_blocked_fitting": len(wrongly),
                    "ai_cost_saved_usd": round(len(pred_block) * COST_PER_ANALYSIS_USD, 2),
                    "corr_atr_vs_stop": round(corr, 3)}
    out["pre_ai_risk_compatibility"] = pre
    (OUT / "lifecycle.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
