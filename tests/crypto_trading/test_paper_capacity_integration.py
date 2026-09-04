from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig, get_settings
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
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

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5,
        max_total_exposure_pct=Decimal("0.25"),
        spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"),
        fee_pct=Decimal("0.0004"),
        max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


def _evidence(instrument: str) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument=instrument,
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


def _confirmed_candidate(i: int) -> Candidate:
    instrument = f"COIN{i}USDT"
    return Candidate(
        candidate_id=f"cand-{i}",
        idempotency_key=f"key-{i}",
        instrument=instrument,
        discovery_run_id="run-1",
        evidence_hash=f"hash-{i}",
        status="CONFIRMED",
        evidence_record=_evidence(instrument),
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


def _count_real_positions(risk_limits: RiskLimitsConfig, n: int, db_path) -> int:
    repo = SQLiteRepository(db_path)
    real = 0
    for i in range(n):
        candidate = _confirmed_candidate(i)
        position = open_position_for_candidate(
            candidate, repo, risk_limits, Decimal("50000"), _NOW, "run-1"
        )
        assert position is not None
        if position.size > 0:
            real += 1
    return real


def test_new_exposure_cap_allows_more_real_positions_than_old_25pct_cap(tmp_path):
    """Regressionstest för hela ändring 1: samma kandidater/stop-avstånd,
    jämfört mot BÅDA konfigurationerna - bevisar att höjningen mekaniskt
    ökar hur många CONFIRMED-kandidater som får en verklig (icke-nollstor)
    storlek, utan att röra risk_per_trade_pct eller sizingformeln. (Om detta
    fortfarande inte räcker för alla 15-20 en given dag beror det på det
    verkliga stop-avståndet Risk Agent föreslår, inte på denna gräns - se
    config/risk_limits.yaml:s kommentar.)"""
    old_risk_limits = _risk_limits(max_total_exposure_pct=Decimal("0.25"))
    new_risk_limits = _risk_limits(max_total_exposure_pct=Decimal("1.00"))

    old_real = _count_real_positions(old_risk_limits, 20, tmp_path / "old.db")
    new_real = _count_real_positions(new_risk_limits, 20, tmp_path / "new.db")

    assert new_real > old_real


def test_gate_still_blocks_at_new_higher_concurrent_position_cap_with_real_config(tmp_path):
    settings = get_settings()
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(settings.risk_limits.max_concurrent_positions):
        candidate = _confirmed_candidate(i)
        open_position_for_candidate(
            candidate, repo, settings.risk_limits, Decimal("50000"), _NOW, "run-1"
        )

    open_positions = repo.count_open_positions()
    assert open_positions == settings.risk_limits.max_concurrent_positions

    extra_candidate = _confirmed_candidate(999)
    decision = evaluate_risk_signal_gate(
        extra_candidate, open_positions, settings.risk_limits.max_concurrent_positions
    )
    assert decision.outcome == "NO_TRADE"


def test_opening_new_positions_never_mutates_already_open_ones(tmp_path):
    settings = get_settings()
    repo = SQLiteRepository(tmp_path / "t.db")
    first_candidate = _confirmed_candidate(0)
    first_position = open_position_for_candidate(
        first_candidate, repo, settings.risk_limits, Decimal("50000"), _NOW, "run-1"
    )

    for i in range(1, 5):
        open_position_for_candidate(
            _confirmed_candidate(i), repo, settings.risk_limits, Decimal("50000"), _NOW, "run-1"
        )

    reloaded_first = repo.get_position(first_position.position_id)
    assert reloaded_first.size == first_position.size
    assert reloaded_first.stop_loss == first_position.stop_loss
    assert reloaded_first.status == "OPEN_POSITION"
