"""GODFATHER structured entry experience:

    signal -> context -> decision -> expected scenario -> actual outcome -> prediction error

Realized scenario = the 4 h forward return against +-1 ATR15 (bullish above,
bearish below, neutral in between) - the same horizon the Forecast Agent
states. Expected R = P(bull) x R:R - P(bear) x 1.
"""
from __future__ import annotations

SIGNAL_KEYS = ("trig_volume", "trig_momentum", "trig_pricevol", "trig_funding", "candidate_score",
               "rsi_30m", "rsi_1h", "volz_30m", "chg_30m", "chg_1h")
CONTEXT_KEYS = ("btc_ret_1h", "btc_ret_4h", "atr15_pct", "ret_4h", "pos_4h", "run_heat", "drift_to_fill_pct")


def realized_scenario(fwd_4h_pct: float | None, atr_pct: float | None) -> str | None:
    if fwd_4h_pct is None or not atr_pct:
        return None
    return "bullish" if fwd_4h_pct > atr_pct else "bearish" if fwd_4h_pct < -atr_pct else "neutral"


def chain_record(row: dict) -> dict:
    f = row["feat"]
    fwd = (row.get("fwd") or {}).get("fwd_240m_pct")
    realized = realized_scenario(fwd, f.get("atr15_pct"))
    expected, error = None, None
    if f.get("fc_bull") is not None:
        bear = f.get("fc_bear") or 0.0
        probs = {"bullish": f["fc_bull"], "bearish": bear, "neutral": max(0.0, 1 - f["fc_bull"] - bear)}
        exp_r = probs["bullish"] * f["rr"] - probs["bearish"] if f.get("rr") is not None else None
        expected = {"probs": probs, "expected_r": exp_r}
        own_r = (row.get("own") or {}).get("r")
        if realized is not None:
            error = {
                "brier": sum((p - (1.0 if k == realized else 0.0)) ** 2 for k, p in probs.items()),
                "direction_wrong": probs["bullish"] >= max(probs.values()) and realized == "bearish",
                "r_error": (own_r - exp_r) if None not in (own_r, exp_r) else None,
            }
    std = (row.get("outcomes") or {}).get("primary")
    live = row.get("live")
    return {
        "signal": {k: f.get(k) for k in SIGNAL_KEYS},
        "context": {k: f.get(k) for k in CONTEXT_KEYS},
        "decision": row.get("cohort"),
        "expected": expected,
        "actual": {"scenario": realized, "fwd_4h_pct": fwd, "std_r": std["r"] if std else None,
                   "own_r": (row.get("own") or {}).get("r"),
                   "live_r": live["r_actual"] if live else None},
        "prediction_error": error,
    }
