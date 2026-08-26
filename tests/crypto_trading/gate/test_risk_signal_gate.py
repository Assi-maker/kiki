from datetime import UTC, datetime

from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
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


def _news(status="ok") -> NewsSentimentAssessment:
    return NewsSentimentAssessment(
        agent_name="crypto-news-sentiment",
        run_id="run-1",
        created_at=_NOW,
        status=status,
        verified_facts=["f"],
        source_claims=["c"],
        interpretation="i",
    )


def _technical(status="ok") -> TechnicalAssessment:
    return TechnicalAssessment(
        agent_name="crypto-technical-analyst",
        run_id="run-1",
        created_at=_NOW,
        status=status,
        market_data={},
        interpretation="i",
    )


def _bull(status="ok") -> BullThesisAssessment:
    return BullThesisAssessment(
        agent_name="crypto-bull-thesis",
        run_id="run-1",
        created_at=_NOW,
        status=status,
        hypothesis="h",
        catalyst="c",
        setup="s",
    )


def _forecast(status="ok") -> ForecastAssessment:
    return ForecastAssessment(
        agent_name="crypto-forecast-agent",
        run_id="run-1",
        created_at=_NOW,
        status=status,
        scenario_probabilities={"bullish": 0.6, "neutral": 0.3, "bearish": 0.1},
        horizon="4h",
        forecast_version="v1",
    )


def _risk(status="ok") -> RiskAssessment:
    return RiskAssessment(
        agent_name="crypto-risk-agent",
        run_id="run-1",
        created_at=_NOW,
        status=status,
        suggested_stop_loss="1",
        suggested_target="2",
        downside="d",
        liquidity_risk="l",
        model_risk="m",
        timing_risk="t",
    )


def _bear(status="ok") -> BearAdversarialAssessment:
    return BearAdversarialAssessment(
        agent_name="crypto-bear-adversarial",
        run_id="run-1",
        created_at=_NOW,
        status=status,
        counterarguments=["c"],
        alternative_explanations=["a"],
        falsification_conditions="f",
    )


def _qa(status="ok", passed=True) -> QAAssessment:
    return QAAssessment(
        agent_name="crypto-qa-gate",
        run_id="run-1",
        created_at=_NOW,
        status=status,
        passed=passed,
        violations=[],
    )


def _full_candidate(**overrides) -> Candidate:
    defaults = dict(
        news_sentiment=_news(),
        technical=_technical(),
        bull_thesis=_bull(),
        forecast=_forecast(),
        risk=_risk(),
        bear_adversarial=_bear(),
        qa=_qa(),
    )
    defaults.update(overrides)
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
        **defaults,
    )


def test_missing_risk_assessment_blocks_confirmed():
    """AC1."""
    candidate = _full_candidate(risk=None)
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome != "CONFIRMED"
    assert decision.outcome == "NO_TRADE"


def test_missing_bear_adversarial_assessment_blocks_confirmed():
    """AC2."""
    candidate = _full_candidate(bear_adversarial=None)
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome != "CONFIRMED"
    assert decision.outcome == "NO_TRADE"


def test_qa_passed_false_results_in_rejected():
    """AC3."""
    candidate = _full_candidate(qa=_qa(passed=False))
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome == "REJECTED"


def test_gate_blocks_confirmed_even_when_all_seven_assessments_are_positive():
    """AC4: gaten har egna oberoende regler som kan neka oavsett AI-utfall."""
    candidate = _full_candidate()  # alla sju "ok", qa.passed=True
    decision = evaluate_risk_signal_gate(candidate, open_positions=5, max_concurrent_positions=5)
    assert decision.outcome == "NO_TRADE"
    assert any("max_concurrent_positions" in r for r in decision.reasons)


def test_gate_confirms_when_everything_passes_and_capacity_available():
    candidate = _full_candidate()
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome == "CONFIRMED"


def test_failed_status_assessment_blocks_confirmed_and_is_not_rejected():
    """Precisering: infrafel -> NO_TRADE, aldrig REJECTED (se Global Constraints)."""
    candidate = _full_candidate(risk=_risk(status="failed"))
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome == "NO_TRADE"


def test_timeout_status_assessment_blocks_confirmed_and_is_not_rejected():
    candidate = _full_candidate(forecast=_forecast(status="timeout"))
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome == "NO_TRADE"
