"""Position thesis tracking: STRONG / VALID / WEAKENING / INVALID / EXIT.

User requirement 4 and 8, in one sentence: after entry, a position must
keep earning its place, and the question "is this still as good as when
we opened it?" must be an explicit, recurring decision rather than the
absence of a stop-loss hit.

Two design commitments make this safe to build on:

1. **Deterministic, from observable features.** Not "free LLM
   improvisation" (the user's words). Every state and every action below
   is a pure function of numbers Guardian already records. The same
   inputs always give the same state, which is what lets
   `counterfactual.py` replay this logic over historical paths and what
   lets `experience.py` measure whether it was ever right.

2. **It reuses Guardian's own deterioration vocabulary.** The decay
   factors (`momentum_decay`, `volume_decay`, `funding_decay`,
   `secondary_confirmation_lost`, `market_regime`) and the
   watch/protect/exit thresholds come from the already-live
   `guardian/deterministic.py` + `config/guardian.yaml`. This module adds
   the *thesis* dimension Guardian lacks - MFE capture, giveback, time
   since the favourable move, distance to SL/TP - but never a second,
   competing definition of "momentum decayed".

Safety: `recommended_action` is advisory in this phase. Nothing here
opens, closes, sizes or moves anything; `pipeline.py` records the
decision and Guardian Authority remains the only execution path
(requirement 4/12). `validate_action_is_safe` encodes the invariants that
must hold even later, when a separately approved activation lets these
recommendations reach Guardian: never widen a stop, never remove the last
stop, never increase size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from crypto_trading.schemas.godfather import ThesisAction, ThesisState
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")
_ONE = Decimal("1")

# A favourable move that has been given back beyond this fraction is the
# single clearest "this position is no longer what it was" signal in the
# real data: it means the trade WAS right and stopped being right. Set at
# half rather than tuned - this is a structural definition of giveback,
# and `experience.py` is what decides whether acting on it pays.
_GIVEBACK_WEAKENING = Decimal("0.5")
_GIVEBACK_SEVERE = Decimal("0.8")

# Minutes without a new favourable extreme before a still-open position
# counts as stalled. 60 minutes is two Guardian ticks' worth of structure
# on the 30m primary timeframe the screener uses - one timeframe bar
# would be noise, two is a stall.
_STALL_MINUTES = 60.0

# A position past this fraction of its own maximum hold that has never
# gone meaningfully favourable is not "waiting to work", it is dead
# capital (requirement 10: capital efficiency and unnecessary turnover
# are both part of the objective).
_TIME_EXHAUSTED_FRACTION = 0.75
_MEANINGFUL_PROGRESS = Decimal("0.25")


@dataclass(frozen=True)
class ThesisFeatures:
    """Everything the thesis evaluation is allowed to see at one moment.

    Constructed only from a PREFIX of the price path (see
    `build_thesis_features`), which is what makes the same function safe
    to run live and in historical replay without lookahead.
    """

    minutes_in_trade: float
    time_fraction: float
    decay_score: Decimal
    progress_ratio: Decimal
    unrealized_pnl: Decimal
    mfe_pct_so_far: Decimal | None
    mae_pct_so_far: Decimal | None
    giveback_ratio_so_far: Decimal | None
    minutes_since_mfe: float | None
    distance_to_sl_pct: Decimal | None
    distance_to_target_pct: Decimal | None
    factors: dict[str, float] = field(default_factory=dict)

    def factor(self, name: str) -> float:
        return float(self.factors.get(name, 0.0))


@dataclass(frozen=True)
class ThesisDecision:
    state: ThesisState
    action: ThesisAction
    reason_codes: list[str]
    proposed_stop_loss: Decimal | None = None


@dataclass(frozen=True)
class ThesisThresholds:
    """Guardian's own live thresholds, passed in rather than duplicated."""

    watch: Decimal
    protect: Decimal
    exit: Decimal
    max_hold_hours: int


def _sign(position: Position) -> Decimal:
    return Decimal("-1") if position.direction == "SHORT" else _ONE


