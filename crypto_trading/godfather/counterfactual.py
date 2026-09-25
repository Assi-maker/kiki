"""Counterfactual engine: what would a different decision have produced?

The user's requirement (2) is precise and the two hard parts are both
about honesty rather than cleverness:

**STRICT NO LOOKAHEAD.** A policy decides at moment T using only what
existed at T. That is enforced structurally here, not by review: the
engine walks the real path index by index and hands each policy a
`prefix` list - a copy of `points[: i + 1]`. The future is not merely
"not consulted", it is not in scope. `test_counterfactual.py` proves it
the only way that really counts: it mutates every point after the trigger
index and asserts the decision is byte-identical.

**Simulated results never contaminate real ones.** Results go to their
own table (`godfather_counterfactuals`) and carry `actual_pnl_usdt`
beside `simulated_pnl_usdt` so the two can never be confused for each
other. A position whose real P/L is unknown (a LIVE-mirrored close with
no PAPER exit data) produces NO counterfactual rows at all - there is
nothing to compare against, and an unanchored simulation is worse than
no simulation.

Costs are real. Every simulated exit goes through the SAME
`compute_fill_price`/`compute_fees`/`compute_funding` the live paper
engine uses (`paper_trading/execution.py`), with the same configured
spread/slippage/fee - so a policy cannot look good merely by trading more
often for free. That is requirement 9's "realistic costs and slippage"
and requirement 10's "transaction costs / unnecessary turnover", made
structural.

**Observed, unobservable, unavailable - never silently neutral.** Every
row carries `detail["observation_status"]`:

* OBSERVED - the simulated outcome rests on prices Guardian actually saw.
* UNOBSERVABLE - a stop, target or limit the policy would have had
  resting on the exchange spans a hole in Guardian's record longer than
  `stop_simulation.MAX_UNOBSERVED_MINUTES`. What it would have done in
  the hole is unknown, so `simulated_pnl_usdt` and `delta_pnl_usdt` are
  None and every aggregation skips the row.
* UNAVAILABLE - the counterfactual needs a price that cannot exist (e.g.
  NO_INTERVENTION for a trade Guardian closed early: the path ends at the
  intervention).

Zero-size (exposure-blocked) positions produce no rows at all: they carry
P/L 0 under every policy and would otherwise enter every sample as
dozens of perfectly "neutral" trades.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.godfather import stats
from crypto_trading.godfather.costs import implied_funding_rate, simulate_exit_pnl
from crypto_trading.godfather.mfe_model import MfeModel
from crypto_trading.godfather.path import PathPoint
from crypto_trading.godfather.position_decision import decide_position
from crypto_trading.godfather.stop_simulation import (
    MAX_UNOBSERVED_MINUTES,
    StopPolicy,
    StopSimulation,
    ThresholdRule,
    excursion,
    simulate_stop_rule,
)
from crypto_trading.godfather.thesis import (
    ThesisThresholds,
    build_thesis_features,
    classify_thesis_state,
    evaluate_thesis,
)
from crypto_trading.paper_trading.execution import (
    compute_fees,
    compute_fill_price,
    compute_funding,
    compute_pnl_or_none,
)
from crypto_trading.schemas.godfather import CounterfactualPolicy, CounterfactualResult
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")
_HALF = Decimal("0.5")

# TIGHTEN_SL_AFTER_FAVORABLE: break-even at +1%, deliberately the SAME
# threshold the live Profit Protection mechanism uses
# (`LiveExecutionConfig.profit_protection_threshold_pct`), so this measures
# the policy the system actually has. PROFIT_LOCK_HALF_MFE is the single
# profit-lock variant, pre-declared in the 2026-09-25 TIGHTEN_SL
# evaluation before its results were computed - no threshold search.
BREAK_EVEN_POLICY = StopPolicy(
    "TIGHTEN_SL_AFTER_FAVORABLE", Decimal("0.01"), _ZERO, "live", "live PP config"
)
PROFIT_LOCK_POLICY = StopPolicy(
    "PROFIT_LOCK_HALF_MFE", Decimal("0.01"), Decimal("0.5"), "hypothesis",
    "pre-declared 2026-09-25",
)

ENGINE_VERSION = 2

_DELAY_POLICIES: dict[str, float] = {
    "DELAY_ENTRY_30M": 30.0,
    "DELAY_ENTRY_60M": 60.0,
}


@dataclass(frozen=True)
class _Trigger:
    index: int
    minutes: float
    price: Decimal


def _sign(position: Position) -> Decimal:
    return Decimal("-1") if position.direction == "SHORT" else Decimal("1")


# ---------------------------------------------------------------------
# Policies. Each returns the index at which it fires given ONLY a prefix,
# or None. They are pure and take no `points` argument by design.
# ---------------------------------------------------------------------


def _thesis_state_at(
    position: Position, prefix: list[PathPoint], thresholds: ThesisThresholds
) -> str | None:
    features = build_thesis_features(position, prefix, thresholds.max_hold_hours)
    if features is None:
        return None
    state, _reasons = classify_thesis_state(features, thresholds)
    return state


def _fires_on_thesis(
    position: Position,
    prefix: list[PathPoint],
    thresholds: ThesisThresholds,
    states: tuple[str, ...],
) -> bool:
    return _thesis_state_at(position, prefix, thresholds) in states


def _fires_on_safe_tp(prefix: list[PathPoint]) -> bool:
    return prefix[-1].progress_ratio >= _HALF


def _first_trigger(
    position: Position,
    points: list[PathPoint],
    predicate,
) -> _Trigger | None:
    """The core no-lookahead loop. `predicate` is handed a COPY of the
    prefix and nothing else; there is no path by which it can observe
    `points[i + 1:]`."""
    for i in range(len(points)):
        prefix = list(points[: i + 1])
        if predicate(prefix):
            point = points[i]
            return _Trigger(index=i, minutes=point.minutes_in_trade, price=point.price)
    return None


# ---------------------------------------------------------------------
# Entry-side policies
# ---------------------------------------------------------------------


def _simulate_delayed_entry(
    position: Position,
    points: list[PathPoint],
    delay_minutes: float,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
) -> tuple[bool, Decimal | None, Decimal | None, dict]:
    """Enter `delay_minutes` later at the price then observed, keeping the
    SAME relative stop/target distances and the SAME absolute time limit,
    and run the remaining real path forward.

    Nothing about this needs the future either: the delayed entry price is
    a real observed price, and the exit scan walks forward from it in
    order.
    """
    entry_point = next((p for p in points if p.minutes_in_trade >= delay_minutes), None)
    if entry_point is None:
        return (False, None, None, {"reason": "path ended before the delay elapsed"})

    sign = _sign(position)
    original_entry = position.simulated_fill_entry
    if original_entry == _ZERO:
        return (False, None, None, {"reason": "degenerate entry price"})

    stop_distance = (position.stop_loss - original_entry) / original_entry
    target_distance = (position.target - original_entry) / original_entry
    new_entry = compute_fill_price(
        entry_point.price,
        position.direction,
        risk_limits.spread_pct,
        risk_limits.slippage_pct,
        "entry",
    )
    new_stop = new_entry * (Decimal("1") + stop_distance)
    new_target = new_entry * (Decimal("1") + target_distance)

    remaining = [p for p in points if p.minutes_in_trade > entry_point.minutes_in_trade]
    exit_point = None
    exit_label = "time_limit"
    for point in remaining:
        if sign * (point.price - new_stop) <= _ZERO:
            exit_point, exit_label = point, "stop_loss"
            break
        if sign * (point.price - new_target) >= _ZERO:
            exit_point, exit_label = point, "target"
            break
    if exit_point is None:
        if not remaining:
            return (False, None, None, {"reason": "no path observed after the delayed entry"})
        exit_point = remaining[-1]

    # The delayed trade's own stop and target rest on the exchange; a hole
    # in the record between its entry and its exit hides whether they hit.
    watched = [entry_point.minutes_in_trade] + [
        p.minutes_in_trade for p in remaining if p.minutes_in_trade <= exit_point.minutes_in_trade
    ]
    if _largest_gap(watched) > MAX_UNOBSERVED_MINUTES:
        return (False, None, None, {
            "observation_status": "UNOBSERVABLE",
            "reason": "hole in the record while the delayed trade was open",
        })

    fill = compute_fill_price(
        exit_point.price,
        position.direction,
        risk_limits.spread_pct,
        risk_limits.slippage_pct,
        "exit",
    )
    price_return = (fill - new_entry) / new_entry
    gross = position.size * price_return * sign
    fees = compute_fees(position.size, risk_limits.fee_pct)
    held = exit_point.minutes_in_trade - entry_point.minutes_in_trade
    held_hours = Decimal(str(max(0.0, held))) / Decimal("60")
    funding = compute_funding(position.size, funding_rate, held_hours)
    pnl = gross - fees - funding
    detail = {
        "delayed_entry_minutes": entry_point.minutes_in_trade,
        "delayed_entry_price": str(new_entry),
        "delayed_stop_loss": str(new_stop),
        "delayed_target": str(new_target),
        "simulated_exit_reason": exit_label,
        "simulated_hold_minutes": held,
    }
    return (True, exit_point.price, pnl, detail)


def _simulate_reduce(
    position: Position,
    points: list[PathPoint],
    trigger: _Trigger,
    actual_pnl: Decimal,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
) -> tuple[Decimal, dict]:
    """Half the exposure is realised at the trigger, half rides to the
    real exit. The surviving half's P/L is taken as half the REAL
    outcome, which is exact: `compute_pnl` is linear in `size`."""
    realised_half = simulate_exit_pnl(
        position,
        trigger.price,
        trigger.minutes,
        risk_limits,
        funding_rate,
        size=position.size * _HALF,
    )
    riding_half = actual_pnl * _HALF
    return (
        realised_half + riding_half,
        {
            "reduced_at_minutes": trigger.minutes,
            "realised_half_pnl_usdt": str(realised_half),
            "riding_half_pnl_usdt": str(riding_half),
        },
    )


def _counterfactual_id(position_id: str, policy: str) -> str:
    return hashlib.sha256(f"{position_id}:{policy}".encode()).hexdigest()


def _largest_gap(minutes: list[float]) -> float:
    return max((b - a for a, b in zip(minutes, minutes[1:], strict=False)), default=0.0)


def _close_minutes(position: Position) -> float | None:
    if position.closed_at is None:
        return None
    return (position.closed_at - position.opened_at).total_seconds() / 60


def _stop_row(sim: StopSimulation) -> tuple[bool, float | None, Decimal | None, dict]:
    """(triggered, trigger_minutes, simulated_pnl, detail) for a stop-type
    policy, with the unobservable case turned into a None outcome.

    `triggered` means the policy INTERVENED - it moved the stop - whether
    or not the moved stop was then hit. A moved stop that is never hit is
    still an intervention with a real (zero) effect, and leaving those
    trades out would score the policy only on the trades it hurt or
    helped, not on every trade it touched."""
    detail = {
        "activated": sim.activated,
        "activation_minutes": sim.activation_minutes,
        "stopped": sim.stopped,
        "stop_level": None if sim.stop_level is None else str(sim.stop_level),
        "pessimistic_pnl_usdt": str(sim.pnl_pessimistic_usdt),
        "max_unobserved_minutes_armed": sim.max_unobserved_minutes_armed,
        "observation_status": "OBSERVED" if sim.observable else "UNOBSERVABLE",
    }
    if not sim.observable:
        return sim.activated, sim.activation_minutes, None, detail
    return sim.activated, sim.activation_minutes, sim.pnl_usdt, detail


class _ThesisTightenRule:
    """THESIS_TIGHTEN: move the stop only when the THESIS says so (B
    alone), never on P/L. The direct counterpart of the unconditional
    break-even, to separate "protecting profit" from "responding to a
    weakening thesis"."""

    def __init__(self, position: Position, thresholds: ThesisThresholds) -> None:
        self._position = position
        self._thresholds = thresholds

    def __call__(self, prefix: list[PathPoint]) -> Decimal | None:
        features = build_thesis_features(
            self._position, prefix, self._thresholds.max_hold_hours
        )
        if features is None:
            return None
        decision = evaluate_thesis(self._position, features, self._thresholds)
        return decision.proposed_stop_loss if decision.action == "TIGHTEN_SL" else None


