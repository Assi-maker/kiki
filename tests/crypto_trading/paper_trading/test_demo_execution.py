from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.demo_execution import (
    close_time_limit_positions,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _open_position(repo, position_id="pos-1", opened_at=_NOW) -> Position:
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
        opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    return position


class _SpyConnector:
    """`position_by_symbol` defaults every symbol to "still open" (a non-
    None dict) unless explicitly set to None - matching a real just-opened
    position that hasn't been closed by the exchange yet."""

    def __init__(self, order_result=None, raise_on_place=None):
        self.calls = []
        self._order_result = order_result or {"orderId": "ex-1", "avgPrice": "50010"}
        self._raise_on_place = raise_on_place
        self.order_lookup_result = None
        self.position_by_symbol: dict[str, dict | None] = {}

    def set_leverage(self, symbol, leverage=1, side="LONG"):
        self.calls.append(("set_leverage", symbol, leverage))
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.calls.append(("place_entry_order_with_sl_tp", kwargs))
        if self._raise_on_place is not None:
            raise self._raise_on_place
        return self._order_result

    def get_order_by_client_order_id(self, symbol, client_order_id):
        self.calls.append(("get_order_by_client_order_id", symbol, client_order_id))
        return self.order_lookup_result

    def get_position(self, symbol):
        self.calls.append(("get_position", symbol))
        return self.position_by_symbol.get(symbol, {"symbol": symbol, "positionAmt": "0.001"})

    def cancel_all_open_orders(self, symbol):
        self.calls.append(("cancel_all_open_orders", symbol))
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        self.calls.append(("close_position_market", symbol, quantity, client_order_id))
        return {"avgPrice": "49500"}


class _SpyMarketDataConnector:
    def __init__(self, last_price: str = "49000"):
        self._last_price = last_price

    def get_ticker(self, symbol):
        return {"lastPrice": self._last_price}


def test_process_pending_positions_places_one_order_and_marks_active(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector()

    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert row["entry_exchange_order_id"] == "ex-1"
    assert row["sl_exchange_order_id"] is None
    assert row["tp_exchange_order_id"] is None
    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 1


def test_process_pending_positions_never_places_twice_for_same_position(tmp_path):
    """Idempotency: simulates a duplicate call (e.g. two ticks racing, or a
    restart re-observing the same still-pending position)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector()

    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)

    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 1


def test_process_pending_positions_marks_failed_on_connector_error(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(raise_on_place=ConnectorUnavailableError("insufficient balance"))

    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "insufficient balance" in row["last_error"]
    # never touches the PAPER position itself
    position = repo.get_position("pos-1")
    assert position.status == "OPEN_POSITION"


def test_recover_stale_claims_resubmits_when_no_order_found(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW - timedelta(seconds=60))
    connector = _SpyConnector()
    connector.order_lookup_result = None  # nothing found on the exchange

    recover_stale_claims(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW, stale_after_seconds=30)

    assert repo.get_demo_execution("pos-1")["phase"] == "ACTIVE"
    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 1


def test_recover_stale_claims_adopts_existing_order_without_resubmitting(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW - timedelta(seconds=60))
    connector = _SpyConnector()
    connector.order_lookup_result = {"orderId": "ex-1", "avgPrice": "50010"}

    recover_stale_claims(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW, stale_after_seconds=30)

    assert repo.get_demo_execution("pos-1")["phase"] == "ACTIVE"
    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 0  # adopted the existing order, never resubmitted


def test_reconcile_active_executions_leaves_row_active_while_position_is_still_open(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector()
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)
    market_data = _SpyMarketDataConnector()

    reconcile_active_executions(repo, connector, market_data, "run-1", _NOW + timedelta(minutes=5))

    assert repo.get_demo_execution("pos-1")["phase"] == "ACTIVE"


def test_reconcile_active_executions_closes_and_classifies_stop_loss_when_position_goes_flat(
    tmp_path,
):
    """BingX exposes no separate order id for the attached SL/TP legs
    (confirmed live 2026-09-04) - reconciliation instead notices the
    position itself went flat and classifies the exit by comparing the
    observed price's distance to our own recorded stop_loss/target."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _open_position(repo)
    connector = _SpyConnector()
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)
    connector.position_by_symbol["BTC-USDT"] = None  # exchange closed it
    market_data = _SpyMarketDataConnector(last_price=str(position.stop_loss))

    reconcile_active_executions(repo, connector, market_data, "run-1", _NOW + timedelta(minutes=5))

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "stop_loss"
    assert row["exchange_fill_exit"] == str(position.stop_loss)


def test_reconcile_active_executions_classifies_target_when_price_is_closer_to_target(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _open_position(repo)
    connector = _SpyConnector()
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)
    connector.position_by_symbol["BTC-USDT"] = None
    market_data = _SpyMarketDataConnector(last_price=str(position.target))

    reconcile_active_executions(repo, connector, market_data, "run-1", _NOW + timedelta(minutes=5))

    assert repo.get_demo_execution("pos-1")["exit_reason"] == "target"


def test_close_time_limit_positions_closes_and_never_touches_positions_table(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    opened_at = _NOW - timedelta(hours=25)
    _open_position(repo, opened_at=opened_at)
    connector = _SpyConnector()
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", opened_at)

    close_time_limit_positions(repo, connector, max_position_hold_hours=24, run_id="run-1", now=_NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "TIME_LIMIT"
    assert ("cancel_all_open_orders", "BTC-USDT") in connector.calls
    position = repo.get_position("pos-1")
    assert position.status == "OPEN_POSITION"  # PAPER untouched