def build_thesis_features(
    position: Position,
    prefix: list,
    max_hold_hours: int,
) -> ThesisFeatures | None:
    """Features from `prefix` ONLY - the path points observed so far.

    `prefix` is a list of `path.PathPoint`. The caller is responsible for
    truncating it; `counterfactual.py` does so explicitly and
    `test_counterfactual.py` proves that a future point cannot change an
    earlier decision.
    """
    if not prefix:
        return None
    sign = _sign(position)
    entry = position.simulated_fill_entry
    current = prefix[-1]

    best = max(prefix, key=lambda p: sign * (p.price - entry))
    worst = min(prefix, key=lambda p: sign * (p.price - entry))

    def _pct(price: Decimal) -> Decimal | None:
        if entry == _ZERO:
            return None
        return sign * (price - entry) / entry * Decimal("100")

    mfe_pnl = best.unrealized_pnl
    giveback = None
    if mfe_pnl > _ZERO:
        giveback = (mfe_pnl - current.unrealized_pnl) / mfe_pnl

    distance_to_sl = None
    distance_to_target = None
    if current.price != _ZERO:
        distance_to_sl = (
            sign * (current.price - position.stop_loss) / current.price * Decimal("100")
        )
        distance_to_target = (
            sign * (position.target - current.price) / current.price * Decimal("100")
        )

    hold_minutes = max_hold_hours * 60
    return ThesisFeatures(
        minutes_in_trade=current.minutes_in_trade,
        time_fraction=(current.minutes_in_trade / hold_minutes) if hold_minutes else 0.0,
        decay_score=current.decay_score,
        progress_ratio=current.progress_ratio,
        unrealized_pnl=current.unrealized_pnl,
        mfe_pct_so_far=_pct(best.price),
        mae_pct_so_far=_pct(worst.price),
        giveback_ratio_so_far=giveback,
        minutes_since_mfe=current.minutes_in_trade - best.minutes_in_trade,
        distance_to_sl_pct=distance_to_sl,
        distance_to_target_pct=distance_to_target,
        factors=current.factors,
    )


def classify_thesis_state(
    features: ThesisFeatures, thresholds: ThesisThresholds
) -> tuple[ThesisState, list[str]]:
    """Ordered, first-match-wins classification. Order matters and is
    deliberate: the most invalidating condition wins, so a position can
    never be reported STRONG because a later, weaker rule also matched."""
    reasons: list[str] = []

    momentum_gone = features.factor("momentum_decay") >= 0.8
    volume_gone = features.factor("volume_decay") >= 0.8
    confirmation_lost = features.factor("secondary_confirmation_lost") >= 1.0
    losing = features.unrealized_pnl < _ZERO
    giveback = features.giveback_ratio_so_far

    if features.decay_score >= thresholds.exit:
        reasons.append("guardian_decay_exit")
        return "EXIT", reasons

    if momentum_gone and volume_gone and losing:
        reasons.append("entry_thesis_fully_invalidated")
        return "INVALID", reasons

    if features.decay_score >= thresholds.protect and losing:
        reasons.append("guardian_decay_protect_while_losing")
        return "INVALID", reasons

    if giveback is not None and giveback >= _GIVEBACK_SEVERE:
        reasons.append("severe_giveback_of_favourable_move")
        return "INVALID", reasons

    if (
        features.time_fraction >= _TIME_EXHAUSTED_FRACTION
        and features.progress_ratio < _MEANINGFUL_PROGRESS
    ):
        reasons.append("time_exhausted_without_progress")
        return "WEAKENING", reasons

    if giveback is not None and giveback >= _GIVEBACK_WEAKENING:
        reasons.append("gave_back_half_of_favourable_move")
        return "WEAKENING", reasons

    if features.decay_score >= thresholds.watch:
        reasons.append("guardian_decay_watch")
        return "WEAKENING", reasons

    if confirmation_lost:
        reasons.append("secondary_timeframe_confirmation_lost")
        return "WEAKENING", reasons

    if (
        features.minutes_since_mfe is not None
        and features.minutes_since_mfe >= _STALL_MINUTES
        and features.progress_ratio < _MEANINGFUL_PROGRESS
    ):
        reasons.append("stalled_since_favourable_extreme")
        return "WEAKENING", reasons

    if features.progress_ratio >= Decimal("0.5") and features.unrealized_pnl > _ZERO:
        reasons.append("more_than_half_way_to_target_in_profit")
        return "STRONG", reasons

    reasons.append("no_deterioration_observed")
    return "VALID", reasons


