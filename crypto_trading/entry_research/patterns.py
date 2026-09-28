"""Entry pattern discovery - pre-registered three-stage protocol (2026-09-28).

Unit: one INDEPENDENT candidate (first per symbol per 6 h) with a primary
standardized outcome. The value is R after real costs, never the win rate.

Conditions: every numeric feature cut into tertiles on TRAIN (LOW / MID /
HIGH; the cutpoints are frozen and applied unchanged later), booleans as
TRUE/FALSE. Patterns: singles, pairs of different features, and triples built
by extending the 30 best TRAIN pairs (per direction).

Stage 1 TRAIN (discovery): every pattern with n >= 30. Edge candidates =
mean R > 0 with one-sided clustered p < 0.05; failure candidates = mean R < 0
and below the rest of the population (p < 0.05). The 25 best of each go on.
Stage 2 VALID: only the selected; BH across all selected (both directions).
Pass = same sign and q < 0.10.
Stage 3 TEST: only the stage-2 survivors; BH across them.

Classes (edge side):
  EDGE          - positive mean R in TRAIN, VALID and TEST; TEST n >= 30,
                  TEST q < 0.05, PF > 1, still positive without its best 5 %
                  of trades, positive in >= 2/3 of the day blocks with n >= 5.
  WEAK_EDGE     - positive in all three periods, TEST n >= 30, but not all of
                  the EDGE conditions.
  HYPOTHESIS    - positive in all periods but TEST or VALID n < 30.
  DECAYING_EDGE - significant in TRAIN, then weaker in VALID and <= 0 in TEST.
  REGIME_DEPENDENT - out-of-train mean R has opposite signs for BTC 4 h up vs
                  down (each side n >= 15).
  NOISE         - everything else.
Failure side:
  FAILURE_PATTERN - mean R < 0 AND below the rest in all three periods, TEST
                  n >= 30, TEST q < 0.05 on the difference.
  FAILURE_HYPOTHESIS - same signs everywhere but TEST n < 30 or q >= 0.05.
"""
from __future__ import annotations

import itertools
import statistics as st

from crypto_trading.entry_research.stats import (
    bh,
    block_of,
    p_diff_negative,
    p_mean_negative,
    p_mean_positive,
    summarize,
)

MIN_N_TRAIN = 30
MIN_N_EDGE = 30
SELECT_PER_SIDE = 25
PAIR_SEEDS = 30

NUMERIC = [
    "candidate_score", "rsi_30m", "rsi_1h", "chg_30m", "chg_1h", "volz_30m", "volz_1h",
    "funding_pct", "ret_15m", "ret_1h", "ret_4h", "atr15_pct", "pos_4h", "btc_ret_1h",
    "btc_ret_4h", "btc_atr15_pct", "run_heat", "drift_to_fill_pct", "n_triggers",
    # AI - only analysed candidates carry them
    "fc_bull_minus_bear", "fc_entropy", "opp_score", "rr", "stop_dist_pct", "signal_age_min",
    "gf_eq_score",
]
BOOLEAN = ["trig_volume", "trig_momentum", "trig_pricevol", "trig_funding", "tf_confirmed",
           "gf_eq_trade"]


