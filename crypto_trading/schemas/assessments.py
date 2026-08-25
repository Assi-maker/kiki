from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator

from crypto_trading.schemas.common import AssessmentStatus


class AssessmentBase(BaseModel):
    agent_name: str
    run_id: str
    created_at: datetime
    status: AssessmentStatus


class NewsSentimentAssessment(AssessmentBase):
    verified_facts: list[str]
    source_claims: list[str]
    interpretation: str


class TechnicalAssessment(AssessmentBase):
    market_data: dict
    interpretation: str


class BullThesisAssessment(AssessmentBase):
    hypothesis: str
    catalyst: str
    setup: str


class ForecastAssessment(AssessmentBase):
    scenario_probabilities: dict[str, float]
    horizon: str
    forecast_version: str

    @field_validator("scenario_probabilities")
    @classmethod
    def probabilities_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if not (0.999 <= total <= 1.001):
            raise ValueError(f"scenario_probabilities must sum to 1.0, got {total}")
        return v


class RiskAssessment(AssessmentBase):
    suggested_stop_loss: str
    suggested_target: str
    downside: str
    liquidity_risk: str
    model_risk: str
    timing_risk: str


class BearAdversarialAssessment(AssessmentBase):
    counterarguments: list[str]
    alternative_explanations: list[str]
    falsification_conditions: str


class QAAssessment(AssessmentBase):
    passed: bool
    violations: list[str]
