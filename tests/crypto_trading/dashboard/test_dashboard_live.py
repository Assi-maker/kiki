from datetime import UTC, datetime

from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.notify.telegram import format_confirmed_message
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

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _evidence(score: float = 0.82) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=score,
        trigger_reasons=["price_volatility", "volume"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _candidate(candidate_id="cand-1", status="CANDIDATE") -> Candidate:
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
    )


def _candidate_event(candidate: Candidate) -> Event:
    return Event(
        event_id=f"CANDIDATE_CREATED:{candidate.candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=_NOW,
        run_id=candidate.discovery_run_id,
        schema_version=1,
        payload={},
    )


def _open_position(position_id="cand-1", candidate_id="cand-1") -> Position:
    return Position(
        position_id=position_id,
        candidate_id=candidate_id,
        instrument="BTCUSDT",
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


def _position_event(position: Position) -> Event:
    return Event(
        event_id=f"POSITION_OPENED:{position.position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def _client(tmp_path) -> TestClient:
    db_path = tmp_path / "test.db"
    repo = SQLiteRepository(db_path)
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app), repo


def test_live_shows_in_progress_candidates_and_open_positions(tmp_path):
    client, repo = _client(tmp_path)
    candidate = _candidate(status="CANDIDATE")
    repo.create_candidate_with_event(candidate, _candidate_event(candidate))
    position = _open_position()
    repo.create_position_with_event(position, _position_event(position))

    response = client.get("/api/live")

    assert response.status_code == 200
    body = response.json()
    assert [c["candidate_id"] for c in body["in_progress_candidates"]] == ["cand-1"]
    assert [p["position_id"] for p in body["open_positions"]] == ["cand-1"]
    open_position = body["open_positions"][0]
    assert open_position["entry"] == str(position.simulated_fill_entry)
    assert open_position["stop_loss"] == str(position.stop_loss)
    assert open_position["target"] == str(position.target)


def test_live_marks_top_n_and_unrealized_pnl_as_unavailable(tmp_path):
    client, repo = _client(tmp_path)
    position = _open_position()
    repo.create_position_with_event(position, _position_event(position))

    body = client.get("/api/live").json()

    assert body["top_n_instruments"] == "unavailable — not persisted historically"
    assert body["open_positions"][0]["unrealized_pnl"] == ("unavailable — live price not persisted")


def test_live_confirmed_candidate_matches_telegram_confirmed_message_fields(tmp_path):
    client, repo = _client(tmp_path)
    candidate = _candidate(status="CONFIRMED")
    repo.create_candidate_with_event(candidate, _candidate_event(candidate))
    repo.save_assessment(
        candidate.candidate_id,
        "bull_thesis",
        BullThesisAssessment(
            agent_name="crypto-bull-thesis",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            hypothesis="h",
            catalyst="c",
            setup="s",
        ),
    )
    repo.save_assessment(
        candidate.candidate_id,
        "forecast",
        ForecastAssessment(
            agent_name="crypto-forecast-agent",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            scenario_probabilities={"bullish": 0.6, "bearish": 0.4},
            horizon="4h",
            forecast_version="v1",
        ),
    )
    repo.save_assessment(
        candidate.candidate_id,
        "risk",
        RiskAssessment(
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
    position = _open_position()
    repo.create_position_with_event(position, _position_event(position))

    reloaded_candidate = repo.get_candidate(candidate.candidate_id)
    reloaded_position = repo.get_position(position.position_id)
    telegram_text = format_confirmed_message(reloaded_candidate, reloaded_position)

    body = client.get("/api/live").json()
    dashboard_confirmed = next(
        c for c in body["confirmed_candidates"] if c["candidate_id"] == candidate.candidate_id
    )
    dashboard_position = next(
        p for p in body["open_positions"] if p["position_id"] == position.position_id
    )

    expected_evidence = reloaded_candidate.evidence_record
    assert dashboard_confirmed["instrument"] == reloaded_candidate.instrument
    assert dashboard_confirmed["candidate_score"] == expected_evidence.candidate_score
    assert dashboard_confirmed["trigger_reasons"] == expected_evidence.trigger_reasons
    assert dashboard_position["entry"] == str(reloaded_position.simulated_fill_entry)
    assert dashboard_position["stop_loss"] == str(reloaded_position.stop_loss)
    assert dashboard_position["target"] == str(reloaded_position.target)
    # Samma tal ska bevisligen synas i den faktiska Telegram-notistexten för samma event.
    assert str(reloaded_position.simulated_fill_entry) in telegram_text
    assert str(reloaded_position.stop_loss) in telegram_text
    assert str(reloaded_position.target) in telegram_text
    assert reloaded_candidate.instrument in telegram_text


def test_live_risk_exposure_matches_repository_and_config_values(tmp_path):
    client, repo = _client(tmp_path)
    settings = get_settings()
    pos_a = _open_position(position_id="pos-a", candidate_id="cand-a")
    pos_b = _open_position(position_id="pos-b", candidate_id="cand-b")
    repo.create_position_with_event(pos_a, _position_event(pos_a))
    repo.create_position_with_event(pos_b, _position_event(pos_b))

    body = client.get("/api/live").json()

    assert body["risk_exposure"]["open_positions_count"] == repo.count_open_positions()
    assert body["risk_exposure"]["open_positions_notional"] == str(
        repo.sum_open_positions_notional()
    )
    assert body["risk_exposure"]["max_concurrent_positions"] == (
        settings.risk_limits.max_concurrent_positions
    )
    assert body["risk_exposure"]["max_total_exposure_pct"] == str(
        settings.risk_limits.max_total_exposure_pct
    )
