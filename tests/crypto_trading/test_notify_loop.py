from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.notify.telegram import TelegramSendError
from crypto_trading.notify_loop import run_notify_tick
from crypto_trading.schemas.assessments import (
    BullThesisAssessment,
    ForecastAssessment,
    RiskAssessment,
)
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class _StubNotifier:
    def __init__(self, fail_when_text_contains: str | None = None):
        self.sent: list[str] = []
        self._fail_when_text_contains = fail_when_text_contains

    def send(self, text: str) -> None:
        if self._fail_when_text_contains and self._fail_when_text_contains in text:
            raise TelegramSendError("stub failure")
        self.sent.append(text)


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


def _confirmed_assessments():
    return dict(
        bull_thesis=BullThesisAssessment(
            agent_name="crypto-bull-thesis",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            hypothesis="Momentum breakout",
            catalyst="Volume spike",
            setup="Continuation",
        ),
        forecast=ForecastAssessment(
            agent_name="crypto-forecast-agent",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            scenario_probabilities={"bullish": 0.6, "neutral": 0.3, "bearish": 0.1},
            horizon="4h",
            forecast_version="v1",
        ),
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


def _seed_confirmed_candidate_with_position(repo, candidate_id, instrument="BTCUSDT"):
    candidate = Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument=instrument,
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CONFIRMED",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
        **_confirmed_assessments(),
    )
    repo.create_candidate_with_event(
        candidate,
        Event(
            event_id=f"CANDIDATE_CREATED:{candidate_id}",
            event_type="CANDIDATE_CREATED",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=_NOW,
            run_id="run-1",
            schema_version=1,
            payload={},
        ),
    )
    for field_name in ("bull_thesis", "forecast", "risk"):
        repo.save_assessment(candidate_id, field_name, getattr(candidate, field_name))
    position = Position(
        position_id=candidate_id,
        candidate_id=candidate_id,
        instrument=instrument,
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{candidate_id}",
            event_type="POSITION_OPENED",
            aggregate_type="position",
            aggregate_id=candidate_id,
            occurred_at=_NOW,
            run_id="run-1",
            schema_version=1,
            payload={},
        ),
    )
    return candidate, position


def _seed_closed_position(repo, position_id, instrument="BTCUSDT"):
    """close_position_with_event() UPPDATERAR bara exit-fälten (theoretical_
    exit/simulated_fill_exit/exit_reason/fees/funding/closed_at) - de finns
    inte i create_position_with_event()s INSERT, så en position måste
    öppnas FÖRST och sedan stängas, precis som i produktion
    (position_opening.py -> position_closing.py), annars persisteras
    exit-fälten aldrig."""
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument=instrument,
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_CREATED:{position_id}",
            event_type="POSITION_CREATED",
            aggregate_type="position",
            aggregate_id=position_id,
            occurred_at=_NOW,
            run_id="run-1",
            schema_version=1,
            payload={},
        ),
    )
    repo.close_position_with_event(
        position_id=position_id,
        theoretical_exit=Decimal("52000"),
        simulated_fill_exit=Decimal("51980"),
        exit_reason="target",
        fees=Decimal("2"),
        funding=Decimal("1"),
        closed_at=_NOW,
        event=Event(
            event_id=f"POSITION_CLOSED:{position_id}",
            event_type="POSITION_CLOSED",
            aggregate_type="position",
            aggregate_id=position_id,
            occurred_at=_NOW,
            run_id="run-1",
            schema_version=1,
            payload={},
        ),
    )
    return repo.get_position(position_id)


def test_run_notify_tick_sends_confirmed_notification_and_records_idempotency(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_confirmed_candidate_with_position(repo, "cand-1")
    notifier = _StubNotifier()

    sent = run_notify_tick(notifier, repo, _settings())

    assert sent == 1
    assert len(notifier.sent) == 1
    assert "BTCUSDT" in notifier.sent[0]
    assert repo.has_telegram_event_been_sent("CONFIRMED:cand-1") is True


def test_run_notify_tick_never_sends_the_same_confirmed_notification_twice(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_confirmed_candidate_with_position(repo, "cand-1")
    notifier = _StubNotifier()

    run_notify_tick(notifier, repo, _settings())
    run_notify_tick(notifier, repo, _settings())

    assert len(notifier.sent) == 1  # andra ticket ska inte skicka en dubblett


def test_run_notify_tick_sends_closed_notification_and_records_idempotency(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "pos-1")
    notifier = _StubNotifier()

    sent = run_notify_tick(notifier, repo, _settings())

    assert sent == 1
    assert "BTCUSDT" in notifier.sent[0]
    assert repo.has_telegram_event_been_sent("CLOSED:pos-1") is True


def test_run_notify_tick_skips_confirmed_candidate_without_a_position_yet(tmp_path):
    """position_opening.py körs i samma cykel som gaten - men om den av
    någon anledning ännu inte hunnit skapa positionen ska notify_loop
    aldrig gissa fälten, bara vänta till nästa tick."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = Candidate(
        candidate_id="cand-no-position",
        idempotency_key="key-1",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CONFIRMED",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    repo.create_candidate_with_event(
        candidate,
        Event(
            event_id="CANDIDATE_CREATED:cand-no-position",
            event_type="CANDIDATE_CREATED",
            aggregate_type="candidate",
            aggregate_id="cand-no-position",
            occurred_at=_NOW,
            run_id="run-1",
            schema_version=1,
            payload={},
        ),
    )
    notifier = _StubNotifier()

    sent = run_notify_tick(notifier, repo, _settings())

    assert sent == 0
    assert notifier.sent == []
    assert repo.has_telegram_event_been_sent("CONFIRMED:cand-no-position") is False


def test_run_notify_tick_continues_after_one_send_failure(tmp_path):
    """Ett enskilt Telegram-sändningsfel för EN rad ska aldrig stoppa
    resten av tick:en - den raden förblir opersisterad i telegram_events
    (försöks igen nästa tick), men övriga rader skickas ändå."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_confirmed_candidate_with_position(repo, "cand-fail", instrument="ETHUSDT")
    _seed_confirmed_candidate_with_position(repo, "cand-ok", instrument="BTCUSDT")
    notifier = _StubNotifier(fail_when_text_contains="ETHUSDT")

    sent = run_notify_tick(notifier, repo, _settings())

    assert sent == 1
    assert any("BTCUSDT" in msg for msg in notifier.sent)
    assert repo.has_telegram_event_been_sent("CONFIRMED:cand-ok") is True
    assert repo.has_telegram_event_been_sent("CONFIRMED:cand-fail") is False


def test_run_notify_tick_persists_a_runs_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    run_notify_tick(_StubNotifier(), repo, _settings())

    row = repo._conn.execute(
        "SELECT run_type, status FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row["run_type"] == "notify"
    assert row["status"] == "ok"


def test_run_notify_tick_never_raises_on_unexpected_error(tmp_path, monkeypatch):
    """Samma yttre fail-safe-mönster som discovery_loop/monitoring_loop: ett
    genuint oväntat fel kraschar aldrig anroparen (run_forever)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_confirmed_candidate_with_position(repo, "cand-1")

    def _boom(*args, **kwargs):
        raise RuntimeError("oväntat programmeringsfel")

    monkeypatch.setattr(repo, "get_position", _boom)

    sent = run_notify_tick(_StubNotifier(), repo, _settings())  # ska aldrig kasta
    assert sent == 0
