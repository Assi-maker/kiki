"""Full livscykel: en redan CONFIRMED candidate -> open_position_for_candidate
-> close_triggered_positions (stop_loss-scenario) -> verifierad slutgiltig
Position-rad."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.paper_trading.position_opening import open_position_for_candidate
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _risk_limits() -> RiskLimitsConfig:
    return RiskLimitsConfig(
        starting_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5,
        max_total_exposure_pct=Decimal("1.0"),
        spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"),
        fee_pct=Decimal("0.0004"),
        max_position_hold_hours=24,
    )


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


def _confirmed_candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        idempotency_key="key-1",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CONFIRMED",
        evidence_record=_evidence(),
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


def test_full_lifecycle_candidate_confirmed_position_opened_and_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    risk_limits = _risk_limits()
    candidate = _confirmed_candidate()

    opened = open_position_for_candidate(
        candidate,
        repo,
        risk_limits,
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-1",
    )
    assert opened is not None
    assert opened.status == "OPEN_POSITION"

    # Pris gappar under stop_loss (49000) -> stop_loss-trigger.
    price_lookup = {
        "BTCUSDT": (Decimal("48000"), Decimal("50100"), Decimal("48100"), Decimal("0.0001"))
    }
    closed = close_triggered_positions(
        repo, price_lookup, now=_NOW + timedelta(hours=1), risk_limits=risk_limits, run_id="run-1"
    )

    assert len(closed) == 1
    final = repo.get_position(opened.position_id)
    assert final.status == "CLOSED"
    assert final.exit_reason == "stop_loss"
    assert final.simulated_fill_exit != final.theoretical_exit
    assert final.theoretical_exit == Decimal("48000")  # konservativ gap-fill, aldrig exakt 49000
    assert final.fees is not None
    assert final.funding is not None