def simulate_position_policy(
    position: Position,
    points: list[PathPoint],
    thresholds: ThesisThresholds,
    mfe_model: MfeModel | None,
    actual_pnl: Decimal,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
) -> tuple[Decimal | None, dict]:
    """THESIS_POLICY: the full `position_decision.decide_position` replayed
    tick by tick. EXIT closes at the observed price; REDUCE realises half
    once; TIGHTEN_SL arms/ratchets a stop that works from the NEXT tick;
    HOLD/PROTECT change nothing. Whatever exposure is left at the real
    close takes the real outcome pro rata (exact: P/L is linear in size).

    `mfe_model` must already be restricted to trades that closed before
    this position OPENED (`MfeModel.as_of`) - the caller's job, checked by
    the tests - so the profit-protection component never sees the future.
    """
    entry = position.simulated_fill_entry
    remaining = Decimal("1")
    realised = _ZERO
    stop: Decimal | None = None
    armed_since: int | None = None
    armed_gap = 0.0
    actions: dict[str, int] = {}
    reduced = False
    for i, point in enumerate(points):
        if stop is not None and i > 0:
            armed_gap = max(armed_gap, point.minutes_in_trade - points[i - 1].minutes_in_trade)
            if point.price <= stop:
                realised += simulate_exit_pnl(
                    position, stop, point.minutes_in_trade, risk_limits, funding_rate,
                    size=position.size * remaining,
                )
                remaining = _ZERO
                actions["STOPPED"] = actions.get("STOPPED", 0) + 1
                break
        features = build_thesis_features(position, points[: i + 1], thresholds.max_hold_hours)
        if features is None:
            continue
        mfe_so_far = max(_ZERO, excursion(max(p.price for p in points[: i + 1]), entry) * 100)
        estimate = mfe_model.estimate(mfe_so_far) if mfe_model is not None else None
        decision = decide_position(position, features, thresholds, estimate)
        actions[decision.action] = actions.get(decision.action, 0) + 1
        if decision.action == "EXIT":
            realised += simulate_exit_pnl(
                position, point.price, point.minutes_in_trade, risk_limits, funding_rate,
                size=position.size * remaining,
            )
            remaining = _ZERO
            break
        if decision.action == "REDUCE" and not reduced:
            realised += simulate_exit_pnl(
                position, point.price, point.minutes_in_trade, risk_limits, funding_rate,
                size=position.size * remaining * _HALF,
            )
            remaining *= _HALF
            reduced = True
        if decision.action == "TIGHTEN_SL" and decision.proposed_stop_loss is not None:
            if stop is None:
                armed_since = i
            stop = decision.proposed_stop_loss if stop is None else max(
                stop, decision.proposed_stop_loss
            )

    if remaining > _ZERO:
        close = _close_minutes(position)
        if stop is not None and close is not None:
            armed_gap = max(armed_gap, close - points[-1].minutes_in_trade)
        exit_price = position.theoretical_exit
        if stop is not None and exit_price is not None and exit_price <= stop and close:
            realised += simulate_exit_pnl(
                position, stop, close, risk_limits, funding_rate,
                size=position.size * remaining,
            )
        else:
            realised += actual_pnl * remaining
    detail = {
        "actions": actions,
        "reduced": reduced,
        "stop_armed_at_index": armed_since,
        "stop_level": None if stop is None else str(stop),
        "max_unobserved_minutes_armed": armed_gap if stop is not None else None,
    }
    if stop is not None and armed_gap > MAX_UNOBSERVED_MINUTES:
        detail["observation_status"] = "UNOBSERVABLE"
        return None, detail
    detail["observation_status"] = "OBSERVED"
    return realised, detail


