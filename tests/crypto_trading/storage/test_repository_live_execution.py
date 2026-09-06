from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _open_position(repo: SQLiteRepository, position_id: str = "pos-1") -> Position:
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTC-USDT",
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


def test_claim_live_execution_is_idempotent_and_records_sizing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)

    first = repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    second = repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    assert first is True
    assert second is False
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLAIMED"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"
    assert row["leverage"] == "10"


def test_find_positions_pending_live_execution_excludes_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    _open_position(repo, "pos-2")
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    pending = repo.find_positions_pending_live_execution(limit=10)

    assert [p.position_id for p in pending] == ["pos-2"]


def test_update_live_execution_submitted_then_close(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.update_live_execution_submitted(
        "pos-1",
        entry_client_order_id="cid-1",
        entry_exchange_order_id="ex-1",
        entry_quantity="0.002",
        exchange_fill_entry="50030",
        sl_exchange_order_id=None,
        tp_exchange_order_id=None,
        updated_at=_NOW,
    )
    active = repo.find_active_live_executions()
    assert len(active) == 1
    assert active[0]["phase"] == "ACTIVE"

    repo.close_live_execution(
        "pos-1", "target", "52100", _NOW + timedelta(hours=1),
        realized_fees_usdt="0.08", realized_funding_usdt="-0.01",
    )
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "target"
    assert row["realized_fees_usdt"] == "0.08"
    assert row["realized_funding_usdt"] == "-0.01"
    assert repo.find_active_live_executions() == []


def test_mark_live_execution_failed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.mark_live_execution_failed("pos-1", "ConnectorUnavailableError: boom", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "boom" in row["last_error"]


def test_mark_live_execution_skipped(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.mark_live_execution_skipped("pos-1", "below_exchange_minimum", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "SKIPPED"
    assert row["last_error"] == "below_exchange_minimum"
    # SKIPPED is terminal and must never be retried:
    assert repo.find_positions_pending_live_execution(limit=10) == []


def test_find_stale_claimed_live_executions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    not_yet_stale = repo.find_stale_claimed_live_executions(_NOW - timedelta(seconds=1))
    stale = repo.find_stale_claimed_live_executions(_NOW + timedelta(seconds=31))

    assert not_yet_stale == []
    assert len(stale) == 1
    assert stale[0]["position_id"] == "pos-1"


def test_live_execution_never_writes_to_positions_table(tmp_path):
    """Isolation guarantee (spec §3): every repository method touching
    live_executions must leave the positions row exactly as it was."""
    repo = SQLiteRepository(tmp_path / "t.db")
    before = _open_position(repo)

    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50030", None, None, _NOW
    )
    repo.close_live_execution("pos-1", "target", "52100", _NOW)

    after = repo.get_position("pos-1")
    assert after == before
    assert after.status == "OPEN_POSITION"  # untouched by live_execution close
