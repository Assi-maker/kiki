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
        lookup_sequence=None,
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
        # Task 6: some recovery paths look up the new SL's client order id
        # TWICE in one tick (once by the recovery pass itself, once more by
        # a shared placement/verification helper it delegates to after a
        # not-found result) - lookup_sequence lets a test give each call a
        # different answer, same pattern as position_sequence above. Falls
        # back to the static lookup_order once exhausted (or if never set).
        self._lookup_sequence = list(lookup_sequence) if lookup_sequence is not None else None
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
        if self._lookup_sequence is not None:
            if self._lookup_sequence:
                return self._lookup_sequence.pop(0)
            return self._lookup_order
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


# --- 11. A position with an existing PP row is never re-examined by the --
# --- main scan (Task 6 note: a still-CLAIMED row IS now examined, but by --
# --- the restart-recovery pass, not this scan - see the Task 6 section --
# --- below for that behavior; a TERMINAL-status row is untouched by ------
# --- either) ---------------------------------------------------------------

def test_run_tick_position_with_terminal_status_is_never_touched_by_recovery_or_scan(tmp_path):
    """Spec test case 8: once a position's live_profit_protection row has
    reached ANY terminal status (not CLAIMED), neither the restart-recovery
    pass (which only ever reads rows via find_claimed_live_profit_protection,
    i.e. status == 'CLAIMED') nor the main scan (which skips any position
    with an existing row at all, terminal or not) may examine it again - the
    connector must not be touched for this position at all."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50600", "50000", "existing-cid-pp", _NOW,
    )
    repo.set_live_profit_protection_status("pos-1", "SL_REPLACED", _NOW)
    connector = _SpyConnector(positions=[_ABOVE_THRESHOLD_POSITION])

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    assert connector.calls == []  # the connector is never touched at all for this position
    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"  # left exactly as-is


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


# =========================================================================
# Task 6: restart/crash recovery for rows left CLAIMED by an interrupted
# prior tick (spec test cases 7 and 8; case labels A/B/C per the Task 6
# brief's decision tree, keyed off which of old_sl_order_id/new_sl_order_id
# are already populated on the row).
# =========================================================================


def _claim_row(repo, position_id="pos-1", new_sl_client_order_id="existing-cid-pp"):
    """Seeds a bare CLAIMED live_profit_protection row (Case A shape: no
    old_sl_order_id, no new_sl_order_id yet) for a position already opened
    via _open_active_live_position."""
    repo.claim_live_profit_protection(
        position_id, "0.01", "50600", "50000", new_sl_client_order_id, _NOW,
    )


# --- Case A: old_sl_order_id is None (crash before step 4 ever completed) -

def test_recovery_case_a_position_still_open_retries_from_scratch(tmp_path):
    """Nothing was ever placed on the exchange for this attempt (old SL not
    yet even identified) - this is exactly what _run_claimed_sequence
    already handles from scratch. Recovery must reuse the row's stored
    breakeven_price/new_sl_client_order_id (never mint a new client order
    id) and complete the sequence normally."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.place_calls == [{
        "symbol": "BTC-USDT", "quantity": "0.002", "stop_price": "50000",
        "client_order_id": "existing-cid-pp",  # the SAME id stored at claim time
    }]
    assert connector.cancel_calls == ["old-sl-1"]


def test_recovery_case_a_position_closed_yields_position_closed_before_pp(tmp_path):
    """Position already flat by the time recovery observes it, and no old
    SL was ever identified - nothing to protect, nothing was ever placed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    connector = _SpyConnector(positions=[])  # get_position(instrument) -> None

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "POSITION_CLOSED_BEFORE_PP"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# --- Case B: old_sl_order_id set, new_sl_order_id is None ------------------

def test_recovery_case_b_new_sl_not_found_retries_placement_and_completes(tmp_path):
    """New SL was never confirmed placed before the crash - safe to retry
    placement under the SAME deterministic client order id. The lookup is
    consulted twice in this scenario: once by the recovery pass itself
    (not found), once more by the shared placement/verification helper
    after the retried placement (this time found ACTIVE)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        # Fix 2: Case B now reads get_open_orders itself first (the
        # zero-protective-orders anomaly gate) - only old-sl-1 is present,
        # matching "new SL never confirmed placed yet".
        open_orders=_ONE_OLD_SL,
        lookup_sequence=[None, {"orderId": "new-sl-1", "status": "NEW"}],
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.place_calls == [{
        "symbol": "BTC-USDT", "quantity": "0.002", "stop_price": "50000",
        "client_order_id": "existing-cid-pp",
    }]
    assert connector.cancel_calls == ["old-sl-1"]


