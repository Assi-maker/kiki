from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.assessments import AssessmentBase

GuardianState = Literal["HOLD", "WATCH", "PROTECT", "EXIT"]


class GuardianObservation(BaseModel):
    """One append-only row (storage/db.py::guardian_observations). Shadow
    mode only (design doc §2/§10) - this model is never used to mutate
    positions/demo_executions, only ever inserted fresh."""

    observation_id: str
    position_id: str
    observed_at: datetime
    state: GuardianState
    decay_score: Decimal
    progress_ratio: Decimal
    unrealized_pnl: Decimal
    factors: dict[str, float]
    ai_reasoning: str | None = None
    ai_cost_usd: Decimal | None = None
    run_id: str


class GuardianAssessment(AssessmentBase):
    """AI output for ONE state-transition explanation (design doc §6).
    Deliberately has no state/decision field of any kind - the state is
    already decided by classify_guardian_state() (deterministic.py) before
    this is ever called; the AI can only narrate it."""

    reasoning: str
