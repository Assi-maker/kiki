from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.common import DataQualityStatus


class PriceVolatilityEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class MomentumBreakoutEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class VolumeEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class FundingOpenInterestEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class CandidateEvidenceRecord(BaseModel):
    instrument: str
    timeframes: list[str]
    evaluated_at: datetime
    price_volatility_evidence: PriceVolatilityEvidence
    momentum_breakout_evidence: MomentumBreakoutEvidence
    volume_evidence: VolumeEvidence
    funding_oi_evidence: FundingOpenInterestEvidence
    candidate_score: float
    trigger_reasons: list[str]
    data_quality_status: DataQualityStatus
    outcome: Literal["worth_deeper_analysis", "not_a_candidate"]


def compute_evidence_hash(evidence: CandidateEvidenceRecord) -> str:
    """Hash av evidensens SEMANTISKA innehåll — evaluated_at exkluderas medvetet
    så att två beräkningar av samma underliggande evidens vid olika millisekund
    ger samma hash (SPEC §8.6 / Phase 0-design)."""
    data = evidence.model_dump(exclude={"evaluated_at"}, mode="json")
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_candidate_idempotency_key(
    instrument: str, discovery_run_id: str, evidence_hash: str
) -> str:
    normalized_instrument = instrument.strip().upper()
    raw = f"{normalized_instrument}\x1f{discovery_run_id}\x1f{evidence_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
