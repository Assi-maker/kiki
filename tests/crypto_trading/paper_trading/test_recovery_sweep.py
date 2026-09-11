from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.recovery_sweep import sweep_confirmed_candidates_without_position
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"), risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5, max_total_exposure_pct=Decimal("1.0"),
        max_position_notional_usdt=Decimal("1000000"), spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"), fee_pct=Decimal("0.0004"), max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


def _evidence() -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT", timeframes=["1h"], evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.8, trigger_reasons=["price_volatility"],
        data_quality_status="ok", outcome="worth_deeper_analysis",
    )


def _confirmed_candidate(candidate_id="cand-1", instrument="BTCUSDT") -> Candidate:
    return Candidate(
        candidate_id=candidate_id, idempotency_key=f"key-{candidate_id}", instrument=instrument,
        discovery_run_id="run-1", evidence_hash="hash-1", status="CONFIRMED",
        evidence_record=_evidence(), created_at=_NOW, updated_at=_NOW,
        risk=RiskAssessment(
            agent_name="crypto-risk-agent", run_id="run-1", created_at=_NOW, status="ok",
            suggested_stop_loss="49000", suggested_target="52000",
            downside="d", liquidity_risk="l", model_risk="m", timing_risk="t",
        ),
    )


def _seed_confirmed_candidate(repo, candidate: Candidate, confirmed_at: datetime) -> None:
    create_event = Event(
        event_id=f"CANDIDATE_CREATED:{candidate.candidate_id}", event_type="CANDIDATE_CREATED",
        aggregate_type="candidate", aggregate_id=candidate.candidate_id, occurred_at=confirmed_at,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_candidate_with_event(candidate.model_copy(update={"status": "NEW"}), create_event)
    repo.save_assessment(candidate.candidate_id, "risk", candidate.risk)
    transition_event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:CONFIRMED",
        event_type="CANDIDATE_TRANSITIONED", aggregate_type="candidate",
        aggregate_id=candidate.candidate_id, occurred_at=confirmed_at, run_id="seed",
        schema_version=1, payload={"from": "UNDER_AI_ANALYSIS", "to": "CONFIRMED"},
    )
    repo.transition_candidate_with_event(
        candidate.candidate_id, "CONFIRMED", confirmed_at, transition_event
    )


class _TickerStubConnector:
    def __init__(self, tickers=None, raise_for=None):
        self._tickers = tickers or {}
        self._raise_for = raise_for or {}

    def get_ticker(self, symbol):
        if symbol in self._raise_for:
            raise self._raise_for[symbol]
        return self._tickers[symbol]


def _raw_ticker(symbol: str, last_price: str) -> dict:
    return {
        "symbol": symbol, "lastPrice": last_price, "priceChange": "0", "priceChangePercent": "0",
        "highPrice": last_price, "lowPrice": last_price, "volume": "100", "quoteVolume": "1000000",
        "openPrice": last_price, "askPrice": last_price, "askQty": "1", "bidPrice": last_price,
        "bidQty": "1", "closeTime": int(_NOW.timestamp() * 1000),
    }


def test_opens_a_position_for_a_confirmed_candidate_forward_of_activation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()
    activated_at = _NOW - timedelta(minutes=10)
    repo.set_recovery_sweep_activated_at_if_missing(activated_at)
    _seed_confirmed_candidate(repo, candidate, confirmed_at=_NOW - timedelta(minutes=5))
    connector = _TickerStubConnector(tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "50000")})

    opened = sweep_confirmed_candidates_without_position(
        repo, connector, _risk_limits(), _NOW, "run-1"
    )

    assert len(opened) == 1
    assert opened[0].position_id == "cand-1"
    assert opened[0].theoretical_entry == Decimal("50000")
    assert repo.get_position("cand-1") is not None


def test_never_auto_opens_a_candidate_confirmed_before_first_activation(tmp_path):
    """The core forward-only guarantee: a CONFIRMED candidate that already
    existed (as an orphan) BEFORE the sweep's very first call must never be
    auto-opened, even though it's structurally indistinguishable from a
    'genuine' recovery target."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()
    _seed_confirmed_candidate(repo, candidate, confirmed_at=_NOW - timedelta(days=3))
    connector = _TickerStubConnector(tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "50000")})

    opened = sweep_confirmed_candidates_without_position(
        repo, connector, _risk_limits(), _NOW, "run-1"
    )

    assert opened == []
    assert repo.get_position("cand-1") is None
    # the watermark itself was still set, anchored to THIS first call
    assert repo.get_recovery_sweep_activated_at() == _NOW


def test_is_idempotent_when_candidate_already_has_a_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()
    repo.set_recovery_sweep_activated_at_if_missing(_NOW - timedelta(minutes=10))
    _seed_confirmed_candidate(repo, candidate, confirmed_at=_NOW - timedelta(minutes=5))
    connector = _TickerStubConnector(tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "50000")})

    first = sweep_confirmed_candidates_without_position(
        repo, connector, _risk_limits(), _NOW, "run-1"
    )
    second = sweep_confirmed_candidates_without_position(
        repo, connector, _risk_limits(), _NOW + timedelta(seconds=1), "run-2"
    )

    assert len(first) == 1
    assert second == []  # already has a position - nothing left to recover
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    assert count == 1


def test_skips_candidate_on_connector_failure_without_crashing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()
    repo.set_recovery_sweep_activated_at_if_missing(_NOW - timedelta(minutes=10))
    _seed_confirmed_candidate(repo, candidate, confirmed_at=_NOW - timedelta(minutes=5))
    connector = _TickerStubConnector(raise_for={"BTCUSDT": ConnectorUnavailableError("nere")})

    opened = sweep_confirmed_candidates_without_position(
        repo, connector, _risk_limits(), _NOW, "run-1"
    )  # must never raise

    assert opened == []
    assert repo.get_position("cand-1") is None
