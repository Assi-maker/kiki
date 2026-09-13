from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.connectors.bingx_live_trading import OrderRejectedError
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.live_profit_protection import (
    run_live_profit_protection_tick,
    unrealized_profit_pct,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
_THRESHOLD = Decimal("0.01")


def test_unrealized_profit_pct_positive_when_mark_above_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("101")) == Decimal("0.01")


def test_unrealized_profit_pct_negative_when_mark_below_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("98")) == Decimal("-0.02")


def test_unrealized_profit_pct_zero_at_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("100")) == Decimal("0")


def _open_active_live_position(
    repo, position_id="pos-1", entry_quantity="0.002", avg_entry=Decimal("50000")
) -> Position:
    """Seeds a positions row plus a live_executions row already in phase
    ACTIVE (mirrors the real state run_live_profit_protection_tick's
    caller-side reconciliation always produces before this module ever
    scans - see live_execution.py's reconcile_active_executions/
    update_live_execution_submitted), with the given entry_quantity so
    step 5's place_stop_loss_order call can be asserted against it."""
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=avg_entry,
        simulated_fill_entry=avg_entry, stop_loss=Decimal("49000"), target=Decimal("52000"),
        size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    repo.claim_live_execution(position_id, _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        position_id, "cid-1", "ex-1", entry_quantity, str(avg_entry), None, None, _NOW,
    )
    return position


class _SpyConnector:
    """Hand-written spy following this codebase's established convention
    (see test_live_execution.py's _SpyConnector): every configurable
    behavior (what a call returns, or raises) is an independent constructor
    flag, and every call is recorded in `.calls` (a full trace, used to
    assert a connector was NEVER touched at all - e.g. for a position that
    already has a live_profit_protection row) as well as method-specific
    lists (`place_calls`, `cancel_calls`) for asserting exact arguments and
    call counts. Deliberately has NO take-profit-related method at all -
    its absence, and it never being called, IS the proof TP is never
    touched by this module."""

    def __init__(
        self,
        positions=None,
        position_sequence=None,
        open_orders=None,
        place_sl_raises=None,
        lookup_order=None,
        lookup_raises=None,
        cancel_raises=None,
    ):
        self.calls: list[tuple] = []
        self.place_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls = 0
        self._positions = positions if positions is not None else []
        self._position_sequence = list(position_sequence) if position_sequence is not None else None
        self._open_orders = open_orders if open_orders is not None else []
        self._place_sl_raises = place_sl_raises
        self._lookup_order = lookup_order
        self._lookup_raises = lookup_raises
        self._cancel_raises = cancel_raises

    def get_position(self, symbol):
        self.calls.append(("get_position", symbol))
        if self._position_sequence is not None:
            if self._position_sequence:
                return self._position_sequence.pop(0)
            return None
        for p in self._positions:
            if p.get("symbol") == symbol:
                return p
        return None

    def get_open_orders(self, symbol):
        self.calls.append(("get_open_orders", symbol))
        return self._open_orders

    def place_stop_loss_order(self, symbol, quantity, stop_price, client_order_id):
        call = {
            "symbol": symbol, "quantity": quantity, "stop_price": stop_price,
            "client_order_id": client_order_id,
        }
        self.place_calls.append(call)
        self.calls.append(("place_stop_loss_order", client_order_id))
        if self._place_sl_raises is not None:
            raise self._place_sl_raises
        return {"orderId": "new-sl-1", "status": "NEW"}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        self.lookup_calls += 1
        self.calls.append(("get_order_by_client_order_id", client_order_id))
        if self._lookup_raises is not None:
            raise self._lookup_raises
        return self._lookup_order

    def cancel_order(self, symbol, order_id):
        self.cancel_calls.append(order_id)
        self.calls.append(("cancel_order", order_id))
        if self._cancel_raises is not None:
            raise self._cancel_raises
        return {}


