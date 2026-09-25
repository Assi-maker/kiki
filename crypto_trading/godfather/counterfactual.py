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
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.godfather import stats
from crypto_trading.godfather.path import PathPoint
from crypto_trading.godfather.thesis import (
    ThesisThresholds,
    build_thesis_features,
    classify_thesis_state,
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

# Favourable excursion after which TIGHTEN_SL_AFTER_FAVORABLE moves the
# stop to breakeven. Deliberately the SAME 1% the live Profit Protection
# mechanism already uses (`config/loader.py::
# LiveExecutionConfig.profit_protection_threshold_pct`), so this
# counterfactual measures the policy the system actually has rather than
# a hypothetical one nobody could ship.
_FAVORABLE_THRESHOLD = Decimal("0.01")

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


def _hold_hours(position: Position, minutes: float) -> Decimal:
    return Decimal(str(max(0.0, minutes))) / Decimal("60")


def _simulate_exit_pnl(
    position: Position,
    exit_reference_price: Decimal,
    minutes_in_trade: float,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
    size: Decimal | None = None,
) -> Decimal:
    """P/L of closing `size` (default: the whole position) at
    `exit_reference_price`, through the live fill/fee/funding model.

    Not a new formula: `compute_fill_price`, `compute_fees` and
    `compute_funding` are imported unmodified from the paper engine, and
    the gross term is the same `size * price_return` used by
    `execution.py::compute_pnl`.
    """
    notional = position.size if size is None else size
    fill = compute_fill_price(
        exit_reference_price,
        position.direction,
        risk_limits.spread_pct,
        risk_limits.slippage_pct,
        "exit",
    )
    entry = position.simulated_fill_entry
    if entry == _ZERO:
        return _ZERO
    price_return = (fill - entry) / entry
    gross = notional * price_return * _sign(position)
    fees = compute_fees(notional, risk_limits.fee_pct)
    funding = compute_funding(notional, funding_rate, _hold_hours(position, minutes_in_trade))
    return gross - fees - funding


def _implied_funding_rate(position: Position) -> Decimal:
    """Back out the per-period funding rate the real close actually
    charged, so a simulated exit is charged on the same basis rather than
    on an assumed zero. Falls back to 0 when the real position carries no
    funding data (a LIVE-mirrored close), which is the only honest choice
    available there."""
    if position.funding is None or position.size == _ZERO or position.closed_at is None:
        return _ZERO
    hold_hours = Decimal(
        str((position.closed_at - position.opened_at).total_seconds() / 3600)
    )
    periods = int(hold_hours // Decimal("8"))
    if periods <= 0:
        return _ZERO
    return position.funding / (position.size * periods)


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


def _effective_tightened_stop(
    position: Position, prefix: list[PathPoint]
) -> Decimal | None:
    """Breakeven, but only once the favourable excursion SO FAR has
    reached the threshold. Returns None before that - the original stop
    still governs, and it is never widened."""
    sign = _sign(position)
    entry = position.simulated_fill_entry
    if entry == _ZERO:
        return None
    best = max(prefix, key=lambda p: sign * (p.price - entry))
    excursion = sign * (best.price - entry) / entry
    if excursion < _FAVORABLE_THRESHOLD:
        return None
    return entry


def _fires_on_tightened_stop(position: Position, prefix: list[PathPoint]) -> bool:
    stop = _effective_tightened_stop(position, prefix)
    if stop is None:
        return False
    return _sign(position) * (prefix[-1].price - stop) <= _ZERO


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
    funding = compute_funding(position.size, funding_rate, _hold_hours(position, held))
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
    realised_half = _simulate_exit_pnl(
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


def run_counterfactuals(
    position: Position,
    points: list[PathPoint],
    risk_limits: RiskLimitsConfig,
    thresholds: ThesisThresholds,
    now: datetime,
    run_id: str,
) -> list[CounterfactualResult]:
    """Every policy, against one real position's real path.

    Returns an EMPTY list when the position's real P/L is unknown or the
    path is empty - a counterfactual with nothing to be counter to is not
    produced at all rather than produced and flagged.
    """
    actual_pnl = compute_pnl_or_none(position)
    if actual_pnl is None or not points:
        return []

    funding_rate = _implied_funding_rate(position)
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
                detail={**detail, "path_point_count": len(points)},
                run_id=run_id,
            )
        )

    # The reference row. Its delta is zero by construction - it exists so
    # every aggregation can be written against a uniform table instead of
    # special-casing "the real one".
    _emit("BASELINE", True, None, position.simulated_fill_exit, actual_pnl, {
        "exit_reason": position.exit_reason,
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
        (
            "TIGHTEN_SL_AFTER_FAVORABLE",
            lambda prefix: _fires_on_tightened_stop(position, prefix),
        ),
        ("SAFE_TP_AT_HALF_TARGET", lambda prefix: _fires_on_safe_tp(prefix)),
    ]
    for policy, predicate in exit_policies:
        trigger = _first_trigger(position, points, predicate)
        if trigger is None:
            # Never fired => this policy would have produced exactly the
            # real outcome. Recorded explicitly (delta 0) rather than
            # omitted, so "how often does this policy even apply?" is
            # answerable from the table.
            _emit(policy, False, None, position.simulated_fill_exit, actual_pnl, {
                "note": "policy never triggered; outcome identical to baseline",
            })
            continue
        pnl = _simulate_exit_pnl(
            position, trigger.price, trigger.minutes, risk_limits, funding_rate
        )
        _emit(policy, True, trigger.minutes, trigger.price, pnl, {
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
