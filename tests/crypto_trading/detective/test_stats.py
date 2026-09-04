from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.detective.stats import (
    compute_batch_win_loss_counts,
    compute_breakdown_by_signal_type,
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


def _evidence(trigger_reasons) -> CandidateEvidenceRecord:
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
        trigger_reasons=trigger_reasons,
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _candidate(candidate_id: str, trigger_reasons) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash=f"hash-{candidate_id}",
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


def _position(position_id: str, win: bool) -> Position:
    return Position(
        position_id=position_id,
        candidate_id=position_id,
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
        theoretical_exit=Decimal("52000") if win else Decimal("49000"),
        simulated_fill_exit=Decimal("51975") if win else Decimal("48975"),
        exit_reason="target" if win else "stop_loss",
        fees=Decimal("0.4"),
        funding=Decimal("0"),
        closed_at=_LATER,
    )


def test_compute_batch_win_loss_counts_counts_wins_and_losses():
    positions = [_position("p1", win=True), _position("p2", win=False), _position("p3", win=True)]

    counts = compute_batch_win_loss_counts(positions)

    assert counts == {"win_count": 2, "loss_count": 1, "breakeven_count": 0}


def test_compute_breakdown_by_signal_type_groups_and_reuses_metrics_formulas():
    win_position = _position("p1", win=True)
    loss_position = _position("p2", win=False)
    candidates_by_id = {
        "p1": _candidate("p1", ["momentum_breakout"]),
        "p2": _candidate("p2", ["price_volatility"]),
    }

    breakdown = compute_breakdown_by_signal_type([win_position, loss_position], candidates_by_id)

    assert breakdown["momentum_breakout"]["trade_count"] == 1
    assert breakdown["momentum_breakout"]["win_rate"] == "1"
    assert breakdown["price_volatility"]["trade_count"] == 1
    assert breakdown["price_volatility"]["win_rate"] == "0"


def test_compute_breakdown_by_signal_type_groups_missing_candidates_as_unknown():
    breakdown = compute_breakdown_by_signal_type([_position("p1", win=True)], {})

    assert "unknown" in breakdown
    assert breakdown["unknown"]["trade_count"] == 1
