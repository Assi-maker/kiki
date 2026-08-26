from datetime import UTC, datetime

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.gate.qa_gate import run_qa_gate
from crypto_trading.schemas.assessments import QAAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _evidence() -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.8,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _full_candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        idempotency_key="key-1",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="UNDER_AI_ANALYSIS",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _qa_assessment(passed=True) -> QAAssessment:
    return QAAssessment(
        agent_name="crypto-qa-gate",
        run_id="run-1",
        created_at=_NOW,
        status="ok",
        passed=passed,
        violations=[],
    )


def test_run_qa_gate_returns_qa_assessment_from_runner():
    candidate = _full_candidate()
    runner = MockAgentRunner(fixtures={"crypto-qa-gate": _qa_assessment()})
    result = run_qa_gate(candidate, runner, run_id="run-1")
    assert result.passed is True


def test_run_qa_gate_propagates_failed_status():
    candidate = _full_candidate()
    runner = MockAgentRunner(
        fixtures={"crypto-qa-gate": _qa_assessment()}, fail_agents={"crypto-qa-gate"}
    )
    result = run_qa_gate(candidate, runner, run_id="run-1")
    assert result.status == "failed"
