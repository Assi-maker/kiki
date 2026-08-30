from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    repo = SQLiteRepository(db_path)
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app), repo


def _open_position(position_id: str) -> Position:
    return Position(
        position_id=position_id,
        candidate_id=f"cand-{position_id}",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="100",
        simulated_fill_entry="100",
        stop_loss="90",
        target="110",
        size="1000",
        fill_model_version="v1",
        opened_at=_NOW,
    )


def _position_event(position: Position, event_type: str = "POSITION_OPENED") -> Event:
    return Event(
        event_id=f"{event_type}:{position.position_id}",
        event_type=event_type,
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def _open_and_close(repo, position_id, entry, exit_price, closed_at):
    position = _open_position(position_id)
    position = position.model_copy(
        update={"theoretical_entry": entry, "simulated_fill_entry": entry}
    )
    repo.create_position_with_event(position, _position_event(position))
    close_event = _position_event(position, "POSITION_CLOSED")
    repo.close_position_with_event(
        position_id=position_id,
        theoretical_exit=exit_price,
        simulated_fill_exit=exit_price,
        exit_reason="target",
        fees="0",
        funding="0",
        closed_at=closed_at,
        event=close_event,
    )


def test_dashboard_performance_returns_real_numbers_for_seeded_closed_positions(tmp_path):
    client, repo = _client(tmp_path)
    _open_and_close(repo, "win", "100", "110", datetime(2026, 8, 30, 10, tzinfo=UTC))
    _open_and_close(repo, "loss", "100", "90", datetime(2026, 8, 30, 11, tzinfo=UTC))

    body = client.get("/api/performance").json()

    assert body["trade_count"] == 2
    assert Decimal(body["cumulative_pnl"]) == Decimal("0")  # +100 + -100
    assert Decimal(body["win_rate"]) == Decimal("1") / Decimal("2")
    assert Decimal(body["expectancy"]) == Decimal("0")
    assert len(body["equity_curve"]) == 2
    assert set(body["by_instrument"].keys()) == {"BTCUSDT"}
    assert body["by_instrument"]["BTCUSDT"]["trade_count"] == 2
    assert set(body["by_direction"].keys()) == {"LONG"}


def test_dashboard_performance_zero_trades_gives_null_metrics_not_zero(tmp_path):
    """Samma disciplin genom hela HTTP-vägen som performance/metrics.py:s
    egna tester: en tom historik ger äkta null för odefinierade mått,
    aldrig ett fabricerat 0."""
    client, _repo = _client(tmp_path)

    body = client.get("/api/performance").json()

    assert body["trade_count"] == 0
    assert Decimal(body["cumulative_pnl"]) == Decimal("0")
    assert body["win_rate"] is None
    assert body["expectancy"] is None
    assert body["profit_factor"] is None
    assert body["max_drawdown"] is None
    assert body["equity_curve"] == []
    assert body["by_instrument"] == {}
    assert body["by_direction"] == {}


def test_dashboard_performance_ignores_still_open_positions(tmp_path):
    client, repo = _client(tmp_path)
    open_pos = _open_position("open")
    repo.create_position_with_event(open_pos, _position_event(open_pos))

    body = client.get("/api/performance").json()

    assert body["trade_count"] == 0