def run_counterfactuals(
    position: Position,
    points: list[PathPoint],
    risk_limits: RiskLimitsConfig,
    thresholds: ThesisThresholds,
    now: datetime,
    run_id: str,
    mfe_model: MfeModel | None = None,
) -> list[CounterfactualResult]:
    """Every policy, against one real position's real path.

    Returns an EMPTY list when the position's real P/L is unknown, the
    path is empty, or the position had no exposure - a counterfactual
    with nothing to be counter to is not produced at all rather than
    produced and flagged.

    `mfe_model`, when given, must be restricted to trades closed before
    this position opened; only THESIS_POLICY uses it.
    """
    actual_pnl = compute_pnl_or_none(position)
    if actual_pnl is None or not points or position.size == _ZERO:
        return []
    if position.simulated_fill_entry == _ZERO:
        return []

    funding_rate = implied_funding_rate(position)
    results: list[CounterfactualResult] = []

    def _emit(
        policy: CounterfactualPolicy,
        triggered: bool,
        trigger_minutes: float | None,
        exit_price: Decimal | None,
        simulated_pnl: Decimal | None,
        detail: dict,
    ) -> None:
        delta = None if simulated_pnl is None else simulated_pnl - actual_pnl
        status = detail.get("observation_status", "OBSERVED")
        results.append(
            CounterfactualResult(
                counterfactual_id=_counterfactual_id(position.position_id, policy),
                position_id=position.position_id,
                policy=policy,
                created_at=now,
                triggered=triggered,
                trigger_minutes=trigger_minutes,
                simulated_exit_price=exit_price,
                simulated_pnl_usdt=simulated_pnl,
                actual_pnl_usdt=actual_pnl,
                delta_pnl_usdt=delta,
                no_lookahead_verified=True,
                detail={
                    **detail,
                    "observation_status": status,
                    "path_point_count": len(points),
                    "engine_version": ENGINE_VERSION,
                },
                run_id=run_id,
            )
        )

    # The reference row: the real outcome. Delta zero by construction.
    _emit("BASELINE", True, None, position.simulated_fill_exit, actual_pnl, {
        "exit_reason": position.exit_reason,
    })

    # No intervention at all. For the PAPER book this IS the real outcome,
    # except when Guardian closed the trade early: the path ends at the
    # intervention, so what the untouched trade would have done is not
    # observable - UNAVAILABLE, never assumed equal to the real outcome.
    if (position.exit_reason or "").lower() == "guardian_exit":
        _emit("NO_INTERVENTION", False, None, None, None, {
            "observation_status": "UNAVAILABLE",
            "reason": "real trade was closed by an intervention; its path ends there",
        })
    else:
        _emit("NO_INTERVENTION", False, None, position.simulated_fill_exit, actual_pnl, {
            "note": "no intervention changed this trade; identical to BASELINE",
        })

    # Not taking the trade at all: no exposure, no costs, no P/L.
    _emit("REJECT_ENTRY", True, 0.0, None, _ZERO, {
        "note": "no exposure taken, therefore no fees, funding or slippage",
    })

    for policy, delay in _DELAY_POLICIES.items():
        ok, exit_price, pnl, detail = _simulate_delayed_entry(
            position, points, delay, risk_limits, funding_rate
        )
        _emit(policy, ok, delay if ok else None, exit_price, pnl if ok else None, detail)

    # Stop-type policies: shared simulator, shared observability rule.
    stop_policies: list[tuple[CounterfactualPolicy, object]] = [
        ("TIGHTEN_SL_AFTER_FAVORABLE", ThresholdRule(position, BREAK_EVEN_POLICY)),
        ("PROFIT_LOCK_HALF_MFE", ThresholdRule(position, PROFIT_LOCK_POLICY)),
        ("THESIS_TIGHTEN", _ThesisTightenRule(position, thresholds)),
    ]
    for policy, rule in stop_policies:
        sim = simulate_stop_rule(position, points, rule, actual_pnl, risk_limits, funding_rate)
        triggered, minutes, pnl, detail = _stop_row(sim)
        _emit(policy, triggered, minutes, sim.stop_level if sim.stopped else None, pnl, detail)

    # Decision-at-a-tick policies: they act only when Guardian observes,
    # exactly as the live system can only act when it runs.
    exit_policies: list[tuple[CounterfactualPolicy, object]] = [
        (
            "EXIT_ON_THESIS_INVALID",
            lambda prefix: _fires_on_thesis(position, prefix, thresholds, ("INVALID", "EXIT")),
        ),
        (
            "EXIT_ON_THESIS_WEAKENING",
            lambda prefix: _fires_on_thesis(
                position, prefix, thresholds, ("WEAKENING", "INVALID", "EXIT")
            ),
        ),
    ]
    for policy, predicate in exit_policies:
        trigger = _first_trigger(position, points, predicate)
        if trigger is None:
            _emit(policy, False, None, position.simulated_fill_exit, actual_pnl, {
                "note": "policy never triggered; outcome identical to baseline",
            })
            continue
        pnl = simulate_exit_pnl(
            position, trigger.price, trigger.minutes, risk_limits, funding_rate
        )
        _emit(policy, True, trigger.minutes, trigger.price, pnl, {
            "trigger_index": trigger.index,
        })

    # Half-target take-profit is a resting limit order: like a stop it
    # would have worked through a hole, so a hole before it fires (or
    # before the real close, if it never fires) makes it unobservable.
    trigger = _first_trigger(position, points, _fires_on_safe_tp)
    until = trigger.minutes if trigger is not None else _close_minutes(position)
    watched = [p.minutes_in_trade for p in points if until is None or p.minutes_in_trade <= until]
    if until is not None:
        watched.append(until)
    if _largest_gap(watched) > MAX_UNOBSERVED_MINUTES:
        _emit("SAFE_TP_AT_HALF_TARGET", trigger is not None, None, None, None, {
            "observation_status": "UNOBSERVABLE",
        })
    elif trigger is None:
        _emit("SAFE_TP_AT_HALF_TARGET", False, None, position.simulated_fill_exit, actual_pnl, {
            "note": "policy never triggered; outcome identical to baseline",
        })
    else:
        pnl = simulate_exit_pnl(position, trigger.price, trigger.minutes, risk_limits, funding_rate)
        _emit("SAFE_TP_AT_HALF_TARGET", True, trigger.minutes, trigger.price, pnl, {
            "trigger_index": trigger.index,
        })

    reduce_trigger = _first_trigger(
        position,
        points,
        lambda prefix: _fires_on_thesis(
            position, prefix, thresholds, ("WEAKENING", "INVALID", "EXIT")
        ),
    )
    if reduce_trigger is None:
        _emit("REDUCE_ON_WEAKENING", False, None, position.simulated_fill_exit, actual_pnl, {
            "note": "policy never triggered; outcome identical to baseline",
        })
    else:
        pnl, detail = _simulate_reduce(
            position, points, reduce_trigger, actual_pnl, risk_limits, funding_rate
        )
        _emit(
            "REDUCE_ON_WEAKENING",
            True,
            reduce_trigger.minutes,
            reduce_trigger.price,
            pnl,
            detail,
        )

    pnl, detail = simulate_position_policy(
        position, points, thresholds, mfe_model, actual_pnl, risk_limits, funding_rate
    )
    acted = any(action != "HOLD" for action in detail["actions"])
    _emit("THESIS_POLICY", acted, None, None, pnl, detail)

    return results


