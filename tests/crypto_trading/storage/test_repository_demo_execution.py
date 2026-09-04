from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _open_position(repo: SQLiteRepository, position_id: str = "pos-1") -> Position:
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=Decimal("49000"),
        target=Decimal("52000"),
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=_NOW,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=_NOW,
        run_id="seed",
        schema_version=1,
        payload={},
    )
    repo.create_position_with_event(position, event)
    return position


def test_claim_demo_execution_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)

    first = repo.claim_demo_execution("pos-1", _NOW)
    second = repo.claim_demo_execution("pos-1", _NOW)

    assert first is True
    assert second is False
    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLAIMED"


def test_find_positions_pending_demo_execution_excludes_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    _open_position(repo, "pos-2")
    repo.claim_demo_execution("pos-1", _NOW)

    pending = repo.find_positions_pending_demo_execution(limit=10)

    assert [p.position_id for p in pending] == ["pos-2"]


def test_update_demo_execution_submitted_then_close(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW)

    repo.update_demo_execution_submitted(
        "pos-1",
        entry_client_order_id="cid-1",
        entry_exchange_order_id="ex-1",
        entry_quantity="0.02",
        exchange_fill_entry="50030",
        sl_exchange_order_id="sl-1",
        tp_exchange_order_id="tp-1",
        updated_at=_NOW,
    )
    active = repo.find_active_demo_executions()
    assert len(active) == 1
    assert active[0]["phase"] == "ACTIVE"
    assert active[0]["sl_exchange_order_id"] == "sl-1"

    repo.close_demo_execution("pos-1", "target", "52100", _NOW + timedelta(hours=1))
    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "target"
    assert repo.find_active_demo_executions() == []


def test_mark_demo_execution_failed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW)

    repo.mark_demo_execution_failed("pos-1", "ConnectorUnavailableError: boom", _NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "boom" in row["last_error"]


def test_find_stale_claimed_demo_executions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW)

    not_yet_stale = repo.find_stale_claimed_demo_executions(_NOW - timedelta(seconds=1))
    stale = repo.find_stale_claimed_demo_executions(_NOW + timedelta(seconds=31))

    assert not_yet_stale == []
    assert len(stale) == 1
    assert stale[0]["position_id"] == "pos-1"


def test_demo_execution_never_writes_to_positions_table(tmp_path):
    """Isolation guarantee (spec §3): every repository method touching
    demo_executions must leave the positions row exactly as it was."""
    repo = SQLiteRepository(tmp_path / "t.db")
    before = _open_position(repo)

    repo.claim_demo_execution("pos-1", _NOW)
    repo.update_demo_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.02", "50030", "sl-1", "tp-1", _NOW
    )
    repo.close_demo_execution("pos-1", "target", "52100", _NOW)

    after = repo.get_position("pos-1")
    assert after == before
    assert after.status == "OPEN_POSITION"  # untouched by demo_execution close
