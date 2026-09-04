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


def _position(position_id: str, win: bool, size: Decimal = Decimal("1000")) -> Position:
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
        size=size,
        fill_model_version="v1",
        opened_at=_NOW,
        theoretical_exit=Decimal("52000") if win else Decimal("49000"),
        simulated_fill_exit=Decimal("51975") if win else Decimal("48975"),
        exit_reason="target" if win else "stop_loss",
        fees=Decimal("0.4"),
        funding=Decimal("0"),
        closed_at=_LATER,
    )


def _zero_size_position(position_id: str) -> Position:
    """En position vars `size` trycktes till 0 av max_total_exposure_pct-
    taket (paper_trading/position_sizing.py::compute_position_size()) -
    representerar noll verklig marknadsexponering, ska aldrig räknas som
    break-even i Detectives statistik (samma konvention som performance/
    paper_track_report.py::_is_blocked_by_exposure()). fees/funding är
    också 0 här - båda beräknas proportionellt mot size (paper_trading/
    position_closing.py::compute_fees()/compute_funding()), så en äkta
    nollstorlekstrade har verkligen exakt 0 i compute_pnl(), inte bara 0
    gross_pnl minus kvarvarande avgifter."""
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
        size=Decimal("0"),
        fill_model_version="v1",
        opened_at=_NOW,
        theoretical_exit=Decimal("52000"),
        simulated_fill_exit=Decimal("51975"),
        exit_reason="target",
        fees=Decimal("0"),
        funding=Decimal("0"),
        closed_at=_LATER,
    )


def test_compute_batch_win_loss_counts_counts_wins_and_losses():
    positions = [_position("p1", win=True), _position("p2", win=False), _position("p3", win=True)]

    counts = compute_batch_win_loss_counts(positions)

    assert counts == {
        "win_count": 2,
        "loss_count": 1,
        "breakeven_count": 0,
        "blocked_by_exposure_count": 0,
    }


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


def test_compute_batch_win_loss_counts_excludes_zero_size_positions_blocked_by_exposure():
    positions = [
        _position("p1", win=True),
        _position("p2", win=False),
        _zero_size_position("p3"),
        _zero_size_position("p4"),
    ]

    counts = compute_batch_win_loss_counts(positions)

    assert counts == {
        "win_count": 1,
        "loss_count": 1,
        "breakeven_count": 0,
        "blocked_by_exposure_count": 2,
    }


def test_compute_breakdown_by_signal_type_excludes_zero_size_positions_blocked_by_exposure():
    win_position = _position("p1", win=True)
    zero_size_position = _zero_size_position("p2")
    candidates_by_id = {
        "p1": _candidate("p1", ["momentum_breakout"]),
        "p2": _candidate("p2", ["momentum_breakout"]),
    }

    breakdown = compute_breakdown_by_signal_type(
        [win_position, zero_size_position], candidates_by_id
    )

    # Bara den riktiga tradet (p1) räknas - p2 (blocked by exposure)
    # exkluderas helt, spär inte ut win_rate/profit_factor/expectancy.
    assert breakdown["momentum_breakout"]["trade_count"] == 1
    assert breakdown["momentum_breakout"]["win_rate"] == "1"