def test_recovery_case_b_new_sl_found_active_resumes_at_verified_tail(tmp_path):
    """New SL WAS placed successfully before the crash - resume right after
    verification: record it, re-check the position, cancel the old SL.
    Placement must never be retried once the new SL is confirmed active."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,  # Fix 2: Case B's zero-protective-orders anomaly gate reads this first
        lookup_order={"orderId": "new-sl-1", "status": "PENDING"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.place_calls == []  # never re-placed - already confirmed active
    assert connector.cancel_calls == ["old-sl-1"]


def test_recovery_case_b_new_sl_found_filled_position_flat_yields_sl_replaced(tmp_path):
    """New SL was found FILLED - the position closed at/near breakeven
    during the interrupted attempt, a valid outcome. Re-checking the real
    position confirms flat, so this resolves to SL_REPLACED with no further
    order operation (mirrors the normal sequence's own FILLED branch)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    connector = _SpyConnector(
        position_sequence=[_ABOVE_THRESHOLD_POSITION, None],  # 1st: recovery's flat-check, 2nd: FILLED re-check
        open_orders=_ONE_OLD_SL,  # Fix 2: Case B's zero-protective-orders anomaly gate reads this first
        lookup_order={"orderId": "new-sl-1", "status": "FILLED"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# --- Fix 2 (deep review of Task 6): Case B must detect the same ----------
# --- zero-protective-orders anomaly Case C already detects ---------------

def test_recovery_case_b_zero_open_orders_yields_anomaly_not_a_blind_placement(tmp_path):
    """Reviewer-reproduced gap: the old SL can be gone (externally
    cancelled, or triggered) by the time Case B runs, with the new SL never
    placed either - zero STOP_MARKET orders at all while the position is
    still open. Case B must detect this the same way Case C does, rather
    than placing a "fine, additive" new SL and later attempting to cancel
    an old SL that was never verified to exist, which could silently
    auto-heal an anomaly that should block for human review instead."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    connector = _SpyConnector(positions=[_ABOVE_THRESHOLD_POSITION], open_orders=[])

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ANOMALY_NO_PROTECTIVE_ORDER_FOUND"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# --- Fix 3 (deep review of Task 6): Case B must not collapse a genuine ---
# --- lookup error into "not found" and retry placement on that basis -----

def test_recovery_case_b_new_sl_lookup_error_leaves_row_claimed_never_retries_placement(tmp_path):
    """Reviewer-reproduced regression: a lookup ConnectTimeout (or any
    _UNKNOWN_OUTCOME_ERRORS) must NOT be treated the same as a genuine
    not-found response. Task 5's original discipline for an uncertain
    lookup is fail-CLOSED (never blindly retry a write whose outcome is
    unknown - _resolve_uncertain_entry only ever looks up, never
    resubmits). The row must stay CLAIMED for a later tick's recovery pass
    to try the lookup again - a real placement must never happen here."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_raises=ConnectorUnavailableError("ConnectTimeout looking up new SL"),
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "CLAIMED"  # left for the next tick's recovery pass to retry the lookup
    assert connector.place_calls == []  # a lookup error must NEVER trigger a real placement
    assert connector.cancel_calls == []


# --- Case C: new_sl_order_id is set (step 6 confirmed before the crash) ---

def test_recovery_case_c_both_orders_exist_cancel_succeeds(tmp_path):
    """Only the pre-cancel re-check, the cancel itself, or the final status
    write didn't complete before the crash. Both orders still genuinely
    exist on the exchange - attempt the cancel exactly once more."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "new-sl-1", "stopPrice": "50000"},
        ],
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert connector.place_calls == []  # no third order ever placed
    assert connector.cancel_calls == ["old-sl-1"]  # exactly one more cancel attempt


def test_recovery_case_c_both_orders_exist_cancel_fails_again(tmp_path):
    """The single evidence-based retry cancel fails again - permanently
    blocked, both order IDs retained, position stays protected by both."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "new-sl-1", "stopPrice": "50000"},
        ],
        cancel_raises=ConnectorUnavailableError("still unreachable cancelling old SL"),
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "REPLACEMENT_PARTIAL"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.cancel_calls == ["old-sl-1"]  # exactly one attempt, not retried further


def test_recovery_case_c_position_closes_between_identification_and_cancel_skips_cancel(tmp_path):
    """Final-whole-branch-review fix: Case C's both-present cancel path must
    go through the SAME pre-cancel get_position re-check the fresh path
    (_finalize_verified_active_new_sl, Task 5 deep-review fix 3) already
    has. Case C's own position-liveness check happens once, early - before
    the get_open_orders call that identifies old/new SL presence. If the
    position goes flat in the window between that check and the eventual
    cancel, cancelling the old SL now and writing SL_REPLACED would be
    exactly the "guess based on an unverified assumption" the design
    forbids - the fresh path already refuses to do this for the identical
    situation, so recovery must refuse it too. Must resolve to
    POSITION_CLOSED_DURING_REPLACEMENT with the cancel skipped entirely."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(
        # 1st call: Case C's own early position-liveness check (open).
        # 2nd call: the shared helper's pre-cancel re-check (flat).
        position_sequence=[_ABOVE_THRESHOLD_POSITION, None],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "new-sl-1", "stopPrice": "50000"},
        ],
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "POSITION_CLOSED_DURING_REPLACEMENT"
    assert connector.cancel_calls == []  # cancel skipped entirely, not attempted and failed


def test_recovery_case_c_old_sl_already_gone_yields_sl_replaced_directly(tmp_path):
    """The old SL is already gone from the exchange (cancel had actually
    already fully succeeded before the crash) - only the final status write
    never landed. No cancel is attempted again."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "new-sl-1", "stopPrice": "50000"}],
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert connector.cancel_calls == []  # already gone - no cancel attempted


# --- Fix 1 (deep review of Task 6): Case C's cancel must be gated on the -
# --- recorded new SL's presence being freshly, positively confirmed - ----
# --- never on old-SL presence alone -----------------------------------------

def test_recovery_case_c_new_sl_missing_does_not_cancel_blindly_resolves_uncertain(tmp_path):
    """CRITICAL reviewer-reproduced bug: the old SL is still present, but
    the row's OWN recorded new_sl_order_id is NOT among the freshly-read
    open orders (externally cancelled, or BingX auto-cancelled a duplicate
    STOP_MARKET - exactly the exchange behavior the spec refuses to
    assume). The previous guard checked old-SL presence only and would
    cancel the old SL anyway, leaving a real open position with ZERO stops
    recorded as a permanent SL_REPLACED success. The fix must NOT cancel
    here - it must resolve the new SL's actual state via the same
    deterministic-client-order-id lookup Case B uses, and here the lookup
    also fails to find it - so this blocks (old SL is NEVER cancelled)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        # Old SL still present; the recorded new SL (new-sl-1) is NOT here.
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"}],
        lookup_order=None,  # deterministic lookup also can't find it
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert connector.cancel_calls == []  # the old SL must NEVER be cancelled on this evidence
    assert connector.place_calls == []  # never blindly re-placed either


def test_recovery_case_c_two_unrelated_stop_orders_does_not_cancel_blindly(tmp_path):
    """Second reviewer reproduction of the same root cause, proving the fix
    is identity-based and not merely presence/count-based: TWO STOP_MARKET
    orders exist (old SL plus one unrelated order), neither of which is the
    recorded new SL. A naive "len(sl_orders) >= 2 means old+new" fix would
    still wrongly cancel here - the fix must check new_sl_order_id
    specifically, by id, not just count."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "unrelated-order-9", "stopPrice": "51000"},
        ],
        lookup_order=None,
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert connector.cancel_calls == []
    assert connector.place_calls == []


