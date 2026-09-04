from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_trading.schemas.guardian import GuardianAssessment, GuardianObservation

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_guardian_observation_accepts_all_fields():
    obs = GuardianObservation(
        observation_id="pos-1:2026-09-04T12:00:00+00:00",
        position_id="pos-1",
        observed_at=_NOW,
        state="WATCH",
        decay_score=Decimal("0.42"),
        progress_ratio=Decimal("0.3"),
        unrealized_pnl=Decimal("15.50"),
        factors={"time_decay": 0.1, "momentum_decay": 0.5},
        ai_reasoning=None,
        ai_cost_usd=None,
        run_id="run-1",
    )
    assert obs.state == "WATCH"


def test_guardian_observation_rejects_invalid_state():
    with pytest.raises(ValidationError):
        GuardianObservation(
            observation_id="pos-1:2026-09-04T12:00:00+00:00",
            position_id="pos-1",
            observed_at=_NOW,
            state="BOGUS",
            decay_score=Decimal("0.1"),
            progress_ratio=Decimal("0.1"),
            unrealized_pnl=Decimal("0"),
            factors={},
            run_id="run-1",
        )


def test_guardian_assessment_has_only_reasoning_no_state_field():
    assessment = GuardianAssessment(
        agent_name="crypto-guardian", run_id="run-1", created_at=_NOW, status="ok",
        reasoning="Momentum has faded but volume remains supportive.",
    )
    assert not hasattr(assessment, "state")
    assert assessment.reasoning.startswith("Momentum")