_ABOVE_THRESHOLD_POSITION = {"symbol": "BTC-USDT", "avgPrice": "50000", "markPrice": "50600"}  # +1.2%
_ONE_OLD_SL = [{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"}]


# --- 1. Normal success -------------------------------------------------

def test_run_tick_normal_success_yields_sl_replaced(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row is not None
    assert row["status"] == "SL_REPLACED"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["old_sl_price"] == "49000"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert row["breakeven_price"] == "50000"
    assert connector.place_calls == [{
        "symbol": "BTC-USDT", "quantity": "0.002", "stop_price": "50000",
        "client_order_id": row["new_sl_client_order_id"],
    }]
    assert connector.cancel_calls == ["old-sl-1"]


# --- 2. New SL rejected -------------------------------------------------

def test_run_tick_new_sl_rejected_aborts_and_never_cancels_old(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        place_sl_raises=OrderRejectedError("TP Price must be greater than Last Price"),
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ABORTED_NEW_SL_REJECTED"
    assert row["old_sl_order_id"] == "old-sl-1"  # recorded before the rejected placement
    assert row["new_sl_order_id"] is None
    assert connector.cancel_calls == []


# --- 3. Old SL cancel fails after new SL verified -----------------------

def test_run_tick_old_sl_cancel_fails_yields_replacement_partial(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "PENDING"},
        cancel_raises=ConnectorUnavailableError("network blip cancelling old SL"),
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "REPLACEMENT_PARTIAL"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert len(connector.place_calls) == 1  # no third order ever placed
    assert connector.cancel_calls == ["old-sl-1"]  # exactly one cancel attempt


# --- 4. New SL status cannot be determined ------------------------------

def test_run_tick_new_sl_lookup_raises_yields_uncertain_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_raises=ConnectorUnavailableError("timeout verifying new SL"),
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert row["new_sl_order_id"] is None
    assert connector.cancel_calls == []


def test_run_tick_new_sl_unrecognized_status_yields_uncertain_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "SOME_FUTURE_STATUS"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert connector.cancel_calls == []


# --- 5/6. Ambiguous existing SL count ------------------------------------

def test_run_tick_zero_existing_sl_orders_aborts_ambiguous(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[_ABOVE_THRESHOLD_POSITION], open_orders=[])

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ABORTED_AMBIGUOUS_SL"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_run_tick_two_existing_sl_orders_aborts_ambiguous(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "old-sl-2", "stopPrice": "48900"},
        ],
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ABORTED_AMBIGUOUS_SL"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# --- 7. TP untouched in every scenario above -----------------------------
# Covered structurally: _SpyConnector above has no take-profit-related
# method whatsoever, so any code path that tried to touch TP would raise
# AttributeError and fail the relevant test loudly. Every assertion above
# also only ever asserts on `place_calls`/`cancel_calls`/`calls`, none of
# which could contain a TP operation this spy doesn't even expose.


# --- 8. Position already closed on the exchange at first check ----------

def test_run_tick_position_already_closed_creates_no_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[])  # get_position(instrument) -> None

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    assert repo.get_live_profit_protection("pos-1") is None
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# --- 9. Position closes between placing and verifying the new SL --------

def test_run_tick_position_closes_during_verification_yields_sl_replaced(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        position_sequence=[_ABOVE_THRESHOLD_POSITION, None],  # 1st: threshold check, 2nd: re-check after FILLED
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "FILLED"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert connector.cancel_calls == []  # no order operation follows a closure during verification


# --- 10. Below-threshold position is skipped entirely --------------------

def test_run_tick_below_threshold_skips_no_row_created(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[{"symbol": "BTC-USDT", "avgPrice": "50000", "markPrice": "50010"}],  # +0.02%
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    assert repo.get_live_profit_protection("pos-1") is None
    assert connector.place_calls == []


# --- 11. A position with an existing PP row is never re-examined --------

def test_run_tick_position_with_existing_pp_row_is_never_touched(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50600", "50000", "existing-cid-pp", _NOW,
    )
    connector = _SpyConnector(positions=[_ABOVE_THRESHOLD_POSITION])

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    assert connector.calls == []  # the connector is never touched at all for this position
    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "CLAIMED"  # left exactly as-is - Task 6's job to resolve