def common_scorable_positions(rows: list[dict]) -> set[str]:
    """The positions for which EVERY policy present produced a scorable
    delta.

    This exists because of a real bias found on the first run over live
    history (2026-09-25): `DELAY_ENTRY_30M` cannot be scored for a trade
    that closed inside 30 minutes, so its raw total was computed over 102
    positions while `REJECT_ENTRY`'s was computed over 112 - and the ten
    it silently dropped were exactly the fastest-resolving trades. Any
    ranking across policies MUST use this common subset, or it compares
    policies on different books and rewards the one that quietly opted
    out of the hard cases.
    """
    policies: set[str] = set()
    scorable: dict[str, set[str]] = {}
    for row in rows:
        policy = str(row["policy"])
        policies.add(policy)
        if not row.get("no_lookahead_verified"):
            continue
        if row.get("delta_pnl_usdt") is None or row.get("actual_pnl_usdt") is None:
            continue
        scorable.setdefault(policy, set()).add(str(row["position_id"]))
    if not policies:
        return set()
    sets = [scorable.get(policy, set()) for policy in policies]
    return set.intersection(*sets) if sets else set()


def aggregate_policy_performance(
    rows: list[dict], restrict_to: set[str] | None = None
) -> dict[str, dict]:
    """Per-policy totals, split by what the REAL trade did.

    The split is the whole point and is requirement 1's second clause:
    "without creating unacceptable damage to winning trades". A policy
    that saves 40 USDT across losers while destroying 90 across winners
    is a bad policy, and an undifferentiated total would hide that
    completely. Rows with `no_lookahead_verified = 0` are excluded, not
    down-weighted.

    `restrict_to` limits the aggregation to a set of position ids - pass
    `common_scorable_positions(rows)` whenever the numbers will be
    compared ACROSS policies.
    """
    buckets: dict[str, dict] = {}
    for row in rows:
        if not row.get("no_lookahead_verified"):
            continue
        if restrict_to is not None and str(row["position_id"]) not in restrict_to:
            continue
        delta_raw = row.get("delta_pnl_usdt")
        actual_raw = row.get("actual_pnl_usdt")
        if delta_raw is None or actual_raw is None:
            continue
        policy = str(row["policy"])
        delta = Decimal(str(delta_raw))
        actual = Decimal(str(actual_raw))
        bucket = buckets.setdefault(
            policy,
            {
                "n": 0,
                "triggered": 0,
                "total_delta_usdt": _ZERO,
                "winner_delta_usdt": _ZERO,
                "winner_n": 0,
                "loser_delta_usdt": _ZERO,
                "loser_n": 0,
                "improved": 0,
                "worsened": 0,
            },
        )
        bucket["n"] += 1
        bucket["triggered"] += 1 if row.get("triggered") else 0
        bucket["total_delta_usdt"] += delta
        if actual > _ZERO:
            bucket["winner_delta_usdt"] += delta
            bucket["winner_n"] += 1
        elif actual < _ZERO:
            bucket["loser_delta_usdt"] += delta
            bucket["loser_n"] += 1
        if delta > _ZERO:
            bucket["improved"] += 1
        elif delta < _ZERO:
            bucket["worsened"] += 1
    return buckets


