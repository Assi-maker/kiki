from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _position(position_id, size, status="OPEN_POSITION") -> Position:
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
        size=size,
        fill_model_version="v1",
        opened_at=_NOW,
    )


def _event(position: Position) -> Event:
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


def test_sum_open_positions_notional_returns_zero_when_none(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.sum_open_positions_notional() == 0


def test_sum_open_positions_notional_sums_only_open_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    open_a = _position("pos-a", "1000")
    open_b = _position("pos-b", "2000")
    closed = _position("pos-c", "5000", status="CLOSED")
    repo.create_position_with_event(open_a, _event(open_a))
    repo.create_position_with_event(open_b, _event(open_b))
    repo.create_position_with_event(closed, _event(closed))

    assert repo.sum_open_positions_notional() == Decimal("3000")
