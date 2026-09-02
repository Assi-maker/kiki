from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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
    # Root-cause-fix (2026-09-02): senaste ticker-pris vid evidens-
    # tillfället, INTE en del av evidence_record (och därmed aldrig en del
    # av compute_evidence_hash()/cooldown-/re-analys-jämförelsen - ett pris
    # som rör sig ska inte i sig trigga om-analys). Enda syftet: ge Risk
    # Agent något att förankra suggested_stop_loss/suggested_target mot
    # (se orchestrator.py::_build_context()) - utan det kan agenten aldrig
    # svara med ett absolut, Decimal-parsbart tal (position_opening.py),
    # bara en kvalitativ beskrivning som alltid misslyckas parsningen.
    reference_price: Decimal | None = None

    news_sentiment: NewsSentimentAssessment | None = None
    technical: TechnicalAssessment | None = None
    bull_thesis: BullThesisAssessment | None = None
    forecast: ForecastAssessment | None = None
    risk: RiskAssessment | None = None
    bear_adversarial: BearAdversarialAssessment | None = None
    qa: QAAssessment | None = None