def tertile_cuts(rows: list[dict]) -> dict[str, tuple[float, float]]:
    cuts = {}
    for f in NUMERIC:
        xs = sorted(r["feat"][f] for r in rows if r["feat"].get(f) is not None)
        if len(xs) >= 60 and xs[len(xs) // 3] < xs[2 * len(xs) // 3]:
            cuts[f] = (xs[len(xs) // 3], xs[2 * len(xs) // 3])
    return cuts


def conditions(cuts: dict) -> list[tuple]:
    out = []
    for f, (lo, hi) in cuts.items():
        out += [(f, "LOW", lo, hi), (f, "MID", lo, hi), (f, "HIGH", lo, hi)]
    out += [(f, "TRUE", None, None) for f in BOOLEAN] + [(f, "FALSE", None, None) for f in BOOLEAN]
    return out


def holds(cond: tuple, feat: dict) -> bool:
    f, op, lo, hi = cond
    v = feat.get(f)
    if v is None:
        return False
    if op == "TRUE":
        return v is True
    if op == "FALSE":
        return v is False
    if op == "LOW":
        return v <= lo
    if op == "HIGH":
        return v > hi
    return lo < v <= hi


def label(pattern: tuple) -> str:
    parts = []
    for f, op, lo, hi in pattern:
        if op in ("TRUE", "FALSE"):
            parts.append(f"{f}={op.lower()}")
        elif op == "LOW":
            parts.append(f"{f}<={lo:.4g}")
        elif op == "HIGH":
            parts.append(f"{f}>{hi:.4g}")
        else:
            parts.append(f"{lo:.4g}<{f}<={hi:.4g}")
    return " & ".join(parts)


def _rec(row: dict) -> dict:
    o = row["outcomes"]["primary"]
    return {**o, "t0": row["t0"], "row": row}


def _split(rows, pattern):
    grp, rest = [], []
    for r in rows:
        (grp if all(holds(c, r["feat"]) for c in pattern) else rest).append(r)
    return grp, rest


def _vals(rows):
    return [r["outcomes"]["primary"]["r"] for r in rows], [block_of(r["t0"]) for r in rows]


def _stage_stats(rows, pattern) -> dict:
    grp, rest = _split(rows, pattern)
    gv, gc = _vals(grp)
    rv, rc = _vals(rest)
    return {
        "n": len(gv), "mean": st.mean(gv) if gv else None,
        "rest_mean": st.mean(rv) if rv else None,
        "p_pos": p_mean_positive(gv, gc) if len(gv) >= 2 else 1.0,
        "p_neg_abs": p_mean_negative(gv, gc) if len(gv) >= 2 else 1.0,
        "p_below_rest": p_diff_negative(gv, gc, rv, rc) if len(gv) >= 2 else 1.0,
        "rows": grp,
    }


def discover(train: list[dict], cuts: dict) -> tuple[list[dict], int]:
    conds = conditions(cuts)
    singles = [(c,) for c in conds]
    pairs = [(a, b) for a, b in itertools.combinations(conds, 2) if a[0] != b[0]]
    tested: dict[tuple, dict] = {}
    for p in singles + pairs:
        s = _stage_stats(train, p)
        if s["n"] >= MIN_N_TRAIN:
            tested[p] = s
    for side in ("edge", "fail"):
        key = "p_pos" if side == "edge" else "p_below_rest"
        seeds = sorted((p for p in tested if len(p) == 2), key=lambda p: tested[p][key])[:PAIR_SEEDS]
        for p in seeds:
            used = {c[0] for c in p}
            for c in conds:
                if c[0] in used:
                    continue
                t = tuple(sorted(p + (c,)))
                if t in tested:
                    continue
                s = _stage_stats(train, t)
                if s["n"] >= MIN_N_TRAIN:
                    tested[t] = s
    edge = [p for p, s in tested.items() if s["mean"] > 0 and s["p_pos"] < 0.05]
    fail = [p for p, s in tested.items()
            if s["mean"] < 0 and s["rest_mean"] is not None and s["p_below_rest"] < 0.05
            and s["mean"] < s["rest_mean"]]
    edge = sorted(edge, key=lambda p: tested[p]["p_pos"])[:SELECT_PER_SIDE]
    fail = sorted(fail, key=lambda p: tested[p]["p_below_rest"])[:SELECT_PER_SIDE]
    train_q = dict(zip(tested, bh([s["p_pos"] for s in tested.values()])))
    train_q_fail = dict(zip(tested, bh([s["p_below_rest"] for s in tested.values()])))
    selected = ([{"pattern": p, "side": "edge", "train": tested[p], "train_family_q": train_q[p]}
                 for p in edge]
                + [{"pattern": p, "side": "fail", "train": tested[p], "train_family_q": train_q_fail[p]}
                   for p in fail])
    return selected, len(tested)


def _p_for(side, s):
    return s["p_pos"] if side == "edge" else s["p_below_rest"]


def _same_sign(side, s):
    if s["n"] == 0 or s["mean"] is None:
        return False
    if side == "edge":
        return s["mean"] > 0
    return s["mean"] < 0 and s["rest_mean"] is not None and s["mean"] < s["rest_mean"]


def _day_block_stability(rows: list[dict]) -> float | None:
    days: dict[str, list[float]] = {}
    for r in rows:
        days.setdefault(r["t0"].date().isoformat(), []).append(r["outcomes"]["primary"]["r"])
    usable = [st.mean(v) for v in days.values() if len(v) >= 5]
    if len(usable) < 3:
        return None
    return sum(1 for m in usable if m > 0) / len(usable)


def _regime_split(rows: list[dict]) -> tuple[dict, dict]:
    up = [r["outcomes"]["primary"]["r"] for r in rows if (r["feat"].get("btc_ret_4h") or 0) > 0]
    down = [r["outcomes"]["primary"]["r"] for r in rows if (r["feat"].get("btc_ret_4h") or 0) <= 0]
    return ({"n": len(up), "mean": st.mean(up) if up else None},
            {"n": len(down), "mean": st.mean(down) if down else None})


def evaluate(selected: list[dict], valid: list[dict], test: list[dict]) -> list[dict]:
    for item in selected:
        item["valid"] = _stage_stats(valid, item["pattern"])
    vq = bh([_p_for(i["side"], i["valid"]) for i in selected]) if selected else []
    survivors = []
    for item, q in zip(selected, vq):
        item["valid_q"] = q
        item["valid_pass"] = _same_sign(item["side"], item["valid"]) and q < 0.10
        item["test"] = _stage_stats(test, item["pattern"])
        if item["valid_pass"]:
            survivors.append(item)
    tq = bh([_p_for(i["side"], i["test"]) for i in survivors]) if survivors else []
    for item in selected:
        item["test_q"] = None
    for item, q in zip(survivors, tq):
        item["test_q"] = q
    for item in selected:
        item["class"] = classify(item)
    return selected


def classify(item: dict) -> str:
    side, tr, va, te = item["side"], item["train"], item["valid"], item["test"]
    signs = [_same_sign(side, s) for s in (tr, va, te)]
    oos_rows = va["rows"] + te["rows"]
    if side == "fail":
        if all(signs):
            if te["n"] >= MIN_N_EDGE and item.get("test_q") is not None and item["test_q"] < 0.05:
                return "FAILURE_PATTERN"
            return "FAILURE_HYPOTHESIS"
        return "NOISE"
    up, down = _regime_split(oos_rows)
    if all(signs):
        if va["n"] < MIN_N_EDGE or te["n"] < MIN_N_EDGE:
            return "HYPOTHESIS"
        summ = summarize([_rec(r) for r in te["rows"]])
        stability = _day_block_stability(tr["rows"] + oos_rows)
        if (item.get("test_q") is not None and item["test_q"] < 0.05 and (summ["pf"] or 0) > 1
                and (summ["mean_r_wo_top5pct"] or 0) > 0 and (stability or 0) >= 2 / 3):
            return "EDGE"
        return "WEAK_EDGE"
    if (up["n"] >= 15 and down["n"] >= 15 and up["mean"] is not None and down["mean"] is not None
            and (up["mean"] > 0) != (down["mean"] > 0)):
        return "REGIME_DEPENDENT"
    if tr["p_pos"] < 0.05 and va["mean"] is not None and te["mean"] is not None \
            and va["mean"] < tr["mean"] and te["mean"] <= 0:
        return "DECAYING_EDGE"
    return "NOISE"