def test_recovery_case_c_new_sl_missing_from_open_orders_but_confirmed_active_via_lookup_completes(tmp_path):
    """When the new SL isn't in the (possibly stale) open-orders read but a
    direct deterministic-client-order-id lookup DOES positively confirm it
    ACTIVE, that authoritative confirmation is trusted and the sequence
    resumes normally (cancel old, finalize) - the fix must not become
    overly conservative once genuine positive confirmation exists."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(
        positions=[_ABOVE_THRESHOLD_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"}],
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert connector.cancel_calls == ["old-sl-1"]
    assert connector.place_calls == []  # never re-placed - already confirmed active


def test_recovery_case_c_position_closed_yields_position_closed_during_replacement(tmp_path):
    """Both orders were already recorded (new SL confirmed active before the
    crash); the position itself is now flat. Per the established policy,
    neither order is touched without a verified need - there is nothing
    left to protect."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(positions=[])  # flat

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "POSITION_CLOSED_DURING_REPLACEMENT"
    assert connector.cancel_calls == []


# --- Anomaly: position open, zero STOP_MARKET orders exist at all --------

def test_recovery_zero_protective_orders_with_position_open_yields_anomaly(tmp_path):
    """A state this code should never itself produce (add-before-remove
    guarantees >=1 protective order at every self-caused transition) - do
    not auto-heal, do not guess a price and place a fresh SL."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.update_live_profit_protection_old_sl(
        "pos-1", old_sl_order_id="old-sl-1", old_sl_price="49000", updated_at=_NOW,
    )
    repo.update_live_profit_protection_new_sl("pos-1", new_sl_order_id="new-sl-1", updated_at=_NOW)
    connector = _SpyConnector(positions=[_ABOVE_THRESHOLD_POSITION], open_orders=[])

    run_live_profit_protection_tick(repo, connector, _THRESHOLD, "r1", _NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ANOMALY_NO_PROTECTIVE_ORDER_FOUND"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# --- Production isolation (design spec "Integration point" / test plan 13) ---

def test_module_never_imports_forbidden_production_modules():
    """Design spec 'Integration point': this module is intentionally
    standalone and must never import from paper_trading.position_closing,
    paper_trading.profit_protection_experiment, or crypto_trading.backtest
    (or anything under it) - same discipline as this codebase's other
    Tier 1 production-file-isolation tests (see
    tests/crypto_trading/test_no_intelligence_coupling.py)."""
    import ast
    from pathlib import Path

    import crypto_trading.paper_trading.live_profit_protection as module_under_test

    module_path = Path(module_under_test.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = (
        "crypto_trading.paper_trading.position_closing",
        "crypto_trading.paper_trading.profit_protection_experiment",
        "crypto_trading.backtest",
    )
    offenders = [
        m for m in imported_modules
        if any(m == prefix or m.startswith(prefix + ".") for prefix in forbidden_prefixes)
    ]
    assert offenders == [], f"live_profit_protection.py imports forbidden modules: {offenders}"
