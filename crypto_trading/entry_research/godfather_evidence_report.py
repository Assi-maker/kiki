"""Does evidence-as-context make GODFATHER better? (research only)

    python -m crypto_trading.entry_research.godfather_evidence_report

Reads the shadow DB (godfather_decisions / godfather_outcomes) READ-ONLY.
Arms: WITHOUT (baseline = today's context), WITH (+ historical evidence +
self-critique), WITHOUT_REPLICATE (the baseline asked again = AI noise).
Everything is paired per decision, so "better" means better on the SAME
decisions, and every difference is shown next to the AI's own noise.

ENTRY - the trade is taken regardless; the arms state expectations:
  prediction error of expected R (bias, mean |error|), Brier of P(win),
  MFE / MAE prediction error, stance vs outcome (advisory, never applied).
MANAGEMENT - HOLD / WATCH / PROTECT / EXIT at Guardian transitions:
  counterfactual expectancy (EXIT = realise at the decision, anything else
  = the actual outcome), right / false EXIT, HOLD that should have been
  EXIT, EXIT that should have been HOLD, MFE given away after EXIT, MAE
  sat through after HOLD, prediction error of the expected final R.
ATTRIBUTION - per closed trade: bad entry / bad management / both / good,
  and whether it developed as GODFATHER expected (|actual - expected| <=
  0.5 R), next to the investigator's own entry / management verdicts.
OVER TIME - the headline numbers per ISO week.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from crypto_trading.config.loader import get_settings
from crypto_trading.entry_research.evidence_ab_report import (
    _binom_two_sided,
    _cluster_ci,
    _mean,
    _rate,
    _ro,
    _wilson,
)

ARMS = ("WITHOUT", "WITH")
AS_EXPECTED_R = 0.5
GOOD_ENTRY_MFE_R = 0.5  # the entry offered at least half a planned risk in favour
# Pre-registered minimums (same as the evidence A/B): no conclusion below.
MIN_PAIRED, MIN_AA, MIN_TRADES = 100, 30, 100


def load(sh: sqlite3.Connection) -> dict[str, dict]:
    """decision_id -> {kind, arms: {arm: row}, outcome: row | None}."""
    out: dict[str, dict] = {}
    for r in sh.execute("SELECT * FROM godfather_decisions"):
        d = out.setdefault(r["decision_id"], {"kind": r["kind"], "arms": {}, "outcome": None,
                                              "decided_at": r["decided_at"],
                                              "position_id": r["position_id"]})
        d["arms"][r["arm"]] = dict(r)
    for r in sh.execute("SELECT * FROM godfather_outcomes"):
        if r["decision_id"] in out:
            out[r["decision_id"]]["outcome"] = dict(r)
    return out


def _ok(a: dict | None) -> bool:
    return bool(a) and a["error"] is None and (a["expected_r"] is not None)


def _scorable(d: dict) -> bool:
    o = d["outcome"]
    return o is not None and o["actual_r"] is not None and all(_ok(d["arms"].get(a))
                                                               for a in ARMS)


def _week(ts: str) -> str:
    y, w, _ = datetime.fromisoformat(ts).isocalendar()
    return f"{y}-W{w:02d}"


# ------------------------------------------------------------------ entry


def entry_section(ds: list[dict]) -> dict:
    sc = [d for d in ds if _scorable(d)]
    out: dict = {"decisions": len(ds), "scorable": len(sc)}
    for arm in ARMS:
        a = [d["arms"][arm] for d in sc]
        o = [d["outcome"] for d in sc]
        err = [x["actual_r"] - y["expected_r"] for x, y in zip(o, a, strict=True)]
        mfe = [(x["mfe_r_after"] - y["expected_mfe_r"]) for x, y in zip(o, a, strict=True)
               if y["expected_mfe_r"] is not None and x["mfe_r_after"] is not None]
        mae = [(x["mae_r_after"] - y["expected_mae_r"]) for x, y in zip(o, a, strict=True)
               if y["expected_mae_r"] is not None and x["mae_r_after"] is not None]
        by_stance: dict = defaultdict(list)
        for x, y in zip(o, a, strict=True):
            by_stance[y["stance"]].append(x["actual_r"])
        out[arm] = {
            "mean_expected_r": _mean([y["expected_r"] for y in a]),
            "mean_actual_r": _mean([x["actual_r"] for x in o]),
            "bias_actual_minus_expected_r": _mean(err),
            "mean_abs_error_r": _mean([abs(e) for e in err]),
            "brier_p_win": _mean([(y["p_win"] - (x["actual_r"] > 0)) ** 2
                                  for x, y in zip(o, a, strict=True)]),
            "mfe_error_r": _mean(mfe),
            "mae_error_r": _mean(mae),
            "as_expected_rate": _rate([abs(e) <= AS_EXPECTED_R for e in err]),
            "stance_mix": dict(Counter(y["stance"] for y in a)),
            "actual_r_by_stance": {k: {"n": len(v), "mean_r": _mean(v)}
                                   for k, v in by_stance.items()},
        }
    days = [d["decided_at"][:10] for d in sc]
    d_abs = [abs(d["outcome"]["actual_r"] - d["arms"]["WITH"]["expected_r"])
             - abs(d["outcome"]["actual_r"] - d["arms"]["WITHOUT"]["expected_r"]) for d in sc]
    d_brier = [(d["arms"]["WITH"]["p_win"] - (d["outcome"]["actual_r"] > 0)) ** 2
               - (d["arms"]["WITHOUT"]["p_win"] - (d["outcome"]["actual_r"] > 0)) ** 2
               for d in sc]
    out["with_minus_without"] = {
        "abs_error_r (negative = WITH predicts better)": _cluster_ci(d_abs, days),
        "brier (negative = WITH better)": _cluster_ci(d_brier, days),
    }
    aa = [d for d in sc if _ok(d["arms"].get("WITHOUT_REPLICATE"))]
    out["ai_noise"] = {
        "aa_pairs": len(aa),
        "replicate_abs_diff_expected_r": _mean([abs(d["arms"]["WITHOUT"]["expected_r"]
                                                    - d["arms"]["WITHOUT_REPLICATE"]["expected_r"])
                                                for d in aa]),
        "with_abs_diff_expected_r_same_rows": _mean([abs(d["arms"]["WITHOUT"]["expected_r"]
                                                         - d["arms"]["WITH"]["expected_r"])
                                                     for d in aa]),
        "replicate_stance_flip_rate": _rate([d["arms"]["WITHOUT"]["stance"]
                                             != d["arms"]["WITHOUT_REPLICATE"]["stance"]
                                             for d in aa]),
        "with_stance_flip_rate_same_rows": _rate([d["arms"]["WITHOUT"]["stance"]
                                                  != d["arms"]["WITH"]["stance"] for d in aa]),
    }
    # stance is advisory; what WOULD skipping DOUBTFUL do - reported, never applied
    for arm in ARMS:
        keep = [d["outcome"]["actual_r"] for d in sc if d["arms"][arm]["stance"] != "DOUBTFUL"]
        out[arm]["if_doubtful_skipped_NOT_APPLIED"] = {
            "trades_kept_share": round(len(keep) / len(sc), 3) if sc else None,
            "expectancy_r_kept": _mean(keep),
            "expectancy_r_all": _mean([d["outcome"]["actual_r"] for d in sc]),
        }
    return out


# ------------------------------------------------------------------ management


def _cf(rec: str, o: dict) -> float:
    return o["at_decision_r"] if rec == "EXIT" else o["actual_r"]


def management_section(ds: list[dict]) -> dict:
    sc = [d for d in ds if _scorable(d) and all(d["arms"][a]["recommendation"] for a in ARMS)]
    out: dict = {"decisions": len(ds), "scorable": len(sc),
                 "positions": len({d["position_id"] for d in sc})}
    for arm in ARMS:
        rows = [(d["arms"][arm], d["outcome"]) for d in sc]
        ex = [(a, o) for a, o in rows if a["recommendation"] == "EXIT"]
        hold = [(a, o) for a, o in rows if a["recommendation"] != "EXIT"]
        out[arm] = {
            "expectancy_r": _mean([_cf(a["recommendation"], o) for a, o in rows]),
            "pnl_usdt_total": round(sum(_cf(a["recommendation"], o) * o["risk_usdt"]
                                        for a, o in rows), 2) if rows else None,
            "recommendation_mix": dict(Counter(a["recommendation"] for a, _ in rows)),
            "exit_right": sum(o["hold_r"] < 0 for _, o in ex),
            "exit_should_have_held": sum(o["hold_r"] > 0 for _, o in ex),
            "hold_should_have_exited": sum(o["hold_r"] < 0 for _, o in hold),
            "hold_right": sum(o["hold_r"] >= 0 for _, o in hold),
            "mfe_given_away_after_exit_r": _mean([o["mfe_r_after"] for _, o in ex]),
            "mae_sat_through_after_hold_r": _mean([o["mae_r_after"] for _, o in hold]),
            "prediction_bias_r": _mean([o["actual_r"] - a["expected_r"] for a, o in rows]),
            "prediction_abs_error_r": _mean([abs(o["actual_r"] - a["expected_r"])
                                             for a, o in rows]),
            "brier_p_win": _mean([(a["p_win"] - (o["actual_r"] > 0)) ** 2 for a, o in rows]),
        }
    pos = [d["position_id"] for d in sc]
    diff = [_cf(d["arms"]["WITH"]["recommendation"], d["outcome"])
            - _cf(d["arms"]["WITHOUT"]["recommendation"], d["outcome"]) for d in sc]
    d_abs = [abs(d["outcome"]["actual_r"] - d["arms"]["WITH"]["expected_r"])
             - abs(d["outcome"]["actual_r"] - d["arms"]["WITHOUT"]["expected_r"]) for d in sc]
    ver = [i for i, d in enumerate(sc) if d["outcome"]["verified"]]
    out["with_minus_without"] = {
        "expectancy_r": _cluster_ci(diff, pos),
        "expectancy_r_verified_exits_only": _cluster_ci([diff[i] for i in ver],
                                                        [pos[i] for i in ver]),
        "prediction_abs_error_r (negative = WITH better)": _cluster_ci(d_abs, pos),
    }
    aa = [d for d in sc if (d["arms"].get("WITHOUT_REPLICATE") or {}).get("recommendation")]
    b = sum(d["arms"]["WITH"]["recommendation"] != d["arms"]["WITHOUT"]["recommendation"]
            and d["arms"]["WITHOUT_REPLICATE"]["recommendation"]
            == d["arms"]["WITHOUT"]["recommendation"] for d in aa)
    c = sum(d["arms"]["WITH"]["recommendation"] == d["arms"]["WITHOUT"]["recommendation"]
            and d["arms"]["WITHOUT_REPLICATE"]["recommendation"]
            != d["arms"]["WITHOUT"]["recommendation"] for d in aa)
    noise = sum(d["arms"]["WITHOUT_REPLICATE"]["recommendation"]
                != d["arms"]["WITHOUT"]["recommendation"] for d in aa)
    changed = sum(d["arms"]["WITH"]["recommendation"] != d["arms"]["WITHOUT"]["recommendation"]
                  for d in sc)
    out["ai_noise"] = {
        "aa_pairs": len(aa),
        "replicate_changed_rate": _rate([True] * noise + [False] * (len(aa) - noise)),
        "replicate_changed_ci95": _wilson(noise, len(aa)),
        "with_changed_rate": round(changed / len(sc), 3) if sc else None,
        "with_changed_ci95": _wilson(changed, len(sc)),
        "mcnemar_on_aa_rows": {"only_evidence_changed": b, "only_replicate_changed": c,
                               "p_two_sided": _binom_two_sided(b, c)},
    }
    return out


# ------------------------------------------------------------------ attribution


def attribution_section(entries: list[dict]) -> dict:
    """Why did a closed trade end where it did? One row per trade."""
    sc = [d for d in entries if _scorable(d)]
    cats: Counter = Counter()
    inv: Counter = Counter()
    as_exp = {arm: Counter() for arm in ARMS}
    for d in sc:
        o = d["outcome"]
        good_entry = (o["mfe_r_after"] or 0) >= GOOD_ENTRY_MFE_R
        if o["actual_r"] > 0:
            cat = "GOOD_OUTCOME"
        elif good_entry:
            cat = "BAD_MANAGEMENT_AFTER_REASONABLE_ENTRY"
        elif o["actual_r"] < -1.1:
            cat = "BOTH_BAD_ENTRY_AND_LOSS_BEYOND_PLAN"
        else:
            cat = "BAD_ENTRY"
        cats[cat] += 1
        inv[f"{o['entry_verdict']}/{o['management_verdict']}"] += 1
        for arm in ARMS:
            ok = abs(o["actual_r"] - d["arms"][arm]["expected_r"]) <= AS_EXPECTED_R
            as_exp[arm][f"{cat}:{'AS_EXPECTED' if ok else 'NOT_AS_EXPECTED'}"] += 1
    return {
        "trades": len(sc),
        "rule": f"good entry = MFE after entry >= {GOOD_ENTRY_MFE_R} R; beyond plan = < -1.1 R;"
                f" as expected = |actual - expected| <= {AS_EXPECTED_R} R",
        "categories": dict(cats),
        "investigator_entry_management_verdicts": dict(inv),
        "as_expected_by_arm": {a: dict(c) for a, c in as_exp.items()},
    }


# ------------------------------------------------------------------ over time


def weekly_section(entries: list[dict], mgmt: list[dict]) -> dict:
    weeks: dict = defaultdict(lambda: defaultdict(list))
    for d in entries:
        if _scorable(d):
            w = weeks[_week(d["decided_at"])]
            for arm in ARMS:
                w[f"entry_abs_error_{arm}"].append(
                    abs(d["outcome"]["actual_r"] - d["arms"][arm]["expected_r"]))
            w["entry_actual_r"].append(d["outcome"]["actual_r"])
    for d in mgmt:
        if _scorable(d) and all(d["arms"][a]["recommendation"] for a in ARMS):
            w = weeks[_week(d["decided_at"])]
            for arm in ARMS:
                w[f"mgmt_cf_r_{arm}"].append(_cf(d["arms"][arm]["recommendation"], d["outcome"]))
    return {
        k: {m: {"n": len(v), "mean": _mean(v)} for m, v in sorted(w.items())}
        for k, w in sorted(weeks.items())
    }


def report(sh: sqlite3.Connection) -> dict:
    data = load(sh)
    entries = [d for d in data.values() if d["kind"] == "ENTRY"]
    mgmt = [d for d in data.values() if d["kind"] == "MANAGEMENT"]
    rows = list(sh.execute("SELECT arm, error, evidence_available, live, cost_usd"
                           " FROM godfather_decisions"))
    e, m = entry_section(entries), management_section(mgmt)
    return {
        "coverage": {
            "decision_rows": len(rows),
            "errors": sum(r["error"] is not None for r in rows),
            "with_arm_evidence_available_share": _rate(
                [bool(r["evidence_available"]) for r in rows if r["arm"] == "WITH"]),
            "live_share": _rate([bool(r["live"]) for r in rows if r["arm"] == "WITH"]),
            "ai_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 2),
        },
        "entry": e,
        "management": m,
        "attribution": attribution_section(entries),
        "weekly": weekly_section(entries, mgmt),
        "sufficient_data": {
            "required": {"paired_management": MIN_PAIRED, "aa_pairs": MIN_AA,
                         "closed_trades": MIN_TRADES},
            "have": {"paired_management": m["scorable"],
                     "aa_pairs": m["ai_noise"]["aa_pairs"] + e["ai_noise"]["aa_pairs"],
                     "closed_trades": e["scorable"]},
        },
        "not_applied_note": "Everything here is measurement. Evidence stays context: no REJECT, "
                            "no size, no leverage, no SL/TP, no Guardian authority, no Safety "
                            "Kernel, no cap change.",
    }


def main() -> None:
    s = get_settings()
    out = report(_ro(s.evidence.shadow_db))
    out["sufficient_data"]["met"] = (
        out["sufficient_data"]["have"]["paired_management"] >= MIN_PAIRED
        and out["sufficient_data"]["have"]["aa_pairs"] >= MIN_AA
        and out["sufficient_data"]["have"]["closed_trades"] >= MIN_TRADES
    )
    print(json.dumps(out, indent=1))
    Path("data/entry_research/godfather_evidence_report.json").write_text(
        json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
    sys.exit(0)

