from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.detective.context import (
    build_position_analysis_context,
    signal_type_for_candidate,
)
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.trade import Position

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_LATER = datetime(2026, 9, 4, 18, 0, tzinfo=UTC)


def _evidence(trigger_reasons=("price_volatility",)) -> CandidateEvidenceRecord:
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
        trigger_reasons=list(trigger_reasons),
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _candidate(trigger_reasons=("price_volatility",)) -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        idempotency_key="key-1",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CONFIRMED",
        evidence_record=_evidence(trigger_reasons),
        created_at=_NOW,
        updated_at=_NOW,
        risk=RiskAssessment(
            agent_name="crypto-risk-agent",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            suggested_stop_loss="49000",
            suggested_target="52000",
            downside="d",
            liquidity_risk="l",
            model_risk="m",
            timing_risk="t",
        ),
    )


def _closed_position(pnl_favorable: bool = True) -> Position:
    return Position(
        position_id="cand-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="CLOSED",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=Decimal("49000"),
        target=Decimal("52000"),
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=_NOW,
        theoretical_exit=Decimal("52000") if pnl_favorable else Decimal("49000"),
        simulated_fill_exit=Decimal("51975") if pnl_favorable else Decimal("48975"),
        exit_reason="target" if pnl_favorable else "stop_loss",
        fees=Decimal("0.4"),
        funding=Decimal("0"),
        closed_at=_LATER,
    )


def test_build_position_analysis_context_includes_core_trade_fields():
    context = build_position_analysis_context(_closed_position(), _candidate(), None)

    assert context["position_id"] == "cand-1"
    assert context["instrument"] == "BTCUSDT"
    assert context["direction"] == "LONG"
    assert context["exit_reason"] == "target"
    assert context["hold_hours"] == 6.0
    assert Decimal(context["realized_pnl_usdt"]) > 0


def test_build_position_analysis_context_includes_all_seven_assessments_when_present():
    context = build_position_analysis_context(_closed_position(), _candidate(), None)

    assert "risk_assessment" in context
    assert context["risk_assessment"]["suggested_stop_loss"] == "49000"


def test_build_position_analysis_context_handles_missing_candidate_without_crashing():
    context = build_position_analysis_context(_closed_position(), None, None)

    assert context["position_id"] == "cand-1"
    assert "evidence_record" not in context


def test_build_position_analysis_context_includes_gate_decision_when_present():
    gate_decision = {"decision": "CONFIRMED", "reasons": ["all_checks_passed"], "evaluated_at": "x"}

    context = build_position_analysis_context(_closed_position(), _candidate(), gate_decision)

    assert context["gate_decision"] == gate_decision


def test_signal_type_for_candidate_joins_sorted_trigger_reasons():
    candidate = _candidate(trigger_reasons=["volume", "price_volatility"])
    assert signal_type_for_candidate(candidate) == "price_volatility,volume"


def test_signal_type_for_candidate_returns_unknown_for_missing_candidate():
    assert signal_type_for_candidate(None) == "unknown"


def test_signal_type_for_candidate_returns_unknown_for_empty_trigger_reasons():
    candidate = _candidate(trigger_reasons=[])
    assert signal_type_for_candidate(candidate) == "unknown"
