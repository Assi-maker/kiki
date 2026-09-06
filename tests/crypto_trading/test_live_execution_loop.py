from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.live_execution_loop import run_live_execution_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class _SpyConnector:
    def __init__(self, order_status="FILLED"):
        self.place_calls = 0
        self._order_status = order_status

    def set_leverage(self, symbol, leverage=10, side="LONG"):
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.place_calls += 1
        return {"orderId": "ex-1", "avgPrice": "50010"}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        return {
            "orderId": "ex-1", "status": self._order_status,
            "executedQty": "0.002" if self._order_status == "FILLED" else "0",
            "avgPrice": "50010",
        }

    def get_all_positions(self):
        return [{"symbol": "BTC-USDT", "positionAmt": "0.002"}] if self._order_status == "FILLED" else []

    def get_position(self, symbol):
        return {"symbol": symbol, "positionAmt": "0.002"} if self._order_status == "FILLED" else None

    def get_balance(self):
        return {"availableMargin": "100.00"}

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "0"}


class _SpyMarketDataConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "50000"}


def _seed_open_position(repo, position_id="pos-1"):
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)


def test_run_live_execution_tick_processes_pending_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector()

    run_live_execution_tick(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"


def test_run_live_execution_tick_resolves_a_pending_entry_across_ticks_without_duplicate_order(tmp_path):
    """End-to-end (2026-09-06 safety audit, Risk D fix): an order left
    uncertain (still "NEW") after one tick must be resolved - never
    resubmitted - by resolve_pending_entries() on a later tick."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector(order_status="NEW")

    run_live_execution_tick(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )
    assert repo.get_live_execution("pos-1")["phase"] == "ENTRY_SUBMITTED"
    assert connector.place_calls == 1

    connector._order_status = "FILLED"
    run_live_execution_tick(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW + timedelta(minutes=1),
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert connector.place_calls == 1  # still exactly one - resolution never resubmits


def test_run_live_execution_tick_never_crashes_the_caller_on_unexpected_error(tmp_path):
    class _ExplodingConnector(_SpyConnector):
        def get_balance(self):
            # get_balance() is on the real call path (has_sufficient_live_capacity,
            # called from process_pending_positions) - unlike get_all_positions,
            # which nothing in live_execution.py calls directly.
            raise RuntimeError("simulated crash")

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)

    # must not raise
    run_live_execution_tick(
        repo, _ExplodingConnector(), _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )
