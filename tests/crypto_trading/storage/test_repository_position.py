import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _make_position(position_id="pos-1", candidate_id="cand-1", status="OPEN_POSITION") -> Position:
    return Position(
        position_id=position_id,
        candidate_id=candidate_id,
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
    )


def _make_event(position: Position, event_type: str) -> Event:
    return Event(
        event_id=f"{event_type}:{position.position_id}",
        event_type=event_type,
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={"instrument": position.instrument},
    )


def test_create_position_with_event_persists_both(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    position = _make_position()
    event = _make_event(position, "POSITION_OPENED")

    created = repo.create_position_with_event(position, event)

    assert created is True
    reloaded = repo.get_position("pos-1")
    assert reloaded is not None
    assert reloaded.status == "OPEN_POSITION"
    assert reloaded.simulated_fill_entry == position.simulated_fill_entry
    row = repo._conn.execute(
        "SELECT event_type FROM events WHERE event_id = ?", (event.event_id,)
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "POSITION_OPENED"


def test_create_position_with_event_is_idempotent_on_retry(tmp_path):
    """AC6: samma CONFIRMED-event processat två gånger skapar inte två positioner."""
    repo = SQLiteRepository(tmp_path / "test.db")
    position = _make_position()
    event = _make_event(position, "POSITION_OPENED")

    first = repo.create_position_with_event(position, event)
    second = repo.create_position_with_event(position, event)

    assert first is True
    assert second is False
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    assert count == 1
    event_count = repo._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert event_count == 1


def test_get_position_returns_none_when_missing(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.get_position("does-not-exist") is None


def test_find_open_positions_returns_only_open_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    open_pos = _make_position(position_id="pos-open", candidate_id="cand-open")
    closed_pos = _make_position(
        position_id="pos-closed", candidate_id="cand-closed", status="CLOSED"
    )
    repo.create_position_with_event(open_pos, _make_event(open_pos, "POSITION_OPENED"))
    repo.create_position_with_event(closed_pos, _make_event(closed_pos, "POSITION_OPENED"))

    result = repo.find_open_positions()

    assert [p.position_id for p in result] == ["pos-open"]


def test_close_position_with_event_updates_exit_fields_and_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    position = _make_position()
    repo.create_position_with_event(position, _make_event(position, "POSITION_OPENED"))

    close_event = _make_event(position, "POSITION_CLOSED")
    repo.close_position_with_event(
        position_id="pos-1",
        theoretical_exit="49000",
        simulated_fill_exit="48950",
        exit_reason="stop_loss",
        fees="2",
        funding="1",
        closed_at=_NOW,
        event=close_event,
    )

    reloaded = repo.get_position("pos-1")
    assert reloaded.status == "CLOSED"
    assert reloaded.exit_reason == "stop_loss"
    assert reloaded.simulated_fill_exit == Decimal("48950")
    assert reloaded.theoretical_exit == Decimal("49000")
    assert reloaded.fees == Decimal("2")
    assert reloaded.funding == Decimal("1")
    assert reloaded.closed_at == _NOW


def test_close_position_with_event_is_atomic_on_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    position = _make_position()
    repo.create_position_with_event(position, _make_event(position, "POSITION_OPENED"))

    class _FailingConnection:
        def __init__(self, real_conn, fail_on_call_number: int):
            self._real_conn = real_conn
            self._fail_on_call_number = fail_on_call_number
            self._call_count = 0

        def execute(self, sql, *args, **kwargs):
            self._call_count += 1
            if self._call_count == self._fail_on_call_number:
                raise sqlite3.OperationalError("simulated failure")
            return self._real_conn.execute(sql, *args, **kwargs)

        def commit(self):
            return self._real_conn.commit()

        def rollback(self):
            return self._real_conn.rollback()

    real_conn = repo._conn
    repo._conn = _FailingConnection(real_conn, fail_on_call_number=2)
    close_event = _make_event(position, "POSITION_CLOSED")

    with pytest.raises(sqlite3.OperationalError):
        repo.close_position_with_event(
            position_id="pos-1",
            theoretical_exit="49000",
            simulated_fill_exit="48950",
            exit_reason="stop_loss",
            fees="2",
            funding="1",
            closed_at=_NOW,
            event=close_event,
        )

    repo._conn = real_conn
    reloaded = repo.get_position("pos-1")
    assert reloaded.status == "OPEN_POSITION"  # oförändrat - rollback fungerade
    event_row = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (close_event.event_id,)
    ).fetchone()
    assert event_row is None
