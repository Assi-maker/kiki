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
        open_orders_raises_from_call=None,
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
        self._open_orders_call_count = 0
        self._open_orders_raises_from_call = open_orders_raises_from_call
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
        self._open_orders_call_count += 1
        self.calls.append(("get_open_orders", symbol))
        if (
            self._open_orders_raises_from_call is not None
            and self._open_orders_call_count >= self._open_orders_raises_from_call
        ):
            raise ConnectorUnavailableError("get_open_orders failed on a later call")
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


# positionAmt matches _open_active_live_position's default entry_quantity
# ("0.002") so the quantity-vs-exchange-position cross-check (deep-review
# fix 5) passes by default in every pre-existing test below; tests that
# specifically exercise the mismatch set a different positionAmt.
_ABOVE_THRESHOLD_POSITION = {
    "symbol": "BTC-USDT", "avgPrice": "50000", "markPrice": "50600", "positionAmt": "0.002",
}  # +1.2%
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


# --- Deep-review fix 1: invalid/non-positive entry_quantity must never ---
# --- reach a real order placement -----------------------------------------

def test_run_tick_zero_entry_quantity_aborts_before_any_placement(tmp_path):
    """Reproduces the reviewer's finding: live_execution.py's own
    _resolve_uncertain_entry can leave entry_quantity as "0" (e.g. a fill
    lookup response missing executedQty). Without a guard, that "0" would
    have been sent straight to place_stop_loss_order, the old SL would then
    have been cancelled, and the position reported SL_REPLACED while
    actually protected by a zero-quantity stop. This must abort BEFORE
    placement, with the old SL left completely untouched."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo, entry_quantity="0")
    connector = _SpyConnector(positions=[_ABOVE_THRESHOLD_POSITION], open_orders=_ONE_OLD_SL)

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ABORTED_INVALID_ENTRY_QUANTITY"
    assert row["old_sl_order_id"] == "old-sl-1"  # recorded, never cancelled
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# --- Deep-review fix 2: one bad position must never block the whole tick -

def test_run_tick_one_malformed_position_never_blocks_the_rest_of_the_batch(tmp_path):
    """A malformed exchange position payload (missing avgPrice/markPrice -
    a realistic partial/degraded API response) must not propagate out of
    the scan loop: this module is inserted before close_time_limit_positions
    in the real tick, so an uncaught exception here would silently disable
    time-limit exits for every OTHER live position for the rest of the
    tick, and - because the exception fires before any claim - the same
    position would then repeat the exact same failure every subsequent
    tick forever. Two positions: "pos-bad" (malformed) processed first,
    "pos-good" (fully valid) processed second - pos-good must still reach
    SL_REPLACED despite pos-bad blowing up first."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo, position_id="pos-bad")
    _open_active_live_position(repo, position_id="pos-good")
    connector = _SpyConnector(
        # 1st call: pos-bad's threshold check (malformed, raises).
        # 2nd call: pos-good's threshold check.
        # 3rd call: pos-good's pre-cancel re-check (deep-review fix 3) -
        # must still show the position open so pos-good's own sequence
        # completes normally and isn't itself a false failure.
        position_sequence=[
            {"symbol": "BTC-USDT"}, _ABOVE_THRESHOLD_POSITION, _ABOVE_THRESHOLD_POSITION,
        ],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    assert repo.get_live_profit_protection("pos-bad") is None  # never reached a claim
    good_row = repo.get_live_profit_protection("pos-good")
    assert good_row is not None
    assert good_row["status"] == "SL_REPLACED"  # pos-bad's exception did not block pos-good
    assert len(connector.place_calls) == 1


# --- Deep-review fix 3: orphan-SL race - re-check position before cancel -

def test_run_tick_position_closes_after_new_sl_verified_skips_cancel(tmp_path):
    """New SL verifies NEW/PENDING (not FILLED, so the existing FILLED-
    branch re-check never fires), but the real position has since gone
    flat (e.g. hit TP) in the window between verification and the old-SL
    cancel. Cancelling the old SL now would be a guess about an order that
    may no longer matter - must re-check get_position() immediately before
    cancel_order() and, if flat, skip the cancel entirely rather than
    assume BingX safely rejects stops on closed positions (the spec
    explicitly refuses to assume this)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        position_sequence=[_ABOVE_THRESHOLD_POSITION, None],  # 1st: threshold check, 2nd: pre-cancel re-check
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "POSITION_CLOSED_DURING_REPLACEMENT"
    assert row["new_sl_order_id"] == "new-sl-1"  # already recorded before the re-check
    assert connector.cancel_calls == []  # cancel skipped entirely, not attempted and failed


# --- Deep-review fix 4: SL_REPLACED must be written before the ------------
# --- informational final-state read, not after ----------------------------

def test_run_tick_final_state_read_failure_does_not_undo_sl_replaced(tmp_path):
    """The old SL cancel (the last IRREVERSIBLE step) already succeeded by
    the time the purely informational final get_open_orders() read runs.
    If that read throws, the already-true success must still be recorded
    as SL_REPLACED, never left stuck at CLAIMED."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
        open_orders_raises_from_call=2,  # 1st call (ambiguous-SL check) OK, 2nd (final check) raises
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert connector.cancel_calls == ["old-sl-1"]  # the real, irreversible cancel did happen


# --- Deep-review fix 5: entry_quantity must match the real position size -

def test_run_tick_entry_quantity_mismatch_with_exchange_position_aborts(tmp_path):
    """entry_quantity (locally recorded at entry) has diverged from the
    exchange's own positionAmt (e.g. a partial close since entry) by far
    more than a reasonable tolerance - placing a replacement SL sized to
    the stale, smaller local quantity would leave part of the real
    position unprotected once the old SL is cancelled. Must abort before
    ever placing the new SL, old SL left untouched."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo, entry_quantity="0.002")
    connector = _SpyConnector(
        positions=[{
            "symbol": "BTC-USDT", "avgPrice": "50000", "markPrice": "50600",
            "positionAmt": "0.01",  # 5x the locally-recorded entry_quantity
        }],
        open_orders=_ONE_OLD_SL,
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ABORTED_QUANTITY_MISMATCH"
    assert row["old_sl_order_id"] == "old-sl-1"  # recorded, never cancelled
    assert connector.place_calls == []
    assert connector.cancel_calls == []
