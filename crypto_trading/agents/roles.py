from __future__ import annotations

from pydantic import BaseModel

from crypto_trading.schemas.assessments import (
    AssessmentBase,
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)


class RoleSpec(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    agent_file: str
    assessment_type: type[AssessmentBase]


# Nyckelordning matchar SPEC §6:s agentordning exakt - orchestratorn itererar
# ROLE_MAP i denna ordning.
ROLE_MAP: dict[str, RoleSpec] = {
    "news_sentiment": RoleSpec(
        agent_file="crypto-news-sentiment.md", assessment_type=NewsSentimentAssessment
    ),
    "technical": RoleSpec(
        agent_file="crypto-technical-analyst.md", assessment_type=TechnicalAssessment
    ),
    "bull_thesis": RoleSpec(
        agent_file="crypto-bull-thesis.md", assessment_type=BullThesisAssessment
    ),
    "forecast": RoleSpec(agent_file="crypto-forecast-agent.md", assessment_type=ForecastAssessment),
    "risk": RoleSpec(agent_file="crypto-risk-agent.md", assessment_type=RiskAssessment),
    "bear_adversarial": RoleSpec(
        agent_file="crypto-bear-adversarial.md", assessment_type=BearAdversarialAssessment
    ),
    "qa": RoleSpec(agent_file="crypto-qa-gate.md", assessment_type=QAAssessment),
}
