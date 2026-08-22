from __future__ import annotations

from pydantic import BaseModel

from intelligence.schemas.assessments import (
    AssessmentBase,
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)


class RoleSpec(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    agent_file: str
    assessment_type: type[AssessmentBase]


ROLE_MAP: dict[str, RoleSpec] = {
    "research": RoleSpec(
        agent_file="research-agent.md", assessment_type=ResearchAssessment
    ),
    "opportunity": RoleSpec(
        agent_file="opportunity-hunter.md", assessment_type=OpportunityAssessment
    ),
    "market": RoleSpec(
        agent_file="trading-research.md", assessment_type=MarketAssessment
    ),
    "forecast": RoleSpec(
        agent_file="forecasting-agent.md", assessment_type=ForecastAssessment
    ),
    "risk": RoleSpec(agent_file="risk-agent.md", assessment_type=RiskAssessment),
    "bear": RoleSpec(
        agent_file="fact-checker-bear.md", assessment_type=BearAssessment
    ),
    "qa": RoleSpec(agent_file="qa-agent.md", assessment_type=QAAssessment),
}
