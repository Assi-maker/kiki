from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)

OpportunityStatus = Literal[
    "candidate", "under_review", "approved", "rejected", "reported", "evaluated"
]


class Opportunity(BaseModel):
    opportunity_id: str
    event_id: str
    created_at: datetime
    category: str
    title: str
    summary: str
    time_horizon: str
    liquidity: str
    status: OpportunityStatus = "candidate"

    research: ResearchAssessment | None = None
    opportunity: OpportunityAssessment | None = None
    market: MarketAssessment | None = None
    forecast: ForecastAssessment | None = None
    risk: RiskAssessment | None = None
    bear: BearAssessment | None = None
    qa: QAAssessment | None = None

    score: float | None = None
    score_breakdown: dict | None = None