def assess_policy_significance(
    rows: list[dict], restrict_to: set[str] | None = None, fdr_q: float = 0.10
) -> dict[str, dict]:
    """Is a policy's measured improvement real, or is it sampling noise?

    Raw totals are the most dangerous output this engine produces. On the
    2026-09-25 sweep `DELAY_ENTRY_30M` showed +826 USDT across 96 trades,
    which reads like a discovery and could just as easily be a handful of
    lucky trades. Nine policies are compared over the same small book, so
    the same two guards Experience Memory uses apply here for exactly the
    same reason:

      * a percentile bootstrap CI on the MEAN per-trade delta (trade P/L
        is fat-tailed, so a t-interval would be badly wrong), and
      * an exact sign test on improved-versus-worsened, FDR-corrected
        across every policy tested in the sweep.

    `verdict` is deliberately conservative. A policy is only
    `ROBUST_IMPROVEMENT` when it is significant, its CI excludes zero on
    the positive side, AND it does not damage winning trades - the last
    clause being the user's own requirement, and the one that
    distinguishes a real management improvement from simply trading less.
    """
    subset = restrict_to
    by_policy: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for row in rows:
        if not row.get("no_lookahead_verified"):
            continue
        if subset is not None and str(row["position_id"]) not in subset:
            continue
        delta_raw = row.get("delta_pnl_usdt")
        actual_raw = row.get("actual_pnl_usdt")
        if delta_raw is None or actual_raw is None:
            continue
        by_policy.setdefault(str(row["policy"]), []).append(
            (Decimal(str(delta_raw)), Decimal(str(actual_raw)))
        )

    policies = sorted(p for p in by_policy if p != "BASELINE")
    p_values: list[float] = []
    interim: dict[str, dict] = {}
    for policy in policies:
        pairs = by_policy[policy]
        deltas = [delta for delta, _actual in pairs]
        improved = sum(1 for d in deltas if d > _ZERO)
        worsened = sum(1 for d in deltas if d < _ZERO)
        decided = improved + worsened
        # Sign test against a fair coin: under the null "this policy is
        # irrelevant", improvements and harms are equally likely.
        p_value = stats.binomial_test_two_sided(improved, decided, 0.5)
        ci = stats.bootstrap_mean_ci([float(d) for d in deltas])
        winner_delta = sum((d for d, actual in pairs if actual > _ZERO), _ZERO)
        interim[policy] = {
            "n": len(deltas),
            "improved": improved,
            "worsened": worsened,
            "mean_delta_usdt": str(sum(deltas, _ZERO) / Decimal(len(deltas))),
            "total_delta_usdt": str(sum(deltas, _ZERO)),
            "winner_delta_usdt": str(winner_delta),
            "ci_low_usdt": str(ci.lower) if ci else None,
            "ci_high_usdt": str(ci.upper) if ci else None,
            "p_value": p_value,
        }
        p_values.append(p_value)

    flags = stats.benjamini_hochberg(p_values, fdr_q)
    for policy, significant in zip(policies, flags, strict=True):
        entry = interim[policy]
        entry["fdr_significant"] = significant
        ci_low = entry["ci_low_usdt"]
        ci_high = entry["ci_high_usdt"]
        positive_ci = ci_low is not None and Decimal(ci_low) > _ZERO
        negative_ci = ci_high is not None and Decimal(ci_high) < _ZERO
        damages_winners = Decimal(entry["winner_delta_usdt"]) < _ZERO
        if significant and positive_ci and not damages_winners:
            entry["verdict"] = "ROBUST_IMPROVEMENT"
        elif significant and positive_ci:
            entry["verdict"] = "IMPROVES_BUT_DAMAGES_WINNERS"
        elif significant and negative_ci:
            entry["verdict"] = "ROBUST_HARM"
        else:
            entry["verdict"] = "NOT_SIGNIFICANT"
        entry["damages_winners"] = damages_winners
    return interim
