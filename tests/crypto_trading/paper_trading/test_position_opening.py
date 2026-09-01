from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
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


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5,
        max_total_exposure_pct=Decimal("1.0"),
        spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"),
        fee_pct=Decimal("0.0004"),
        max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


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


def _confirmed_candidate(candidate_id="cand-1", status="CONFIRMED") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status=status,
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


def test_opens_position_with_theoretical_and_simulated_fields_separated(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()

    position = open_position_for_candidate(
        candidate,
        repo,
        _risk_limits(),
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-1",
    )

    assert position is not None
    assert position.theoretical_entry == Decimal("50000")
    assert position.simulated_fill_entry != position.theoretical_entry
    assert position.stop_loss == Decimal("49000")
    assert position.target == Decimal("52000")
    assert position.status == "OPEN_POSITION"


def test_position_id_equals_candidate_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate(candidate_id="cand-xyz")

    position = open_position_for_candidate(
        candidate,
        repo,
        _risk_limits(),
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-1",
    )

    assert position.position_id == "cand-xyz"


def test_calling_twice_for_same_candidate_creates_only_one_position(tmp_path):
    """AC6."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()

    first = open_position_for_candidate(
        candidate,
        repo,
        _risk_limits(),
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-1",
    )
    second = open_position_for_candidate(
        candidate,
        repo,
        _risk_limits(),
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-2",
    )

    assert first.position_id == second.position_id
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    assert count == 1


def test_returns_none_when_candidate_not_confirmed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate(status="NO_TRADE")

    result = open_position_for_candidate(
        candidate,
        repo,
        _risk_limits(),
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-1",
    )

    assert result is None


def test_returns_none_and_never_crashes_when_risk_suggestion_is_not_numeric(tmp_path):
    """Bugfix 2026-09-01: crypto-risk-agent kan legitimt svara med en
    kvalitativ/relativ beskrivning i stället för ett rent tal när dess
    kontext (orchestrator.py::_build_context()) saknar ett absolut
    referenspris - t.ex. "ca 3-4% under senaste pris" (verifierat mot en
    riktig CONFIRMED-candidate, FIL-USDT, 2026-09-01). Ett Decimal()-anrop
    på en sådan sträng får aldrig krascha hela discovery-ticken och
    därmed förlora alla andra candidaters redan färdiga resultat i samma
    tick - samma "en candidates dåliga data kraschar aldrig batchen"-
    princip som redan gäller överallt annars i kodbasen."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()
    candidate.risk.suggested_stop_loss = "ca 3-4% under senaste pris, inget absolut tal i underlaget"

    position = open_position_for_candidate(
        candidate,
        repo,
        _risk_limits(),
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-1",
    )

    assert position is None
    assert repo.get_position(candidate.candidate_id) is None


def test_direction_is_always_long(tmp_path):
    """Dokumenterar PLAN_CRYPTO_PHASE4.md beslut 1 som ett levande test."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()

    position = open_position_for_candidate(
        candidate,
        repo,
        _risk_limits(),
        reference_price=Decimal("50000"),
        opened_at=_NOW,
        run_id="run-1",
    )

    assert position.direction == "LONG"
