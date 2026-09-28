"""Counterfactual entry analysis for LIVE trades - pre-entry information only.

Every alternative is a RULE fixed before looking at the outcome (reject if a
decision-time feature is in its TRAIN tertile; delay the entry N minutes) and
is applied to ALL verified LIVE trades, winners included. Judging a rule only
on the losers it would have avoided is hindsight, so it is never done here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from crypto_trading.entry_research.dataset import FEE_RT, STOP_SLIP, closed_before
from crypto_trading.shadow.evaluation import simulate_trade

DELAYS_MIN = (15, 30, 60)
# Pre-registered reject questions from the brief (feature, tertile side).
REJECT_RULES = {
    "OVEREXTENDED_4H": ("ret_4h", "HIGH"),
    "AT_4H_HIGH": ("pos_4h", "HIGH"),
    "BTC_4H_WEAK": ("btc_ret_4h", "LOW"),
    "WEAK_SIGNAL_SCORE": ("candidate_score", "LOW"),
    "RAN_BEFORE_FILL": ("drift_to_fill_pct", "HIGH"),
    "LOW_VOLUME_Z": ("volz_30m", "LOW"),
    "STALE_SIGNAL": ("signal_age_min", "HIGH"),
    "LOW_RR": ("rr", "LOW"),
}


def _in(feat: dict, rule: tuple, cuts: dict) -> bool | None:
    f, side = rule
    v = feat.get(f)
    if v is None or f not in cuts:
        return None
    lo, hi = cuts[f]
    return v <= lo if side == "LOW" else v > hi


def delayed_r(row: dict, bars, delay_min: int) -> float | None:
    """Own stop/target, entry delayed by N minutes after the real fill. If the
    price has already left the bracket by then, the trade is skipped (0 R)."""
    live = row["live"]
    filled = datetime.fromisoformat(live["entry_filled_at"])
    stop, target, entry0 = row["own_stop_live"], row["own_target_live"], live["entry_fill"]
    if None in (stop, target, entry0) or entry0 <= stop:
        return None
    # if stop or target was hit before the delayed entry, the plan is dead
    before = [b for b in bars if filled <= b.t < filled + timedelta(minutes=delay_min)]
    if any(b.l <= stop or b.h >= target for b in before):
        return 0.0
    after = [b for b in bars if b.t >= filled + timedelta(minutes=delay_min)]
    if not after:
        return None
    entry = after[0].o
    res = simulate_trade(after, entry, stop, target, FEE_RT, STOP_SLIP)
    if res is None:
        return 0.0
    # express in the ORIGINAL planned risk so the numbers are comparable
    return (res.exit_price - entry - FEE_RT * entry) / (entry0 - stop)


def actual_like_r(row: dict, bars) -> float | None:
    """Same simulator on the real fill with 0 delay - the reference the
    delayed variants are compared against (removes simulator bias)."""
    return delayed_r(row, bars, 0)


def analyse(live_rows: list[dict], cuts: dict, cache) -> dict:
    per_trade, rule_totals = [], {k: {"blocked": 0, "blocked_losers": 0, "saved_r": 0.0,
                                      "missed_r": 0.0, "unknown": 0} for k in REJECT_RULES}
    delay_totals = {d: {"n": 0, "sum_delta_r": 0.0} for d in DELAYS_MIN}
    for row in live_rows:
        r = row["live"]["r_actual"]
        if r is None:
            continue
        flags = {name: _in(row["feat"], rule, cuts) for name, rule in REJECT_RULES.items()}
        for name, hit in flags.items():
            t = rule_totals[name]
            if hit is None:
                t["unknown"] += 1
            elif hit:
                t["blocked"] += 1
                if r < 0:
                    t["blocked_losers"] += 1
                    t["saved_r"] += -r
                else:
                    t["missed_r"] += r
        filled = datetime.fromisoformat(row["live"]["entry_filled_at"])
        bars = cache.bars(row["symbol"], filled - timedelta(minutes=5), filled + timedelta(hours=7, minutes=30))
        base = actual_like_r(row, bars)
        delays = {}
        for d in DELAYS_MIN:
            v = delayed_r(row, bars, d)
            delays[d] = v
            if v is not None and base is not None:
                delay_totals[d]["n"] += 1
                delay_totals[d]["sum_delta_r"] += v - base
        pre = closed_before(bars, filled)
        per_trade.append({
            "symbol": row["symbol"], "t0": row["t0"].isoformat(), "r_actual": r,
            "net_usdt": row["live"]["net_usdt"], "exit": row["live"]["exit_reason"],
            "flags": flags, "sim_r_at_fill": base, "delayed_r": delays,
            "ret_4h": row["feat"].get("ret_4h"), "pos_4h": row["feat"].get("pos_4h"),
            "btc_ret_4h": row["feat"].get("btc_ret_4h"), "candidate_score": row["feat"].get("candidate_score"),
            "drift_to_fill_pct": row["feat"].get("drift_to_fill_pct"), "bars_before_fill": len(pre),
        })
    for t in rule_totals.values():
        t["net_effect_r"] = t["saved_r"] - t["missed_r"]
    return {"trades": per_trade, "rules": rule_totals, "delays": delay_totals}