def _breakeven_stop(position: Position) -> Decimal:
    """The entry fill itself. Protecting at breakeven is the ONLY stop
    this module ever proposes - it is always tighter than the original
    stop for a position currently in profit, so the "never widen" rule
    cannot be violated by construction, and it never removes the stop."""
    return position.simulated_fill_entry


def decide_thesis_action(
    position: Position, state: ThesisState, features: ThesisFeatures
) -> tuple[ThesisAction, Decimal | None, list[str]]:
    """Map a thesis state to one of the five permitted actions.

    GODFATHER may never open a position, so there is no BUY/ADD here by
    construction - the `ThesisAction` Literal has no such member.
    """
    reasons: list[str] = []
    in_profit = features.unrealized_pnl > _ZERO
    giveback = features.giveback_ratio_so_far

    if state in ("EXIT", "INVALID"):
        reasons.append("thesis_no_longer_supports_the_position")
        return "EXIT", None, reasons

    if state == "WEAKENING":
        if in_profit and giveback is not None and giveback >= _GIVEBACK_WEAKENING:
            reasons.append("lock_in_remaining_favourable_move")
            stop = _tighter_stop(position, _breakeven_stop(position))
            return ("TIGHTEN_SL", stop, reasons) if stop is not None else ("PROTECT", None, reasons)
        if in_profit:
            reasons.append("protect_open_profit_while_thesis_weakens")
            return "PROTECT", None, reasons
        reasons.append("reduce_exposure_while_thesis_weakens_at_a_loss")
        return "REDUCE", None, reasons

    reasons.append("thesis_intact")
    return "HOLD", None, reasons


def _tighter_stop(position: Position, proposed: Decimal) -> Decimal | None:
    """None unless `proposed` is STRICTLY tighter than the stop the
    position already has. A proposal that would widen the stop is dropped
    here rather than passed on with a warning - the invariant is
    structural, not advisory."""
    sign = _sign(position)
    if sign * (proposed - position.stop_loss) > _ZERO:
        return proposed
    return None


def evaluate_thesis(
    position: Position, features: ThesisFeatures, thresholds: ThesisThresholds
) -> ThesisDecision:
    state, state_reasons = classify_thesis_state(features, thresholds)
    action, stop, action_reasons = decide_thesis_action(position, state, features)
    return ThesisDecision(
        state=state,
        action=action,
        reason_codes=[*state_reasons, *action_reasons],
        proposed_stop_loss=stop,
    )


def validate_action_is_safe(position: Position, decision: ThesisDecision) -> list[str]:
    """The hard invariants from requirement 8, as a checkable function.

    Returns the list of violations - empty means safe. Used as an
    assertion in `pipeline.py` and as the subject of its own test: even
    though this phase never enforces a thesis action, the rules must
    already be true of everything recorded, so that a later activation
    changes only WHO reads the row, never whether the row was safe.
    """
    violations: list[str] = []
    sign = _sign(position)
    stop = decision.proposed_stop_loss

    if decision.action == "TIGHTEN_SL":
        if stop is None:
            violations.append("TIGHTEN_SL without a proposed stop")
        elif sign * (stop - position.stop_loss) <= _ZERO:
            violations.append("proposed stop is not strictly tighter than the current stop")
    elif stop is not None:
        violations.append(f"{decision.action} must not carry a proposed stop")

    if decision.action not in ("HOLD", "PROTECT", "TIGHTEN_SL", "REDUCE", "EXIT"):
        violations.append(f"action {decision.action} is not a permitted GODFATHER action")

    return violations
