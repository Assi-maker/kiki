"""GODFATHER Trade Investigator: one structured post-mortem per closed trade.

Requirement 1, built as data rather than prose. The investigation has
three evidence sections (BEFORE / DURING / AFTER) assembled purely from
rows the system already persisted, one closed classification, two quality
verdicts, and - the part the user singled out as most important - an
explicit answer to:

    "What concrete decision available at time T could have reduced this
     loss without creating unacceptable damage to winning trades?"

That answer comes from `counterfactual.py`, not from an opinion: the best
alternative for THIS trade, cross-checked against that policy's portfolio-
wide effect on winning trades. When no alternative clears both bars the
finding says so with `policy=None`, which is a real result and by far the
most common one for a trade that was simply a bad idea.

Classification is deterministic and ordered. An LLM may later narrate one
of these records (requirement 11 explicitly allows post-mortem reasoning
as a good use of AI), but it never produces the label, because a label
that drifts cannot be counted and a category that cannot be counted can
never reach the sample size `experience.py` requires.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.godfather.path import PathMetrics
from crypto_trading.godfather.risk_units import live_usdt_outcome
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.godfather import (
    AvoidableLossFinding,
    CounterfactualResult,
    QualityVerdict,
    TradeClassification,
    TradeInvestigation,
)
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")

# A favourable excursion below this is not "the trade briefly worked", it
# is tick noise around the entry fill. Set at 0.3% because the configured
# round-trip friction (spread 0.05% + slippage 0.05% + fee 0.04%, see
# config/risk_limits.yaml) is already ~0.14% - a move that cannot cover
# twice its own costs never gave the position anything to manage.
_NOISE_EXCURSION_PCT = Decimal("0.3")

# A favourable excursion at or above this is a real, tradeable move: the
# entry was right about direction, so anything that went wrong afterwards
# is a management finding, not an entry finding.
_REAL_MOVE_PCT = Decimal("1.0")

# Giving back at least half of a real favourable move is the definition
# of bad management used everywhere in this subsystem - same constant
# meaning as thesis.py's own _GIVEBACK_WEAKENING, deliberately not shared
# via import so that retuning one never silently retunes the other.
_BAD_GIVEBACK = Decimal("0.5")
_GOOD_GIVEBACK = Decimal("0.25")

# |P/L| below this fraction of notional is a scratch, not an outcome.
_NOISE_PNL_FRACTION = Decimal("0.001")

# How close to the target counts as TARGET_TOO_FAR rather than
# EXIT_TOO_LATE. Both describe a trade that found a move and did not keep
# it, but they name DIFFERENT fixable parameters, and the boundary is
# what decides which advice the trade contributes to:
#   - it came within 20% of the target and never touched it => the target
#     was the binding constraint (TARGET_TOO_FAR),
#   - it made a real move but never got near the target, then gave it
#     back => exit timing was the binding constraint (EXIT_TOO_LATE).
# A losing trade always gives back essentially all of its favourable
# move, so giveback alone cannot separate these two - only distance to
# target can.
_NEAR_TARGET_PROGRESS = Decimal("0.8")

_ASSESSMENT_ROLES = (
    "opportunity_screen",
    "news_sentiment",
    "technical",
    "bull_thesis",
    "forecast",
    "risk",
    "bear_adversarial",
    "qa",
)


def _pct_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def build_before_section(
    candidate: Candidate | None, gate_decision: dict | None, position: Position
) -> dict:
    """Everything that was known and said BEFORE entry, verbatim from
    storage. Missing sources are omitted, never filled with a guess -
    the same non-invention rule `detective/context.py` already follows."""
    before: dict = {
        "planned_entry": str(position.theoretical_entry),
        "actual_entry_fill": str(position.simulated_fill_entry),
        "entry_slippage_pct": _entry_slippage_pct(position),
        "stop_loss": str(position.stop_loss),
        "target": str(position.target),
        "size_usdt": str(position.size),
        "risk_reward": _risk_reward(position),
    }
    if candidate is not None:
        evidence = candidate.evidence_record
        before["evidence_record"] = evidence.model_dump(mode="json")
        before["candidate_score"] = evidence.candidate_score
        before["trigger_reasons"] = list(evidence.trigger_reasons)
        before["data_quality_status"] = evidence.data_quality_status
        for role in _ASSESSMENT_ROLES:
            assessment = getattr(candidate, role, None)
            if assessment is not None:
                before[f"{role}_assessment"] = assessment.model_dump(mode="json")
        before["bull_bear_conflict"] = _bull_bear_conflict(candidate)
        before["expected_scenario"] = _expected_scenario(candidate)
    if gate_decision is not None:
        before["gate_decision"] = gate_decision
    return before


def _entry_slippage_pct(position: Position) -> str | None:
    if position.theoretical_entry == _ZERO:
        return None
    diff = (position.simulated_fill_entry - position.theoretical_entry) / (
        position.theoretical_entry
    )
    return str(diff * Decimal("100"))


def _exit_slippage_pct(position: Position) -> str | None:
    if position.theoretical_exit in (None, _ZERO) or position.simulated_fill_exit is None:
        return None
    diff = (position.simulated_fill_exit - position.theoretical_exit) / position.theoretical_exit
    return str(diff * Decimal("100"))


def _risk_reward(position: Position) -> str | None:
    risk = abs(position.simulated_fill_entry - position.stop_loss)
    reward = abs(position.target - position.simulated_fill_entry)
    if risk == _ZERO:
        return None
    return str(reward / risk)


def _bull_bear_conflict(candidate: Candidate) -> dict:
    """The size of the disagreement the pipeline carried into the trade.

    Counted, not judged: the Bear agent's presence is a process
    requirement rather than a veto (see .claude/agents/
    crypto-bear-adversarial.md), so what matters for a post-mortem is
    how much contradiction was on the table and whether it turned out to
    be the thing that mattered."""
    bull = getattr(candidate, "bull_thesis", None)
    bear = getattr(candidate, "bear_adversarial", None)
    return {
        "bull_present": bull is not None,
        "bear_present": bear is not None,
        "counterargument_count": len(getattr(bear, "counterarguments", []) or []),
        "alternative_explanation_count": len(
            getattr(bear, "alternative_explanations", []) or []
        ),
        # `falsification_conditions` is a single string on
        # BearAdversarialAssessment, not a list - counting it would count
        # characters. What matters for a post-mortem is whether the Bear
        # committed to a falsifiable condition at all.
        "falsification_condition_stated": bool(
            str(getattr(bear, "falsification_conditions", "") or "").strip()
        ),
    }


def _expected_scenario(candidate: Candidate) -> dict | None:
    forecast = getattr(candidate, "forecast", None)
    if forecast is None:
        return None
    probabilities = dict(getattr(forecast, "scenario_probabilities", {}) or {})
    if not probabilities:
        return None
    top = max(probabilities.items(), key=lambda kv: kv[1])
    return {
        "scenario_probabilities": probabilities,
        "most_likely_scenario": top[0],
        "most_likely_probability": top[1],
        "horizon": getattr(forecast, "horizon", None),
    }


def build_during_section(metrics: PathMetrics) -> dict:
    """The real price path, reduced to the events that matter."""
    return {
        "path_point_count": metrics.point_count,
        "first_observed_minutes": metrics.first_observed_minutes,
        "last_observed_minutes": metrics.last_observed_minutes,
        "mfe_pct": _pct_or_none(metrics.mfe_pct),
        "mae_pct": _pct_or_none(metrics.mae_pct),
        "mfe_pnl_usdt": _pct_or_none(metrics.mfe_pnl),
        "mae_pnl_usdt": _pct_or_none(metrics.mae_pnl),
        "minutes_to_mfe": metrics.minutes_to_mfe,
        "minutes_to_mae": metrics.minutes_to_mae,
        "minutes_to_target_touch": metrics.minutes_to_target_touch,
        "minutes_to_sl_touch": metrics.minutes_to_sl_touch,
        "max_progress_ratio": _pct_or_none(metrics.max_progress_ratio),
        "final_progress_ratio": _pct_or_none(metrics.final_progress_ratio),
        "giveback_pnl_usdt": _pct_or_none(metrics.giveback_pnl),
        "giveback_ratio": _pct_or_none(metrics.giveback_ratio),
        "minutes_since_mfe_at_close": metrics.minutes_since_mfe_at_close,
        "first_questionable_minutes": metrics.first_questionable_minutes,
        "first_invalid_minutes": metrics.first_invalid_minutes,
        "max_decay_score": _pct_or_none(metrics.max_decay_score),
        "price_cross_check_failures": metrics.cross_check_failures,
    }


def build_after_section(
    position: Position, realized_pnl: Decimal | None, live_execution: dict | None
) -> dict:
    """The realised outcome, including exchange-side costs when the
    position also ran LIVE. `realized_pnl_usdt` is None - never a
    substituted zero - for a LIVE-mirrored close with no PAPER exit
    data."""
    hold_minutes = None
    if position.closed_at is not None:
        hold_minutes = (position.closed_at - position.opened_at).total_seconds() / 60
    after: dict = {
        "exit_reason": position.exit_reason,
        "hold_minutes": hold_minutes,
        "theoretical_exit": (
            str(position.theoretical_exit) if position.theoretical_exit is not None else None
        ),
        "actual_exit_fill": (
            str(position.simulated_fill_exit)
            if position.simulated_fill_exit is not None
            else None
        ),
        "exit_slippage_pct": _exit_slippage_pct(position),
        "realized_pnl_usdt": str(realized_pnl) if realized_pnl is not None else None,
        "fees_usdt": str(position.fees) if position.fees is not None else None,
        "funding_usdt": str(position.funding) if position.funding is not None else None,
    }
    if live_execution is not None:
        after["live"] = {
            "phase": live_execution.get("phase"),
            "exit_reason": live_execution.get("exit_reason"),
            "exchange_fill_entry": live_execution.get("exchange_fill_entry"),
            "exchange_fill_exit": live_execution.get("exchange_fill_exit"),
            "realized_fees_usdt": live_execution.get("realized_fees_usdt"),
            "realized_funding_usdt": live_execution.get("realized_funding_usdt"),
            "notional_usdt": live_execution.get("notional_usdt"),
            "leverage": live_execution.get("leverage"),
            "margin_usdt": live_execution.get("margin_usdt"),
            "entry_quantity": live_execution.get("entry_quantity"),
        }
        # Real gross P/L at the LIVE execution's own size - realized_pnl_usdt
        # above is the paper position's, a different (paper) size.
        own_size = live_usdt_outcome(live_execution, None, position.direction, None)
        gross = own_size.get("gross_pnl_usdt") if own_size else None
        after["live"]["gross_pnl_usdt"] = str(gross) if gross is not None else None
    return after


def classify_trade(
    position: Position,
    metrics: PathMetrics,
    realized_pnl: Decimal | None,
    candidate: Candidate | None,
    sustained_factors: dict[str, bool] | None = None,
) -> tuple[TradeClassification, list[str]]:
    """Ordered, first-match-wins classification into the user's taxonomy.

    Order is the design. The most specific, most actionable diagnosis
    wins, so a trade is never filed as generic NOISE when it is really
    EXIT_TOO_LATE, and never filed as BAD_ENTRY when the entry was right
    and only the management failed.
    """
    reasons: list[str] = []
    if realized_pnl is None or metrics.point_count == 0:
        reasons.append("no scorable outcome or no observed price path")
        return "UNKNOWN", reasons

    won = realized_pnl > _ZERO
    mfe = metrics.mfe_pct if metrics.mfe_pct is not None else _ZERO
    mae = metrics.mae_pct if metrics.mae_pct is not None else _ZERO
    giveback = metrics.giveback_ratio
    triggers = set(candidate.evidence_record.trigger_reasons) if candidate else set()
    sustained = sustained_factors or {}
    regime_headwind = bool(sustained.get("market_regime"))
    momentum_gone = bool(sustained.get("momentum_decay"))

    if won and giveback is not None and giveback <= _GOOD_GIVEBACK:
        reasons.append("won and kept most of the favourable move")
        return "GOOD_ENTRY_GOOD_MANAGEMENT", reasons

    # Ordering note (2026-09-25): these two specific diagnoses are
    # checked BEFORE the generic GOOD_ENTRY_BAD_MANAGEMENT/EXIT_TOO_LATE
    # pair on purpose. A trade that reaches half its target almost always
    # also clears the 1% real-move bar, so with the generic branch first
    # both of these were structurally unreachable - the first sweep over
    # the real book produced 0 of either. They name a fixable parameter
    # (the target distance, the stop distance) rather than a diffuse
    # "management" failure, which makes them strictly more actionable.
    if (
        not won
        and metrics.max_progress_ratio is not None
        and metrics.max_progress_ratio >= _NEAR_TARGET_PROGRESS
        and metrics.minutes_to_target_touch is None
    ):
        reasons.append("came within reach of the target and never touched it")
        return "TARGET_TOO_FAR", reasons

    if (
        not won
        and _normalized_exit_reason(position) == "stop_loss"
        and metrics.first_invalid_minutes is not None
        and metrics.minutes_to_sl_touch is not None
        and metrics.minutes_to_sl_touch - metrics.first_invalid_minutes >= 60
    ):
        reasons.append("thesis was already invalid an hour or more before the stop paid for it")
        return "SL_TOO_WIDE", reasons

    if mfe >= _REAL_MOVE_PCT and not won:
        if giveback is not None and giveback >= _BAD_GIVEBACK:
            reasons.append("a real favourable move was given back before exit")
            return "EXIT_TOO_LATE", reasons
        reasons.append("entry found a real move that management failed to convert")
        return "GOOD_ENTRY_BAD_MANAGEMENT", reasons

    if not won and mfe < _NOISE_EXCURSION_PCT and triggers & {
        "momentum_breakout",
        "price_volatility",
    }:
        reasons.append("breakout/volatility entry that never went favourable at all")
        return "FALSE_BREAKOUT", reasons

    if not won and momentum_gone:
        reasons.append("momentum decayed to near-zero during the trade")
        return "MOMENTUM_DECAY", reasons

    if not won and regime_headwind:
        reasons.append("traded long into a sustained bearish BTC regime")
        return "REGIME_FAILURE", reasons

    if position.size != _ZERO and abs(realized_pnl) <= position.size * _NOISE_PNL_FRACTION:
        if abs(mfe) < _NOISE_EXCURSION_PCT and abs(mae) < _NOISE_EXCURSION_PCT:
            reasons.append("neither side of the trade ever moved beyond friction")
            return "NOISE", reasons

    if not won and mfe < _NOISE_EXCURSION_PCT:
        reasons.append("no favourable excursion worth the friction")
        return "BAD_ENTRY", reasons

    if won:
        reasons.append("won, but gave back a large share of the favourable move")
        return "GOOD_ENTRY_BAD_MANAGEMENT", reasons

    reasons.append("moved favourably first, then failed - right idea, wrong moment")
    return "BAD_TIMING", reasons


def _normalized_exit_reason(position: Position) -> str:
    return (position.exit_reason or "").strip().lower()


def summarise_sustained_factors(points: list, threshold: float = 0.7) -> dict[str, bool]:
    """Which Guardian decay factors were elevated for at least half the
    observed path. "Sustained" rather than "peaked" on purpose: a single
    tick above a threshold is noise, a majority of the trade's life above
    it is a condition the trade actually lived in."""
    if not points:
        return {}
    names: set[str] = set()
    for point in points:
        names.update(point.factors.keys())
    summary: dict[str, bool] = {}
    for name in sorted(names):
        elevated = sum(1 for p in points if p.factors.get(name, 0.0) >= threshold)
        summary[name] = elevated * 2 >= len(points)
    return summary


def judge_entry(metrics: PathMetrics, realized_pnl: Decimal | None) -> QualityVerdict:
    """Did the entry find a real move? Deliberately independent of the
    outcome: an entry that produced a 2% favourable excursion was a good
    entry even if the trade lost, and separating that from management is
    the entire point of the two-verdict split."""
    if metrics.point_count == 0 or metrics.mfe_pct is None:
        return "UNKNOWN"
    if metrics.mfe_pct >= _REAL_MOVE_PCT:
        return "GOOD"
    if metrics.mfe_pct < _NOISE_EXCURSION_PCT and (realized_pnl or _ZERO) <= _ZERO:
        return "BAD"
    return "ACCEPTABLE"


def judge_management(metrics: PathMetrics, realized_pnl: Decimal | None) -> QualityVerdict:
    """Given the move the entry found, how much of it was kept?"""
    if metrics.point_count == 0 or metrics.giveback_ratio is None:
        return "UNKNOWN"
    if metrics.giveback_ratio <= _GOOD_GIVEBACK:
        return "GOOD"
    if metrics.giveback_ratio >= _BAD_GIVEBACK:
        return "BAD"
    return "ACCEPTABLE"


def build_avoidable_loss_finding(
    counterfactuals: list[CounterfactualResult],
    policy_portfolio_effect: dict[str, dict] | None,
) -> AvoidableLossFinding:
    """The requirement-1 question, answered from simulation instead of
    hindsight narrative.

    Three bars, all of which must be cleared:
      1. the alternative must actually have improved THIS trade,
      2. across the whole comparable book it must be net positive, and
      3. across that same book it must not take money OUT of winning
         trades at all.

    The third bar is the user's own wording - "without creating
    unacceptable damage to winning trades" - and it is the one that
    matters most in practice. Without it, the first sweep over the real
    book recommended REJECT_ENTRY for 31 trades purely because the book
    as a whole lost money: not trading beats a losing book by
    construction, while destroying 930 USDT of winning trades and
    offering no rule that could have identified the bad ones in advance.
    That is a tautology, not a finding.

    `policy_portfolio_effect` is `counterfactual.aggregate_policy_
    performance(..., restrict_to=common_scorable_positions(...))`. When
    it is None the finding is still produced but `winner_damage_checked`
    stays False, which is what stops a single lucky trade from being
    reported as a rule.
    """
    usable = [
        cf
        for cf in counterfactuals
        if cf.policy != "BASELINE"
        and cf.no_lookahead_verified
        and cf.delta_pnl_usdt is not None
        and cf.delta_pnl_usdt > _ZERO
    ]
    if not usable:
        return AvoidableLossFinding(
            policy=None,
            winner_damage_checked=policy_portfolio_effect is not None,
            explanation=(
                "No simulated alternative available at any point in this trade "
                "would have improved its outcome."
            ),
        )

    usable.sort(key=lambda cf: cf.delta_pnl_usdt, reverse=True)
    for candidate_cf in usable:
        if policy_portfolio_effect is None:
            return AvoidableLossFinding(
                policy=candidate_cf.policy,
                decision_available_at_minutes=candidate_cf.trigger_minutes,
                estimated_pnl_improvement_usdt=candidate_cf.delta_pnl_usdt,
                winner_damage_checked=False,
                explanation=(
                    f"{candidate_cf.policy} would have improved this trade by "
                    f"{candidate_cf.delta_pnl_usdt} USDT. Portfolio-wide effect on "
                    "winning trades was not available at the time of this "
                    "investigation and is therefore NOT claimed."
                ),
            )
        effect = policy_portfolio_effect.get(candidate_cf.policy)
        if effect is None:
            continue
        winner_delta = Decimal(str(effect["winner_delta_usdt"]))
        total_delta = Decimal(str(effect["total_delta_usdt"]))
        if total_delta <= _ZERO or winner_delta < _ZERO:
            continue
        return AvoidableLossFinding(
            policy=candidate_cf.policy,
            decision_available_at_minutes=candidate_cf.trigger_minutes,
            estimated_pnl_improvement_usdt=candidate_cf.delta_pnl_usdt,
            winner_damage_checked=True,
            explanation=(
                f"{candidate_cf.policy} would have improved this trade by "
                f"{candidate_cf.delta_pnl_usdt} USDT. Across the whole comparable "
                f"book it is net {total_delta} USDT and it does not take money out "
                f"of winning trades ({winner_delta} USDT)."
            ),
        )

    return AvoidableLossFinding(
        policy=None,
        winner_damage_checked=True,
        explanation=(
            "Alternatives existed that would have improved this individual trade, "
            "but none of them is both net positive across the comparable book AND "
            "harmless to winning trades - adopting one would trade this loss for "
            "damage elsewhere."
        ),
    )


def investigate_position(
    position: Position,
    candidate: Candidate | None,
    gate_decision: dict | None,
    live_execution: dict | None,
    points: list,
    metrics: PathMetrics,
    realized_pnl: Decimal | None,
    counterfactuals: list[CounterfactualResult],
    policy_portfolio_effect: dict[str, dict] | None,
    now: datetime,
    run_id: str,
) -> TradeInvestigation:
    """Assemble the whole post-mortem. Pure: every input is already-read
    data, and the only thing this function decides is the judgement."""
    sustained = summarise_sustained_factors(points)
    classification, reasons = classify_trade(
        position, metrics, realized_pnl, candidate, sustained
    )
    during = build_during_section(metrics)
    during["sustained_decay_factors"] = sustained
    return TradeInvestigation(
        position_id=position.position_id,
        candidate_id=position.candidate_id,
        instrument=position.instrument,
        created_at=now,
        classification=classification,
        entry_verdict=judge_entry(metrics, realized_pnl),
        management_verdict=judge_management(metrics, realized_pnl),
        before=build_before_section(candidate, gate_decision, position),
        during=during,
        after=build_after_section(position, realized_pnl, live_execution),
        reason_codes=reasons,
        avoidable_loss=build_avoidable_loss_finding(counterfactuals, policy_portfolio_effect),
        run_id=run_id,
    )
