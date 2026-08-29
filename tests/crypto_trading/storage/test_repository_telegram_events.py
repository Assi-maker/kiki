from datetime import UTC, datetime

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

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _make_evidence() -> CandidateEvidenceRecord:
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


def _make_candidate(candidate_id="cand-1", status="CONFIRMED") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status=status,
        evidence_record=_make_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_candidate_event(candidate: Candidate) -> Event:
    return Event(
        event_id=f"CANDIDATE_CREATED:{candidate.candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def _make_position(position_id="pos-1", status="CLOSED") -> Position:
    return Position(
        position_id=position_id,
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status=status,
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_NOW,
        theoretical_exit="52000" if status == "CLOSED" else None,
        simulated_fill_exit="51980" if status == "CLOSED" else None,
        exit_reason="target" if status == "CLOSED" else None,
        fees="5" if status == "CLOSED" else None,
        funding="0" if status == "CLOSED" else None,
        closed_at=_NOW if status == "CLOSED" else None,
    )


def _make_position_event(position: Position) -> Event:
    return Event(
        event_id=f"POSITION_CREATED:{position.position_id}",
        event_type="POSITION_CREATED",
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def test_record_telegram_event_persists_a_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    result = repo.record_telegram_event("CONFIRMED:cand-1", "CONFIRMED", _NOW)
    assert result is True
    assert repo.has_telegram_event_been_sent("CONFIRMED:cand-1") is True


def test_record_telegram_event_is_idempotent(tmp_path):
    """AC3 (idempotens): samma telegram_event_id skriven två gånger ska
    aldrig ge en dubblettnotis-möjlighet - andra anropet returnerar False
    och lämnar exakt en rad, samma INSERT OR IGNORE-mönster som redan
    används för AI_CALL_MADE-events."""
    repo = SQLiteRepository(tmp_path / "t.db")
    first = repo.record_telegram_event("CONFIRMED:cand-1", "CONFIRMED", _NOW)
    second = repo.record_telegram_event("CONFIRMED:cand-1", "CONFIRMED", _NOW)

    assert first is True
    assert second is False
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM telegram_events").fetchone()["n"]
    assert count == 1


def test_has_telegram_event_been_sent_reflects_state(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.has_telegram_event_been_sent("CONFIRMED:cand-1") is False
    repo.record_telegram_event("CONFIRMED:cand-1", "CONFIRMED", _NOW)
    assert repo.has_telegram_event_been_sent("CONFIRMED:cand-1") is True


def test_find_candidates_pending_notification_excludes_already_notified(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    cand_a = _make_candidate("cand-a", status="CONFIRMED")
    cand_b = _make_candidate("cand-b", status="CONFIRMED")
    repo.create_candidate_with_event(cand_a, _make_candidate_event(cand_a))
    repo.create_candidate_with_event(cand_b, _make_candidate_event(cand_b))
    repo.record_telegram_event("CONFIRMED:cand-a", "CONFIRMED", _NOW)

    pending = repo.find_candidates_pending_notification("CONFIRMED")

    assert [c.candidate_id for c in pending] == ["cand-b"]


def test_find_candidates_pending_notification_filters_by_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    confirmed = _make_candidate("cand-confirmed", status="CONFIRMED")
    rejected = _make_candidate("cand-rejected", status="REJECTED")
    repo.create_candidate_with_event(confirmed, _make_candidate_event(confirmed))
    repo.create_candidate_with_event(rejected, _make_candidate_event(rejected))

    pending = repo.find_candidates_pending_notification("CONFIRMED")

    assert [c.candidate_id for c in pending] == ["cand-confirmed"]


def test_find_positions_pending_notification_excludes_already_notified(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    pos_a = _make_position("pos-a", status="CLOSED")
    pos_b = _make_position("pos-b", status="CLOSED")
    repo.create_position_with_event(pos_a, _make_position_event(pos_a))
    repo.create_position_with_event(pos_b, _make_position_event(pos_b))
    repo.record_telegram_event("CLOSED:pos-a", "CLOSED", _NOW)

    pending = repo.find_positions_pending_notification()

    assert [p.position_id for p in pending] == ["pos-b"]


def test_find_positions_pending_notification_only_returns_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    open_pos = _make_position("pos-open", status="OPEN_POSITION")
    closed_pos = _make_position("pos-closed", status="CLOSED")
    repo.create_position_with_event(open_pos, _make_position_event(open_pos))
    repo.create_position_with_event(closed_pos, _make_position_event(closed_pos))

    pending = repo.find_positions_pending_notification()

    assert [p.position_id for p in pending] == ["pos-closed"]
