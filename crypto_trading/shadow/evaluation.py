"""P3-P6 shadow evaluation (2026-09-28).

For every candidate the Gate evaluated, once its 6 h outcome window has
passed, this records:

- decision-time features, computed ONLY from exchange bars that had closed
  before the Gate decision (BTC regime, symbol momentum/volatility), plus
  what was already known then (volume confirmation, forecast flags,
  same-symbol re-entry, GODFATHER's entry-quality verdict);
- the hypothetical action of every shadow veto rule (would it have blocked?);
- the outcome of the candidate's own stop/target bracket on 1m bars with the
  real cost model, and of the break-even (P4) and trailing (P5) variants;
- the actual LIVE result when the candidate was really traded.

Shadow means shadow: this module holds no trading connector, never touches
a candidate, position or order, and only writes `shadow_evaluations`.
Thresholds here are natural zero-points (sign of a return, the pipeline's own
volume trigger, a verdict); continuous features are cut into tertiles on the
TRAIN half inside `shadow/report.py`, never tuned here.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from crypto_trading.logging import log_event, new_run_id
from crypto_trading.shadow.history import backfill_gate_evaluations

HORIZON_MINUTES = 360
# P4 break-even grid: the stop moves to entry x (1 + BE_OFFSET) - above the
# measured round trip (0.10 %) + stop slippage (0.15 %) - once the trade has
# reached the trigger. +0.5 % is deliberately NOT in the grid (user P4).
BE_TRIGGERS_PCT = (0.75, 1.0, 1.5, 2.0)
BE_OFFSET_PCT = 0.25
# P5 trailing grid (shadow only - never LIVE without independent OOS evidence).
TRAIL_VARIANTS = ((1.0, 0.5), (1.5, 0.5), (1.0, 0.33), (2.0, 0.5))


@dataclass(frozen=True)
class Bar:
    t: datetime
    o: float
    h: float
    l: float  # noqa: E741 - OHLC
    c: float


@dataclass(frozen=True)
class TradeResult:
    exit_price: float
    reason: str  # TP | SL | BE | TRAIL | TIME
    r: float
    mfe_pct: float
    mae_pct: float
    minutes: float


def simulate_trade(
    bars: list[Bar], entry: float, stop: float, target: float, fee_rt: float, stop_slip: float,
    horizon_minutes: int = HORIZON_MINUTES, be_trigger_pct: float | None = None,
    be_offset_pct: float = BE_OFFSET_PCT, trail_start_pct: float | None = None,
    trail_fraction: float | None = None,
) -> TradeResult | None:
    """Minute by minute, no intrabar lookahead: within one bar the stop is
    checked before the target (conservative), and a stop move triggered by a
    bar only applies from the NEXT bar."""
    if not bars or not (stop < entry < target):
        return None
    start = bars[0].t
    window = [b for b in bars if b.t < start + timedelta(minutes=horizon_minutes)]
    current_stop, label = stop, "SL"
    high, low = entry, entry
    exit_price = reason = None
    last = window[-1]
    for bar in window:
        high, low = max(high, bar.h), min(low, bar.l)
        if bar.l <= current_stop:
            exit_price, reason, last = current_stop * (1 - stop_slip), label, bar
            break
        if bar.h >= target:
            exit_price, reason, last = target, "TP", bar
            break
        if be_trigger_pct is not None and high >= entry * (1 + be_trigger_pct / 100):
            be = entry * (1 + be_offset_pct / 100)
            if be > current_stop:
                current_stop, label = be, "BE"
        if trail_start_pct is not None and high >= entry * (1 + trail_start_pct / 100):
            trail = entry + trail_fraction * (high - entry)
            if trail > current_stop:
                current_stop, label = trail, "TRAIL"
    if exit_price is None:
        exit_price, reason = last.c, "TIME"
    risk = entry - stop
    return TradeResult(
        exit_price=exit_price, reason=reason,
        r=(exit_price - entry - fee_rt * entry) / risk,
        mfe_pct=(high / entry - 1) * 100, mae_pct=(low / entry - 1) * 100,
        minutes=(last.t - start).total_seconds() / 60,
    )


def _closed_before(bars: list[Bar], moment: datetime) -> list[Bar]:
    return sorted((b for b in bars if b.t + timedelta(minutes=1) <= moment), key=lambda b: b.t)


def _ret(bars: list[Bar], minutes: int) -> float | None:
    if len(bars) < minutes:
        return None
    return (bars[-1].c / bars[-minutes].o - 1) * 100


def decision_features(symbol_bars: list[Bar], btc_bars: list[Bar], decided_at: datetime) -> dict:
    """Only bars that had CLOSED before `decided_at` are used."""
    sym = _closed_before(symbol_bars, decided_at)
    btc = _closed_before(btc_bars, decided_at)
    btc_1h, btc_4h = _ret(btc, 60), _ret(btc, 240)
    last_hour = sym[-60:] if len(sym) >= 60 else []
    return {
        "btc_ret_1h_pct": btc_1h,
        "btc_ret_4h_pct": btc_4h,
        "btc_falling": (btc_1h < 0 and btc_4h < 0) if btc_1h is not None and btc_4h is not None else None,
        "symbol_ret_1h_pct": _ret(sym, 60),
        "symbol_vol_1h_pct": (
            sum((b.h - b.l) / b.l * 100 for b in last_hour) / len(last_hour) if last_hour else None
        ),
    }


def veto_flags(
    features: dict, gate_metrics: dict, volume_confirmed: bool | None,
    reentry_within_6h: bool | None, gf_eq_verdict: str | None,
) -> dict:
    """True = the rule WOULD have blocked; None = could not be evaluated."""
    bull, bear = gate_metrics.get("bull_probability"), gate_metrics.get("bear_probability")
    neutral = gate_metrics.get("neutral_probability")
    known = None not in (bull, bear, neutral)
    return {
        "BEARISH_DOMINANT": (bear > bull and bear >= neutral) if known else None,
        "BULLISH_NOT_DOMINANT": (not (bull > bear and bull >= neutral)) if known else None,
        "ALT_LONG_WHILE_BTC_FALLING": features.get("btc_falling"),
        "NO_VOLUME_CONFIRMATION": (not volume_confirmed) if volume_confirmed is not None else None,
        "SAME_SYMBOL_REENTRY_6H": reentry_within_6h,
        "GODFATHER_EQ_NOT_TRADE": (gf_eq_verdict != "TRADE") if gf_eq_verdict is not None else None,
    }


# ---------------------------------------------------------------------------
# Tick: DB + read-only public market data. Writes only shadow_evaluations.
# ---------------------------------------------------------------------------

def _bars_from_rows(rows: list[dict]) -> list[Bar]:
    return [Bar(r["open_time"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for r in rows]


def _fetch(connector, symbol: str, start: datetime, end: datetime) -> list[Bar]:
    from crypto_trading.kline_archive import fetch_window

    return _bars_from_rows(fetch_window(connector, symbol, start, end))


def evaluate_candidate(repo, connector, row: dict, costs, now: datetime, oos_start: datetime) -> dict | None:
    """One gate-evaluated candidate -> one shadow record (None = not ready)."""
    decided = datetime.fromisoformat(row["evaluated_at"])
    if decided + timedelta(minutes=HORIZON_MINUTES + 10) > now:
        return None
    detail = json.loads(row["detail_json"])
    metrics = detail.get("metrics", {})
    candidate = repo.get_candidate(row["candidate_id"])
    if candidate is None:
        return None
    symbol = candidate.instrument
    pre_start = decided - timedelta(hours=4, minutes=5)
    post_end = decided + timedelta(minutes=HORIZON_MINUTES + 2)
    symbol_bars = _fetch(connector, symbol, pre_start, post_end)
    btc_bars = symbol_bars if symbol == "BTC-USDT" else _fetch(connector, "BTC-USDT", pre_start, decided)
    features = decision_features(symbol_bars, btc_bars, decided)
    after = [b for b in symbol_bars if b.t >= decided]
    evidence = candidate.evidence_record
    reentry = repo.had_position_on_symbol_within(symbol, decided - timedelta(hours=6), decided,
                                                 exclude=candidate.candidate_id)
    eq = repo.get_godfather_entry_quality_verdict(candidate.candidate_id)
    flags = veto_flags(features, metrics, evidence.volume_evidence.triggered, reentry, eq)
    record = {
        "candidate_id": candidate.candidate_id, "symbol": symbol, "decided_at": decided.isoformat(),
        "cohort": "OOS" if decided >= oos_start else "IN_SAMPLE_HISTORICAL",
        "gate_outcome": row["outcome"], "enforced_failed": detail.get("enforced_failed", []),
        "trigger_reasons": list(evidence.trigger_reasons), "candidate_score": evidence.candidate_score,
        "features": {**features, "risk_reward": metrics.get("risk_reward"),
                     "forecast_uncertainty": metrics.get("forecast_uncertainty"),
                     "volume_zscore": evidence.volume_evidence.value},
        "veto_flags": flags, "outcome": None, "variants": {}, "live": None,
    }
    try:
        stop, target = float(metrics["stop_loss"]), float(metrics["target"])
    except (KeyError, TypeError, ValueError):
        return record
    if after:
        entry = after[0].o
        fee, slip = float(costs.round_trip_fee_pct), float(costs.stop_slippage_pct)
        base = simulate_trade(after, entry, stop, target, fee, slip)
        record["outcome"] = asdict(base) if base else None
        for trigger in BE_TRIGGERS_PCT:
            v = simulate_trade(after, entry, stop, target, fee, slip, be_trigger_pct=trigger)
            record["variants"][f"BE_{trigger}"] = asdict(v) if v else None
        for start, fraction in TRAIL_VARIANTS:
            v = simulate_trade(after, entry, stop, target, fee, slip, trail_start_pct=start,
                               trail_fraction=fraction)
            record["variants"][f"TRAIL_{start}_{fraction}"] = asdict(v) if v else None
    record["live"] = repo.verified_live_result(candidate.candidate_id)
    return record


def run_shadow_evaluation_tick(repo, connector, settings, now: datetime, limit: int = 10) -> int:
    run_id = new_run_id()
    repo.start_run(run_id, "shadow_evaluation", now)
    done, errors = 0, []
    oos_start = datetime.fromisoformat(settings.shadow.oos_start)
    for row in repo.find_gate_evaluations_without_shadow(limit):
        try:
            record = evaluate_candidate(repo, connector, row, settings.costs, now, oos_start)
        except Exception as exc:  # noqa: BLE001 - one candidate never stops the batch
            errors.append(f"{row['candidate_id']}: {type(exc).__name__}: {exc}")
            continue
        if record is None:
            continue
        repo.save_shadow_evaluation(record, now)
        done += 1
    log_event(run_id, event="shadow_evaluation_tick", evaluated=done, errors=len(errors))
    repo.complete_run(run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors[:20])
    return done


def run_forever(repo, connector, settings, interval_seconds: int = 1800) -> None:
    try:  # historical replay rows (IN_SAMPLE), once per process; idempotent
        backfill_gate_evaluations(repo, settings.gate)
    except Exception as exc:  # noqa: BLE001
        log_event("shadow", event="shadow_backfill_failed", error_type=type(exc).__name__, error=str(exc))
    while True:
        try:
            run_shadow_evaluation_tick(repo, connector, settings, datetime.now(UTC), limit=50)
        except Exception as exc:  # noqa: BLE001
            log_event("shadow", event="shadow_evaluation_tick_failed",
                      error_type=type(exc).__name__, error=str(exc))
        time.sleep(interval_seconds)
