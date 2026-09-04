from __future__ import annotations

from decimal import Decimal

from crypto_trading.schemas.candidate import Candidate

_ASSESSMENT_ROLES_FOR_CONTEXT = ("bull_thesis", "risk", "forecast")


def should_invoke_ai(previous_observation: dict | None, new_state: str) -> bool:
    """AI fires only on a transition INTO or BETWEEN non-HOLD states
    (design doc §6) - never every tick, never for a return to HOLD."""
    if new_state == "HOLD":
        return False
    if previous_observation is None:
        return True
    return previous_observation["state"] != new_state


def build_ai_context(
    candidate: Candidate | None,
    factors: dict[str, Decimal],
    decay_score: Decimal,
    progress_ratio: Decimal,
    unrealized_pnl: Decimal,
    new_state: str,
) -> dict:
    context: dict = {
        "new_state": new_state,
        "decay_score": str(decay_score),
        "progress_ratio": str(progress_ratio),
        "unrealized_pnl_usdt": str(unrealized_pnl),
        "factors": {name: str(value) for name, value in factors.items()},
    }
    if candidate is not None:
        for role in _ASSESSMENT_ROLES_FOR_CONTEXT:
            assessment = getattr(candidate, role)
            if assessment is not None:
                context[f"{role}_assessment"] = assessment.model_dump(mode="json")
    return context
