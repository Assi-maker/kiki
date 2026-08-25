from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.common import CandidateStatus
from crypto_trading.schemas.evidence import CandidateEvidenceRecord


class Candidate(BaseModel):
    candidate_id: str
    idempotency_key: str
    instrument: str
    discovery_run_id: str
    evidence_hash: str
    status: CandidateStatus
    evidence_record: CandidateEvidenceRecord
    created_at: datetime
    updated_at: datetime

    news_sentiment: NewsSentimentAssessment | None = None
    technical: TechnicalAssessment | None = None
    bull_thesis: BullThesisAssessment | None = None
    forecast: ForecastAssessment | None = None
    risk: RiskAssessment | None = None
    bear_adversarial: BearAdversarialAssessment | None = None
    qa: QAAssessment | None = None
