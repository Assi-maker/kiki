from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.guardian.tick import run_guardian_tick_body
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord, FundingOpenInterestEvidence, MomentumBreakoutEvidence,
    PriceVolatilityEvidence, VolumeEvidence,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _raw_funding, _raw_kline, _raw_ticker, _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _placeholder_ev(**overrides):
    base = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    base.update(overrides)
    return base


# These three values are chosen to EXACTLY match what evaluate_candidate()
# itself computes from _StubConnector's flat klines (all closes "100", all
# volumes identical -> RSI=50 exactly per quant_screener.py's "helt platt
# fönster: neutralt RSI" rule, volume zscore=0 exactly per its "zero
# variance" rule) and its single funding entry (abs(0.0001)*100 = 0.01) -
# so a genuinely UNCHANGED market produces decay_score=0.0 exactly, not an
# arbitrary/mismatched placeholder that would silently produce some other
# state than the test's own name claims.
_MATCHING_ENTRY_RSI = 50.0
_MATCHING_ENTRY_VOLUME_ZSCORE = 0.0
_MATCHING_ENTRY_FUNDING_MAGNITUDE = 0.01


def _seed_candidate_and_position(repo, position_id="pos-1", opened_at=_NOW):
    evidence = CandidateEvidenceRecord(
        instrument="BTCUSDT", timeframes=["30m"], evaluated_at=opened_at,
        price_volatility_evidence=PriceVolatilityEvidence(**_placeholder_ev(value=3.0, threshold=2.0)),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_RSI, threshold=70.0)
        ),
        volume_evidence=VolumeEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_VOLUME_ZSCORE, threshold=2.5)
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_FUNDING_MAGNITUDE, threshold=0.05)
        ),
        candidate_score=0.5, trigger_reasons=["momentum_breakout"],
        data_quality_status="ok", outcome="worth_deeper_analysis",
    )
    candidate = Candidate(
        candidate_id=position_id, idempotency_key=f"key-{position_id}", instrument="BTCUSDT",
        discovery_run_id="run-0", evidence_hash="hash-1", status="CONFIRMED",
        evidence_record=evidence, created_at=opened_at, updated_at=opened_at,
        risk=RiskAssessment(
            agent_name="crypto-risk-agent", run_id="run-0", created_at=opened_at, status="ok",
            suggested_stop_loss="90", suggested_target="120", downside="d", liquidity_risk="l",
            model_risk="m", timing_risk="t",
        ),
    )
    repo.create_candidate_with_event(
        candidate,
        Event(event_id=f"CANDIDATE_CREATED:{position_id}", event_type="CANDIDATE_CREATED",
              aggregate_type="candidate", aggregate_id=position_id, occurred_at=opened_at,
              run_id="seed", schema_version=1, payload={}),
    )
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=opened_at,
    )
    repo.create_position_with_event(
        position,
        Event(event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
              run_id="seed", schema_version=1, payload={}),
    )
    return candidate, position


class _StubConnector:
    """Flat klines (all closes "100", identical volumes) dated backward
    from _NOW ending exactly at _NOW - the evidence builders reject any
    kline dated after evaluated_at (SPEC §8.4 no-future-data guard)."""

    def __init__(self, price="100"):
        self._price = price

    def get_klines(self, symbol, interval, limit=100):
        return [
            _raw_kline("100", int(_NOW.timestamp() * 1000) - (29 - i) * 60000) for i in range(30)
        ]

    def get_funding_rate(self, symbol, limit=1):
        return [_raw_funding(symbol, "0.0001", int(_NOW.timestamp() * 1000))]

    def get_ticker(self, symbol):
        return _raw_ticker(symbol, self._price, "1000000", int(_NOW.timestamp() * 1000))


class _FakeRunner:
    last_call_billed = True
    last_call_cost_usd = Decimal("0.01")

    def run(self, agent_def, context, response_model):
        return response_model(
            agent_name="crypto-guardian", run_id="run-1", created_at=_NOW, status="ok",
            reasoning="Momentum has faded materially since entry.",
        )


def test_run_guardian_tick_body_persists_a_hold_observation_without_ai(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    connector = _StubConnector(price="100")  # unchanged since entry -> HOLD

    observations = run_guardian_tick_body(repo, connector, _FakeRunner(), _settings(), "run-1", _NOW)

    assert len(observations) == 1
    assert observations[0].state == "HOLD"
    assert observations[0].ai_reasoning is None
    row = repo.find_latest_guardian_observation("pos-1")
    assert row["state"] == "HOLD"


def test_run_guardian_tick_body_skips_position_on_insufficient_data(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)

    class _EmptyConnector:
        def get_klines(self, symbol, interval, limit=100):
            return []

        def get_funding_rate(self, symbol, limit=1):
            return []

        def get_ticker(self, symbol):
            return _raw_ticker(symbol, "100", "1000000", int(_NOW.timestamp() * 1000))

    observations = run_guardian_tick_body(repo, _EmptyConnector(), _FakeRunner(), _settings(), "run-1", _NOW)

    assert observations == []
    assert repo.find_latest_guardian_observation("pos-1") is None


def test_run_guardian_tick_body_never_touches_positions_table(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _candidate, position = _seed_candidate_and_position(repo)
    before = repo.get_position("pos-1")
    connector = _StubConnector(price="100")

    run_guardian_tick_body(repo, connector, _FakeRunner(), _settings(), "run-1", _NOW)

    after = repo.get_position("pos-1")
    assert after == before


def test_run_guardian_tick_body_still_persists_observation_when_budget_exhausted(tmp_path):
    """Forces a non-HOLD state WITHOUT touching the stub's flat kline data
    (which would also perturb the momentum/volume/funding factors in ways
    that are hard to hand-verify) - instead uses two independently
    controllable, exactly-computable levers: opened_at far enough in the
    past to drive time_decay_factor to exactly 1.0 (elapsed 30h vs.
    risk_limits.max_position_hold_hours=24 -> clipped to 1.0), and a
    lowered watch_decay_threshold so that decay_score's contribution from
    time_decay ALONE (1.0 / 6 equally-weighted factors = 0.1667) is enough
    to cross into WATCH. Every other factor stays at 0 (matching entry
    evidence, per _seed_candidate_and_position's docstring above)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    opened_at = _NOW - timedelta(hours=30)  # exceeds max_position_hold_hours=24 -> time_decay=1.0
    _seed_candidate_and_position(repo, opened_at=opened_at)
    for i in range(600):
        repo.record_ai_call_event(
            Event(event_id=f"AI_CALL_MADE:exhaust:{i}", event_type="AI_CALL_MADE",
                  aggregate_type="candidate", aggregate_id="exhaust", occurred_at=_NOW,
                  run_id="run-0", schema_version=1, payload={"role": "risk", "status": "ok", "cost_usd": "10.00"}),
        )
    connector = _StubConnector(price="100")  # matches entry - only time_decay drives the state here
    settings = _settings().model_copy(
        update={
            "guardian": GuardianConfig(
                watch_decay_threshold=Decimal("0.05"),
                protect_decay_threshold=Decimal("0.5"),
                exit_decay_threshold=Decimal("0.9"),
            )
        }
    )

    observations = run_guardian_tick_body(repo, connector, _FakeRunner(), settings, "run-1", _NOW)

    assert len(observations) == 1
    assert observations[0].state == "WATCH"  # first observation, non-HOLD -> should_invoke_ai() would be True
    assert observations[0].ai_reasoning is None  # ...but budget exhaustion still blocked the call
