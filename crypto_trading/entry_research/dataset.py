"""One row per discovered candidate: decision-time features + standardized
outcomes + (when traded) the actual LIVE result.

No look-ahead: every feature uses only bars that had CLOSED before the signal
time t0 (candidate created_at), except `drift_to_fill_pct`, which uses bars
closed before the (later) entry moment and is therefore known at fill time.

Standardized outcome (pre-registered 2026-09-28, never tuned): entry at the
open of the first 1m bar at/after t0 + latency; stop = entry - 2 x ATR15,
target = entry + 3 x ATR15 (ATR15 = mean 15m range of the 16 closed 15m bars
before t0), 6 h limit, real costs (0.10 % round trip, 0.15 % stop slippage).
Primary latency = the median real LIVE signal->fill latency (23 min); 1 min is
reported as "what a fast pipeline would get".
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from crypto_trading.shadow.evaluation import Bar, simulate_trade

PRIMARY_LATENCY_MIN = 23
FAST_LATENCY_MIN = 1
STOP_ATR = 2.0
TARGET_ATR = 3.0
HORIZON_MIN = 360
FEE_RT = 0.001
STOP_SLIP = 0.0015
INDEPENDENCE_HOURS = 6
# Time split (pre-registered). The 20-26/9 gap has no candidates.
VALID_FROM = datetime(2026, 9, 13, tzinfo=UTC)
TEST_FROM = datetime(2026, 9, 26, tzinfo=UTC)


def period_of(t0: datetime) -> str:
    if t0 < VALID_FROM:
        return "TRAIN"
    return "VALID" if t0 < TEST_FROM else "TEST"


# --------------------------------------------------------------------------- bars

def closed_before(bars: list[Bar], moment: datetime) -> list[Bar]:
    return [b for b in bars if b.t + timedelta(minutes=1) <= moment]


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    buckets: dict[datetime, list[Bar]] = {}
    for b in bars:
        key = b.t - timedelta(minutes=b.t.minute % minutes, seconds=b.t.second)
        buckets.setdefault(key, []).append(b)
    out = []
    for key in sorted(buckets):
        group = buckets[key]
        if len(group) < minutes * 0.8:  # incomplete bucket is not a closed bar
            continue
        out.append(Bar(key, group[0].o, max(b.h for b in group), min(b.l for b in group), group[-1].c))
    return out


def _ret(bars: list[Bar], minutes: int) -> float | None:
    if len(bars) < minutes:
        return None
    return (bars[-1].c / bars[-minutes].o - 1) * 100


def atr15_pct(pre: list[Bar]) -> float | None:
    """`pre` must already be cut at t0; the last (possibly running) 15m bucket
    is dropped by `resample` when incomplete."""
    m15 = resample(pre, 15)[-16:]
    if len(m15) < 12:
        return None
    return sum((b.h - b.l) / b.c for b in m15) / len(m15) * 100


def price_features(pre: list[Bar]) -> dict:
    last4h = pre[-240:]
    feats = {
        "ret_15m": _ret(pre, 15), "ret_1h": _ret(pre, 60), "ret_4h": _ret(pre, 240),
        "atr15_pct": atr15_pct(pre), "pos_4h": None,
    }
    if len(last4h) >= 200:
        hi, lo = max(b.h for b in last4h), min(b.l for b in last4h)
        feats["pos_4h"] = (pre[-1].c - lo) / (hi - lo) if hi > lo else None
    return feats


# --------------------------------------------------------------------------- outcomes

def entry_bars(bars: list[Bar], entry_at: datetime) -> list[Bar]:
    return [b for b in bars if b.t >= entry_at]


def standardized_outcome(after: list[Bar], atr_pct: float | None) -> dict | None:
    if not after or not atr_pct:
        return None
    entry = after[0].o
    stop = entry * (1 - STOP_ATR * atr_pct / 100)
    target = entry * (1 + TARGET_ATR * atr_pct / 100)
    res = simulate_trade(after, entry, stop, target, FEE_RT, STOP_SLIP, horizon_minutes=HORIZON_MIN)
    if res is None:
        return None
    out = asdict(res)
    out["risk_pct"] = STOP_ATR * atr_pct
    out["minutes_to_mfe"] = minutes_to_mfe(after, entry, res.minutes)
    return out


def own_bracket_outcome(after: list[Bar], stop: float | None, target: float | None) -> dict | None:
    if not after or stop is None or target is None:
        return None
    res = simulate_trade(after, after[0].o, stop, target, FEE_RT, STOP_SLIP, horizon_minutes=HORIZON_MIN)
    return asdict(res) if res else None


def minutes_to_mfe(after: list[Bar], entry: float, held_minutes: float) -> float:
    best, best_t = entry, 0.0
    start = after[0].t
    for b in after:
        m = (b.t - start).total_seconds() / 60
        if m > held_minutes:
            break
        if b.h > best:
            best, best_t = b.h, m
    return best_t


def forward_returns(after: list[Bar]) -> dict:
    """Net of the round-trip fee, in %."""
    out = {}
    if not after:
        return out
    entry, start = after[0].o, after[0].t
    for h in (60, 120, 240, 360):
        inside = [b for b in after if b.t < start + timedelta(minutes=h)]
        if len(inside) >= h * 0.9:
            out[f"fwd_{h}m_pct"] = (inside[-1].c / entry - 1 - FEE_RT) * 100
    return out


# --------------------------------------------------------------------------- DB

def _json(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _entropy(probs: list[float]) -> float | None:
    ps = [p for p in probs if p and p > 0]
    if not ps:
        return None
    return -sum(p * math.log(p) for p in ps) / math.log(3)


def load_rows(db_path: str, candidate_ids: list[str] | None = None) -> list[dict]:
    """All candidates, or only `candidate_ids` (the forward shadow tick)."""
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    wanted = set(candidate_ids) if candidate_ids is not None else None
    assessments: dict[str, dict] = {}
    for r in c.execute("SELECT candidate_id, field_name, payload FROM assessments"):
        assessments.setdefault(r["candidate_id"], {})[r["field_name"]] = _json(r["payload"])
    gate = {r["candidate_id"]: (r["outcome"], _json(r["detail_json"]) or {})
            for r in c.execute("SELECT candidate_id, outcome, detail_json FROM gate_evaluations")}
    eq = {r["candidate_id"]: (r["verdict"], _f(r["quality_score"]))
          for r in c.execute("SELECT candidate_id, verdict, quality_score FROM godfather_entry_quality")}
    live = {}
    for r in c.execute(
        "SELECT p.candidate_id, p.stop_loss, p.target, l.* FROM live_executions l"
        " JOIN positions p USING(position_id) WHERE l.phase='CLOSED'"
    ):
        live[r["candidate_id"]] = dict(r)
    run_heat: dict[str, int] = {}
    for r in c.execute("SELECT discovery_run_id, count(*) n FROM candidates GROUP BY 1"):
        run_heat[r["discovery_run_id"]] = r["n"]

    rows = []
    for r in c.execute("SELECT * FROM candidates ORDER BY created_at"):
        if wanted is not None and r["candidate_id"] not in wanted:
            continue
        ev = _json(r["evidence_record"]) or {}
        a = assessments.get(r["candidate_id"], {})
        g_out, g = gate.get(r["candidate_id"], (None, {}))
        metrics = g.get("metrics", {})
        fc = (a.get("forecast") or {}).get("scenario_probabilities") or {}
        risk = a.get("risk") or {}
        qa = a.get("qa") or {}
        sec = ev.get("secondary_timeframe_evidence") or {}
        triggers = ev.get("trigger_reasons") or []
        sec_trig = [k for k in ("price_volatility", "momentum_breakout", "volume", "funding_oi")
                    if (sec.get(f"{k}_evidence") or {}).get("triggered")]
        ref = _f(r["reference_price"]) or _f(metrics.get("reference_price"))
        stop, target = _f(risk.get("suggested_stop_loss")), _f(risk.get("suggested_target"))
        lv = live.get(r["candidate_id"])
        # backfilled rows store the outcome that REALLY happened (pre-P1), so
        # "P1 would block" = really CONFIRMED + P1 check failed; live rows
        # already carry the P1 result.
        if g.get("backfilled"):
            p1_block = bool(g.get("enforced_failed")) and g_out == "CONFIRMED"
        else:
            p1_block = bool(g.get("enforced_failed")) and g_out != "CONFIRMED"
        if lv is not None:
            cohort = "LIVE"
        elif p1_block:
            cohort = "P1_BLOCKED"
        elif r["status"] == "CONFIRMED":
            cohort = "CONFIRMED_NOT_LIVE"
        elif r["status"] in ("REJECTED", "NO_TRADE"):
            cohort = "AI_NOT_CONFIRMED"
        else:
            cohort = "NOT_ANALYSED"
        bull, bear, neu = _f(fc.get("bullish")), _f(fc.get("bearish")), _f(fc.get("neutral"))

        def evv(k, src=ev):
            return _f((src.get(f"{k}_evidence") or {}).get("value"))

        rows.append({
            "candidate_id": r["candidate_id"], "symbol": r["instrument"],
            "t0": datetime.fromisoformat(r["created_at"]), "status": r["status"], "cohort": cohort,
            "p1_would_block": p1_block, "p1_reasons": g.get("enforced_failed") or [],
            "discovery_run_id": r["discovery_run_id"],
            "feat": {
                "candidate_score": _f(ev.get("candidate_score")),
                "rsi_30m": evv("momentum_breakout"), "rsi_1h": evv("momentum_breakout", sec),
                "chg_30m": evv("price_volatility"), "chg_1h": evv("price_volatility", sec),
                "volz_30m": evv("volume"), "volz_1h": evv("volume", sec),
                "funding_pct": evv("funding_oi"),
                "trig_volume": "volume" in triggers, "trig_momentum": "momentum_breakout" in triggers,
                "trig_pricevol": "price_volatility" in triggers, "trig_funding": "funding_oi" in triggers,
                "n_triggers": len(triggers),
                "tf_confirmed": bool(set(triggers) & set(sec_trig)),
                "run_heat": run_heat.get(r["discovery_run_id"]),
                "hour_utc": datetime.fromisoformat(r["created_at"]).hour,
                # AI (analysed candidates only)
                "fc_bull": bull, "fc_bear": bear, "fc_bull_minus_bear": (bull - bear) if None not in (bull, bear) else None,
                "fc_entropy": _entropy([bull, bear, neu]) if None not in (bull, bear, neu) else None,
                "opp_score": _f((a.get("opportunity_screen") or {}).get("opportunity_score")),
                "rr": _f(metrics.get("risk_reward")) or (
                    (target - ref) / (ref - stop) if None not in (stop, target, ref) and ref > stop else None),
                "stop_dist_pct": (ref - stop) / ref * 100 if None not in (stop, ref) and ref else None,
                "qa_violations": len(qa.get("violations") or []) if qa else None,
                "bear_counterargs": len((a.get("bear_adversarial") or {}).get("counterarguments") or []) or None,
                "signal_age_min": _f(metrics.get("signal_age_minutes")),
                "gf_eq_trade": (eq[r["candidate_id"]][0] == "TRADE") if r["candidate_id"] in eq else None,
                "gf_eq_score": eq.get(r["candidate_id"], (None, None))[1],
            },
            "own_stop": stop, "own_target": target, "reference_price": ref,
            "live": _live_summary(lv),
            "own_stop_live": _f(lv["stop_loss"]) if lv else None,
            "own_target_live": _f(lv["target"]) if lv else None,
        })
    return rows


def _live_summary(lv: dict | None) -> dict | None:
    if lv is None:
        return None
    entry = _f(lv.get("exchange_fill_entry"))
    qty = _f(lv.get("entry_quantity"))
    stop = _f(lv.get("stop_loss"))
    pnl = _f(lv.get("exchange_realized_pnl_usdt"))
    fees, funding = _f(lv.get("realized_fees_usdt")), _f(lv.get("realized_funding_usdt"))
    risk = (entry - stop) * qty if None not in (entry, stop, qty) and entry > stop else None
    # same definition as repository.verified_live_result: fees/funding are costs
    net = None
    if pnl is not None and lv.get("exit_verification") == "VERIFIED":
        net = pnl - (fees or 0) - (funding or 0)
    return {
        "verification": lv.get("exit_verification"), "exit_reason": lv.get("exit_reason"),
        "entry_fill": entry, "qty": qty, "pnl_usdt": pnl, "fees_usdt": fees, "funding_usdt": funding,
        "net_usdt": net, "planned_risk_usdt": risk,
        "r_actual": (net / risk) if (net is not None and risk) else None,
        "entry_filled_at": lv.get("entry_filled_at") or lv.get("claimed_at"),
    }


# --------------------------------------------------------------------------- build

def mark_independent(rows: list[dict]) -> None:
    """First signal per symbol per 6 h window: later ones overlap the same
    move and the bot could not hold the symbol twice anyway."""
    last: dict[str, datetime] = {}
    for row in sorted(rows, key=lambda x: x["t0"]):
        prev = last.get(row["symbol"])
        row["independent"] = prev is None or row["t0"] - prev >= timedelta(hours=INDEPENDENCE_HOURS)
        if row["independent"]:
            last[row["symbol"]] = row["t0"]


def enrich(row: dict, cache) -> None:
    t0 = row["t0"]
    bars = cache.bars(row["symbol"], t0 - timedelta(hours=4, minutes=20), t0 + timedelta(minutes=HORIZON_MIN + 30))
    btc = cache.bars("BTC-USDT", t0 - timedelta(hours=4, minutes=20), t0)
    pre = closed_before(bars, t0)
    btc_pre = closed_before(btc, t0)
    pf = price_features(pre)
    row["feat"].update(pf)
    row["feat"].update({
        "btc_ret_1h": _ret(btc_pre, 60), "btc_ret_4h": _ret(btc_pre, 240), "btc_atr15_pct": atr15_pct(btc_pre),
    })
    row["bars_ok"] = len(pre) >= 200 and bool(bars)
    row["outcomes"] = {}
    for name, lat in (("primary", PRIMARY_LATENCY_MIN), ("fast", FAST_LATENCY_MIN)):
        entry_at = t0 + timedelta(minutes=lat)
        after = entry_bars(bars, entry_at)
        std = standardized_outcome(after, pf["atr15_pct"])
        row["outcomes"][name] = std
        if name == "primary":
            row["fwd"] = forward_returns(after)
            row["own"] = own_bracket_outcome(after, row["own_stop"], row["own_target"])
            before_fill = closed_before(bars, entry_at)
            row["feat"]["drift_to_fill_pct"] = (
                (after[0].o / pre[-1].c - 1) * 100 if after and pre and before_fill else None)
