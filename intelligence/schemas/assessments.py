from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

AssessmentStatus = Literal["ok", "failed", "timeout"]


class AssessmentBase(BaseModel):
    agent_name: str
    run_id: str
    created_at: datetime
    status: AssessmentStatus


class ResearchAssessment(AssessmentBase):
    verified_facts: list[str]
    source_references: list[str]
    assumptions: list[str]


class OpportunityAssessment(AssessmentBase):
    observed_data: str
    hypothesis: str
    interpretation: str


class MarketAssessment(AssessmentBase):
    market_data: dict
    interpretation: str


class ForecastAssessment(AssessmentBase):
    scenarios: list[dict]
    confidence: float
    uncertainty: str


class RiskAssessment(AssessmentBase):
    downside: str
    liquidity_risk: str
    model_risk: str
    timing_risk: str


class BearAssessment(AssessmentBase):
    counterarguments: list[str]
    alternative_explanations: list[str]
    falsification_conditions: str


class QAAssessment(AssessmentBase):
    passed: bool
    violations: list[str]
