from datetime import UTC, datetime

from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.notify.telegram import format_closed_message
from crypto_trading.paper_trading.execution import compute_pnl
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
        candidate_score=0.82,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _candidate(candidate_id: str, created_at: datetime, status: str = "CANDIDATE") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status=status,
        evidence_record=_evidence(),
        created_at=created_at,
        updated_at=created_at,
    )


def _candidate_event(candidate: Candidate) -> Event:
    return Event(
        event_id=f"CANDIDATE_CREATED:{candidate.candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=candidate.created_at,
        run_id=candidate.discovery_run_id,
        schema_version=1,
        payload={},
    )


def _position(position_id: str, opened_at: datetime, status: str = "OPEN_POSITION") -> Position:
    return Position(
        position_id=position_id,
        candidate_id=f"cand-{position_id}",
        instrument="BTCUSDT",
        direction="LONG",
        status=status,
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=opened_at,
    )


def _position_event(position: Position) -> Event:
    return Event(
        event_id=f"POSITION_OPENED:{position.position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=position.opened_at,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    repo = SQLiteRepository(db_path)
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app), repo


def test_trade_history_lists_all_candidates_and_positions_paginated(tmp_path):
    client, repo = _client(tmp_path)
    for i, hour in enumerate([10, 11, 12]):
        candidate = _candidate(f"cand-{i}", datetime(2026, 8, 30, hour, tzinfo=UTC))
        repo.create_candidate_with_event(candidate, _candidate_event(candidate))
        position = _position(f"pos-{i}", datetime(2026, 8, 30, hour, tzinfo=UTC))
        repo.create_position_with_event(position, _position_event(position))

    all_rows = client.get("/api/trade-history?limit=10").json()
    assert [c["candidate_id"] for c in all_rows["candidates"]] == ["cand-2", "cand-1", "cand-0"]
    assert [p["position_id"] for p in all_rows["positions"]] == ["pos-2", "pos-1", "pos-0"]

    first_page = client.get("/api/trade-history?limit=2").json()
    assert [c["candidate_id"] for c in first_page["candidates"]] == ["cand-2", "cand-1"]

    second_page = client.get("/api/trade-history?limit=2&offset=2").json()
    assert [c["candidate_id"] for c in second_page["candidates"]] == ["cand-0"]


def test_trade_history_marks_mfe_mae_as_not_tracked(tmp_path):
    client, repo = _client(tmp_path)
    position = _position("pos-1", _NOW)
    repo.create_position_with_event(position, _position_event(position))

    body = client.get("/api/trade-history?limit=10").json()

    row = body["positions"][0]
    assert row["mfe"] is None
    assert row["mae"] is None
    assert row["mfe_mae_status"] == "not yet tracked"


def test_trade_history_closed_position_pnl_matches_telegram_closed_message(tmp_path):
    client, repo = _client(tmp_path)
    position = _position("pos-1", _NOW, status="OPEN_POSITION")
    repo.create_position_with_event(position, _position_event(position))
    close_event = Event(
        event_id="POSITION_CLOSED:pos-1",
        event_type="POSITION_CLOSED",
        aggregate_type="position",
        aggregate_id="pos-1",
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )
    repo.close_position_with_event(
        position_id="pos-1",
        theoretical_exit="52000",
        simulated_fill_exit="51980",
        exit_reason="target",
        fees="2",
        funding="1",
        closed_at=_NOW,
        event=close_event,
    )
    reloaded = repo.get_position("pos-1")
    expected_pnl = compute_pnl(reloaded)
    telegram_text = format_closed_message(reloaded, forecast=None)

    body = client.get("/api/trade-history?limit=10").json()
    row = next(p for p in body["positions"] if p["position_id"] == "pos-1")

    assert row["pnl"] == str(expected_pnl)
    assert str(reloaded.simulated_fill_exit) in telegram_text
    assert row["exit_reason"] == "target"


def test_trade_history_open_position_has_no_pnl(tmp_path):
    client, repo = _client(tmp_path)
    position = _position("pos-1", _NOW, status="OPEN_POSITION")
    repo.create_position_with_event(position, _position_event(position))

    body = client.get("/api/trade-history?limit=10").json()

    row = body["positions"][0]
    assert row["pnl"] is None


def test_trade_history_includes_gate_decisions(tmp_path):
    client, repo = _client(tmp_path)
    candidate = _candidate("cand-1", _NOW)
    repo.create_candidate_with_event(candidate, _candidate_event(candidate))
    repo.save_gate_decision(
        "cand-1", decision="NO_TRADE", reasons=["max_concurrent_positions"], evaluated_at=_NOW
    )

    body = client.get("/api/trade-history?limit=10").json()

    row = next(c for c in body["candidates"] if c["candidate_id"] == "cand-1")
    assert row["gate_decision"]["decision"] == "NO_TRADE"
    assert row["gate_decision"]["reasons"] == ["max_concurrent_positions"]
