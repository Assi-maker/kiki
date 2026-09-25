"""The position decision: HOLD / PROTECT / TIGHTEN_SL / REDUCE / EXIT.

Two questions are kept apart on purpose, because conflating them is what
the TIGHTEN_SL_AFTER_FAVORABLE evaluation showed to be expensive:

A. **Profit protection** - "is the open profit at risk of being given
   back?" Answered from history (`mfe_model`): how often did trades that
   reached this favourable level come all the way back to entry?

B. **Thesis management** - "is the position still as good as when we
   opened it?" Answered by the existing, deterministic `thesis.py`
   (STRONG / VALID / WEAKENING / INVALID / EXIT from Guardian's own
   factors plus MFE, giveback, time and distance to SL/TP).

The combination is context-aware by construction, never a P/L rule:

* STRONG thesis + positive MFE           -> HOLD (A is not consulted)
* normal pullback, thesis VALID/STRONG   -> HOLD
* WEAKENING thesis + open profit         -> TIGHTEN_SL to break-even (B)
* INVALID / EXIT thesis                  -> EXIT (B)
* VALID thesis, but history says trades at this level usually come back
  to entry                               -> TIGHTEN_SL to break-even (A)
* A without enough history               -> nothing (INSUFFICIENT_DATA)

A PROTECT that can carry a stop becomes TIGHTEN_SL with that stop; a
PROTECT that cannot (the position is not in profit) stays PROTECT = "no
mechanical action, watch closely". `thesis.validate_action_is_safe`
runs on every decision: a stop is only ever proposed strictly tighter
than the current one, never removed, and there is no action that adds
exposure.

This module decides nothing on its own. It is replayed over history by
the counterfactual engine (policy THESIS_POLICY) and recorded for open
positions by the pipeline with `enforced=False`; whether it may ever act
is the policy registry's question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from crypto_trading.godfather.mfe_model import MfeEstimate
from crypto_trading.godfather.thesis import (
    ThesisDecision,
    ThesisFeatures,
    ThesisThresholds,
    _tighter_stop,
    evaluate_thesis,
    validate_action_is_safe,
)
from crypto_trading.schemas.godfather import ThesisAction, ThesisState
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")

# History says "more likely than not, trades at this level come back to
# entry". Half is the structural midpoint, not a fitted number.
_REVERSAL_PROBABILITY = 0.5


@dataclass(frozen=True)
class PositionDecision:
    thesis_state: ThesisState
    thesis_action: ThesisAction
    profit_protection: str  # NONE | PROTECT | INSUFFICIENT_DATA | NOT_APPLICABLE
    action: ThesisAction
    proposed_stop_loss: Decimal | None
    reason_codes: list[str] = field(default_factory=list)
    mfe_context: dict = field(default_factory=dict)

    def as_thesis_decision(self) -> ThesisDecision:
        return ThesisDecision(
            state=self.thesis_state,
            action=self.action,
            reason_codes=self.reason_codes,
            proposed_stop_loss=self.proposed_stop_loss,
        )


def profit_protection_view(
    features: ThesisFeatures, thesis_state: ThesisState, estimate: MfeEstimate | None
) -> tuple[str, list[str]]:
    """Component A. Only speaks when there is open profit, the thesis is
    not STRONG, and history has enough trades at this level."""
    if features.unrealized_pnl <= _ZERO:
        return "NOT_APPLICABLE", ["no_open_profit"]
    if thesis_state == "STRONG":
        return "NONE", ["strong_thesis_lets_profit_run"]
    if estimate is None or estimate.status != "ESTIMATE":
        return "INSUFFICIENT_DATA", ["mfe_history_insufficient"]
    if (estimate.p_revert_to_entry or 0.0) >= _REVERSAL_PROBABILITY:
        return "PROTECT", ["similar_trades_usually_revert_to_entry"]
    return "NONE", ["similar_trades_usually_keep_their_profit"]


def decide_position(
    position: Position,
    features: ThesisFeatures,
    thresholds: ThesisThresholds,
    estimate: MfeEstimate | None,
) -> PositionDecision:
    thesis = evaluate_thesis(position, features, thresholds)
    protection, protection_reasons = profit_protection_view(features, thesis.state, estimate)
    reasons = [*thesis.reason_codes, *protection_reasons]

    action: ThesisAction = thesis.action
    stop = thesis.proposed_stop_loss
    if thesis.action in ("EXIT", "TIGHTEN_SL", "REDUCE"):
        pass
    elif thesis.action == "PROTECT" or protection == "PROTECT":
        breakeven = _tighter_stop(position, position.simulated_fill_entry)
        if breakeven is not None and features.unrealized_pnl > _ZERO:
            action, stop = "TIGHTEN_SL", breakeven
            reasons.append("protect_at_break_even")
        else:
            action, stop = "PROTECT", None
    else:
        action, stop = "HOLD", None

    decision = PositionDecision(
        thesis_state=thesis.state,
        thesis_action=thesis.action,
        profit_protection=protection,
        action=action,
        proposed_stop_loss=stop,
        reason_codes=reasons,
        mfe_context=estimate.as_dict() if estimate is not None else {},
    )
    violations = validate_action_is_safe(position, decision.as_thesis_decision())
    if violations:
        raise ValueError(f"unsafe position decision: {violations}")
    return decision
