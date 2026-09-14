"""Task 5: LIVE stop-loss tightening for Guardian Authority.

This test list deliberately MIRRORS
tests/crypto_trading/paper_trading/test_live_profit_protection.py case for
case (the module under test is a reuse-and-adapt of
crypto_trading/paper_trading/live_profit_protection.py, whose sequence has
already been through two rounds of deep adversarial review), plus the two
groups of tests that are specific to this module:

  * "Tightening invariant" - independent, second-layer re-verification that
    `new_sl` is strictly greater than the CURRENT, freshly-read live stop
    loss, never trusting Task 3's decision engine or Task 7's caller.
  * "Racing Profit Protection" - the two mechanisms' separate claim tables
    never collide, and SL identification always ratchets from the observed
    current SL rather than from an assumption about which mechanism acted
    most recently.
"""

from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.connectors.bingx_live_trading import OrderRejectedError
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.guardian.authority_live import (
    apply_live_sl_tightening,
    recover_claimed_live_sl_tightenings,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

# The caller-supplied target. Strictly greater than _ONE_OLD_SL's "49000"
# stop price, so the tightening invariant holds in every test that does not
# specifically exercise its violation.
_NEW_SL = Decimal("49500")


def _open_active_live_position(
    repo,
    position_id="pos-1",
    entry_quantity="0.002",
    avg_entry=Decimal("50000"),
    instrument="BTC-USDT",
) -> Position:
    """Seeds a positions row plus a live_executions row already in phase
    ACTIVE - the exact same helper shape test_live_profit_protection.py
    uses, so the two suites' fixtures stay directly comparable."""
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
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
    """Hand-written spy, copied from test_live_profit_protection.py's own
    _SpyConnector (this codebase's established convention) with two
    additions needed by this module's tests: `raise_on_symbol` (per-symbol
    get_position failure, for the recovery-pass isolation test) and
    `open_orders_sequence` (a different answer per get_open_orders call).

    Deliberately has NO take-profit-related method and NO set_leverage
    method at all - their absence, and them never being called, IS the
    proof that neither TP nor leverage is ever touched by this module
    (a Global Constraint of the plan: never call set_leverage)."""

    def __init__(
        self,
        positions=None,
        position_sequence=None,
        open_orders=None,
        open_orders_sequence=None,
        open_orders_raises_from_call=None,
        place_sl_raises=None,
        lookup_order=None,
        lookup_sequence=None,
        lookup_raises=None,
        cancel_raises=None,
        raise_on_symbol=None,
    ):
        self.calls: list[tuple] = []
        self.place_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls = 0
        self._positions = positions if positions is not None else []
        self._position_sequence = list(position_sequence) if position_sequence is not None else None
        self._open_orders = open_orders if open_orders is not None else []
        self._open_orders_sequence = (
            list(open_orders_sequence) if open_orders_sequence is not None else None
        )
        self._open_orders_call_count = 0
        self._open_orders_raises_from_call = open_orders_raises_from_call
        self._place_sl_raises = place_sl_raises
        self._lookup_order = lookup_order
        self._lookup_sequence = list(lookup_sequence) if lookup_sequence is not None else None
        self._lookup_raises = lookup_raises
        self._cancel_raises = cancel_raises
        self._raise_on_symbol = raise_on_symbol

    def get_position(self, symbol):
        self.calls.append(("get_position", symbol))
        if self._raise_on_symbol is not None and symbol == self._raise_on_symbol:
            raise ConnectorUnavailableError(f"get_position failed for {symbol}")
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
        if self._open_orders_sequence is not None and self._open_orders_sequence:
            return self._open_orders_sequence.pop(0)
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
# ("0.002") so the quantity-vs-exchange-position cross-check (inherited
# verbatim from PP's deep-review fix 5) passes by default everywhere except
# in the test that specifically exercises the mismatch.
_LIVE_POSITION = {
    "symbol": "BTC-USDT", "avgPrice": "50000", "markPrice": "50600", "positionAmt": "0.002",
}
_ONE_OLD_SL = [{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"}]


def _apply(repo, connector, position_id="pos-1", new_sl=_NEW_SL, instrument="BTC-USDT"):
    apply_live_sl_tightening(
        repo, connector, position_id, instrument, new_sl, "r1", _NOW,
    )


def _row(repo, position_id="pos-1"):
    return repo.get_guardian_authority_live_sl_action(position_id)


# =========================================================================
# 1. Normal success
# =========================================================================

def test_normal_success_places_new_sl_then_cancels_old(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector)

    row = _row(repo)
    assert row is not None
    assert row["status"] == "SL_REPLACED"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["old_sl_price"] == "49000"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert row["new_sl_price"] == "49500"
    assert connector.place_calls == [{
        "symbol": "BTC-USDT", "quantity": "0.002", "stop_price": "49500",
        "client_order_id": row["new_sl_client_order_id"],
    }]
    assert connector.cancel_calls == ["old-sl-1"]


def test_normal_success_orders_add_before_remove(tmp_path):
    """The single most important ordering property: the new SL is placed
    AND positively verified before the old SL is ever cancelled."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector)

    names = [c[0] for c in connector.calls]
    assert names.index("place_stop_loss_order") < names.index("cancel_order")
    assert names.index("get_order_by_client_order_id") < names.index("cancel_order")


# =========================================================================
# 2. New SL rejected by the exchange
# =========================================================================

def test_new_sl_rejected_aborts_and_never_cancels_old(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        place_sl_raises=OrderRejectedError("Stop price must be below Last Price"),
    )

    _apply(repo, connector)

    row = _row(repo)
    assert row["status"] == "ABORTED_NEW_SL_REJECTED"
    assert row["old_sl_order_id"] == "old-sl-1"  # recorded before the rejected placement
    assert row["new_sl_order_id"] is None
    assert connector.cancel_calls == []


# =========================================================================
# 3. Old SL cancel fails after the new SL is verified
# =========================================================================

def test_old_sl_cancel_fails_yields_replacement_partial(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "PENDING"},
        cancel_raises=ConnectorUnavailableError("network blip cancelling old SL"),
    )

    _apply(repo, connector)

    row = _row(repo)
    assert row["status"] == "REPLACEMENT_PARTIAL"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert len(connector.place_calls) == 1  # no third order ever placed
    assert connector.cancel_calls == ["old-sl-1"]  # exactly one cancel attempt


# =========================================================================
# 4. New SL status cannot be determined
# =========================================================================

def test_new_sl_lookup_raises_yields_uncertain_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_raises=ConnectorUnavailableError("timeout verifying new SL"),
    )

    _apply(repo, connector)

    row = _row(repo)
    assert row["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert row["new_sl_order_id"] is None
    assert connector.cancel_calls == []


def test_new_sl_unrecognized_status_yields_uncertain_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "SOME_FUTURE_STATUS"},
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert connector.cancel_calls == []


# =========================================================================
# 5/6. Ambiguous existing SL count
# =========================================================================

def test_zero_existing_sl_orders_aborts_ambiguous(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=[])

    _apply(repo, connector)

    assert _row(repo)["status"] == "ABORTED_AMBIGUOUS_SL"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_two_existing_sl_orders_aborts_ambiguous(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "old-sl-2", "stopPrice": "48900"},
        ],
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "ABORTED_AMBIGUOUS_SL"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_non_stop_market_orders_are_ignored_when_identifying_the_sl(tmp_path):
    """A take-profit (or any other) open order must never be mistaken for
    the protective SL, and must never be cancelled."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[
            {"type": "TAKE_PROFIT_MARKET", "orderId": "tp-1", "stopPrice": "52000"},
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
        ],
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.cancel_calls == ["old-sl-1"]  # the TP is never touched


# =========================================================================
# 7. Position already closed on the exchange at first check
# =========================================================================

def test_position_already_closed_creates_no_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[])  # get_position(instrument) -> None

    _apply(repo, connector)

    assert _row(repo) is None
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# =========================================================================
# 8. Position closes between placing and verifying the new SL
# =========================================================================

def test_position_closes_during_verification_yields_sl_replaced(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        # 1st: pre-claim liveness check, 2nd: re-check after a FILLED new SL
        position_sequence=[_LIVE_POSITION, None],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "FILLED"},
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.cancel_calls == []  # no order operation follows a closure


def test_new_sl_filled_but_position_still_open_yields_uncertain_status(tmp_path):
    """A FILLED stop implies the position went flat; if the exchange still
    reports the position open, that is an anomaly - never guess, never
    cancel the old SL."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],  # still open on every get_position call
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "FILLED"},
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert connector.cancel_calls == []


# =========================================================================
# 9. Orphan-SL race: re-check the position immediately before cancelling
# =========================================================================

def test_position_closes_after_new_sl_verified_skips_cancel(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        # 1st: pre-claim liveness check, 2nd: pre-cancel re-check (flat)
        position_sequence=[_LIVE_POSITION, None],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector)

    row = _row(repo)
    assert row["status"] == "POSITION_CLOSED_DURING_REPLACEMENT"
    assert row["new_sl_order_id"] == "new-sl-1"  # recorded before the re-check
    assert connector.cancel_calls == []  # cancel skipped entirely


# =========================================================================
# 10. SL_REPLACED is written before the informational final-state read
# =========================================================================

def test_final_state_read_failure_does_not_undo_sl_replaced(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
        open_orders_raises_from_call=2,  # 1st (SL identification) OK, 2nd (final read) raises
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.cancel_calls == ["old-sl-1"]  # the irreversible cancel did happen


# =========================================================================
# 11. Quantity guards (inherited verbatim from PP's deep-review fixes 1/5)
# =========================================================================

def test_zero_entry_quantity_aborts_before_any_placement(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo, entry_quantity="0")
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _apply(repo, connector)

    row = _row(repo)
    assert row["status"] == "ABORTED_INVALID_ENTRY_QUANTITY"
    assert row["old_sl_order_id"] == "old-sl-1"  # recorded, never cancelled
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_entry_quantity_mismatch_with_exchange_position_aborts(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo, entry_quantity="0.002")
    connector = _SpyConnector(
        positions=[{
            "symbol": "BTC-USDT", "avgPrice": "50000", "markPrice": "50600",
            "positionAmt": "0.01",  # 5x the locally-recorded entry_quantity
        }],
        open_orders=_ONE_OLD_SL,
    )

    _apply(repo, connector)

    row = _row(repo)
    assert row["status"] == "ABORTED_QUANTITY_MISMATCH"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# =========================================================================
# 12. Idempotency / per-position claim isolation
# =========================================================================

def test_position_with_existing_terminal_row_is_never_touched_again(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    repo.claim_guardian_authority_live_sl_action("pos-1", "49500", "existing-cid-ga", _NOW)
    repo.set_guardian_authority_live_sl_action_status("pos-1", "SL_REPLACED", _NOW)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _apply(repo, connector)

    assert connector.calls == []  # the connector is never touched at all
    assert _row(repo)["status"] == "SL_REPLACED"  # left exactly as-is


def test_position_with_an_in_flight_claimed_row_is_never_touched_by_apply(tmp_path):
    """A row left CLAIMED by an interrupted attempt belongs to the
    restart-recovery pass alone - apply must never start a second,
    concurrent attempt against the same position."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _apply(repo, connector)

    assert connector.calls == []
    assert _row(repo)["status"] == "CLAIMED"


def test_a_refused_tightening_permanently_blocks_further_attempts(tmp_path):
    """Documents (and pins) the consequence of the mandated position_id
    primary key: this table holds at most ONE tightening attempt per
    position for its whole life, exactly like live_profit_protection's
    own once-per-position claim. Once an attempt has reached any terminal
    status - a refusal included - a later call with a perfectly valid
    target is refused without touching the exchange. Fail-closed: the
    position keeps whatever protection it already has."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector, new_sl=Decimal("48000"))  # looser than live - refused
    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"

    _apply(repo, connector, new_sl=Decimal("49500"))  # a genuinely valid tighten

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"  # unchanged
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_second_apply_call_on_the_same_position_never_places_a_second_order(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector)
    _apply(repo, connector, new_sl=Decimal("49800"))

    assert len(connector.place_calls) == 1  # the claim is the idempotency gate
    assert connector.cancel_calls == ["old-sl-1"]
    assert _row(repo)["new_sl_price"] == "49500"  # the first claim's target stands


def test_claims_are_isolated_per_position(tmp_path):
    """Two live positions, each claimed and replaced independently in its
    own row - one position's row never affects the other's."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo, position_id="pos-a")
    _open_active_live_position(repo, position_id="pos-b")
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector, position_id="pos-a")
    _apply(repo, connector, position_id="pos-b", new_sl=Decimal("49600"))

    assert _row(repo, "pos-a")["status"] == "SL_REPLACED"
    assert _row(repo, "pos-a")["new_sl_price"] == "49500"
    assert _row(repo, "pos-b")["status"] == "SL_REPLACED"
    assert _row(repo, "pos-b")["new_sl_price"] == "49600"
    assert _row(repo, "pos-a")["new_sl_client_order_id"] != _row(repo, "pos-b")[
        "new_sl_client_order_id"
    ]


# =========================================================================
# 13. NEW - the tightening invariant, independently re-verified here
#     (belt-and-suspenders layer 2 of 2; Task 3's decision engine is
#     layer 1 and this module trusts NEITHER it nor Task 7's caller)
# =========================================================================

def test_new_sl_below_current_live_sl_refuses_and_places_nothing(tmp_path):
    """A stale caller, a race, or an upstream logic error proposes a stop
    LOOSER than the one really on the exchange right now. Refuse: no order
    placed, no order cancelled."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _apply(repo, connector, new_sl=Decimal("48500"))  # < the live 49000

    row = _row(repo)
    assert row["status"] == "ABORTED_INVALID_TIGHTENING"
    assert row["old_sl_price"] == "49000"  # what was actually observed, for the audit trail
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_new_sl_equal_to_current_live_sl_refuses(tmp_path):
    """Strictly greater, never merely greater-or-equal."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _apply(repo, connector, new_sl=Decimal("49000"))

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_new_sl_barely_above_current_live_sl_is_accepted(tmp_path):
    """The refusal must be exactly the strict-inequality boundary, not an
    over-conservative band that would block genuine tightenings."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector, new_sl=Decimal("49000.01"))

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.place_calls[0]["stop_price"] == "49000.01"


def test_unparseable_current_live_sl_refuses_rather_than_guessing(tmp_path):
    """If the CURRENT live stop price cannot be read as a positive number,
    the invariant cannot be verified at all - fail closed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": None}],
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_zero_current_live_sl_refuses_rather_than_guessing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "0"}],
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"
    assert connector.place_calls == []


def test_non_positive_new_sl_parameter_never_reaches_the_exchange(tmp_path):
    """A caller bug that supplies a non-positive/unusable target must be
    rejected before ANY exchange call and before any claim - it never
    poisons the position's one claim row."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _apply(repo, connector, new_sl=Decimal("0"))

    assert _row(repo) is None
    assert connector.calls == []


def test_negative_new_sl_parameter_never_reaches_the_exchange(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _apply(repo, connector, new_sl=Decimal("-1"))

    assert _row(repo) is None
    assert connector.calls == []


# =========================================================================
# 14. NEW - racing LIVE Profit Protection (spec test item 5)
# =========================================================================

def _seed_pp_claim(repo, position_id="pos-1", breakeven="50000", status=None):
    """Seeds a live_profit_protection row on the SAME position_id, in PP's
    OWN claim table - an in-flight (CLAIMED) one by default, or a terminal
    one when `status` is given."""
    repo.claim_live_profit_protection(
        position_id, "0.01", "50600", breakeven, "existing-cid-pp", _NOW,
    )
    if status is not None:
        repo.set_live_profit_protection_status(position_id, status, _NOW)


def test_pp_claim_in_flight_does_not_block_or_collide_with_this_claim(tmp_path):
    """(a) The two mechanisms claim different rows in DIFFERENT tables:
    an in-flight PP claim on the same position_id neither blocks Guardian
    Authority's own claim nor is modified by it."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _seed_pp_claim(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector)

    ga_row = _row(repo)
    pp_row = repo.get_live_profit_protection("pos-1")
    assert ga_row["status"] == "SL_REPLACED"
    assert pp_row["status"] == "CLAIMED"  # PP's own row is untouched by this module
    assert ga_row["new_sl_client_order_id"] != pp_row["new_sl_client_order_id"]


def test_completed_pp_claim_does_not_block_this_claim(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _seed_pp_claim(repo, status="SL_REPLACED")
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert repo.get_live_profit_protection("pos-1")["status"] == "SL_REPLACED"


def test_this_claim_does_not_block_pp_claiming_the_same_position(tmp_path):
    """The reverse direction: a completed Guardian Authority action never
    consumes PP's own claim - the two tables are fully independent."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )
    _apply(repo, connector)

    claimed = repo.claim_live_profit_protection(
        "pos-1", "0.01", "50600", "50000", "cid-pp", _NOW,
    )

    assert claimed is True
    assert _row(repo)["status"] == "SL_REPLACED"  # unaffected by PP's claim


def test_tightening_ratchets_from_the_sl_pp_actually_placed_not_a_stale_one(tmp_path):
    """(b) Profit Protection already replaced the original 49000 stop with
    its own break-even 50000 stop. Guardian Authority must identify THAT
    order as the current SL - by reading the exchange, not by assuming
    which mechanism acted last - and ratchet from 50000."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _seed_pp_claim(repo, status="SL_REPLACED")
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        # The ONLY STOP_MARKET on the exchange is the one PP placed.
        open_orders=[{"type": "STOP_MARKET", "orderId": "pp-sl-1", "stopPrice": "50000"}],
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _apply(repo, connector, new_sl=Decimal("50400"))

    row = _row(repo)
    assert row["status"] == "SL_REPLACED"
    assert row["old_sl_order_id"] == "pp-sl-1"  # PP's order, found by observation
    assert row["old_sl_price"] == "50000"
    assert connector.place_calls[0]["stop_price"] == "50400"
    assert connector.cancel_calls == ["pp-sl-1"]


def test_target_that_only_looks_tighter_against_the_stale_original_sl_is_refused(tmp_path):
    """The decisive anti-regression test: 49500 WOULD have been a valid
    tightening against the position's original 49000 stop, but Profit
    Protection has since moved the real stop to 50000. Ratcheting from the
    stale value would LOOSEN the real protection - it must be refused."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _seed_pp_claim(repo, status="SL_REPLACED")
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "pp-sl-1", "stopPrice": "50000"}],
    )

    _apply(repo, connector, new_sl=Decimal("49500"))

    row = _row(repo)
    assert row["status"] == "ABORTED_INVALID_TIGHTENING"
    assert row["old_sl_price"] == "50000"
    assert connector.place_calls == []
    assert connector.cancel_calls == []  # PP's stop is left exactly where it is


def test_pp_mid_replacement_two_stop_orders_is_ambiguous_and_aborts(tmp_path):
    """PP is caught mid-replacement (its add-before-remove window leaves
    both its new and the old stop live for a moment). Two STOP_MARKET
    orders means the current SL cannot be unambiguously identified -
    abort rather than guess which one is authoritative."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _seed_pp_claim(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "pp-sl-1", "stopPrice": "50000"},
        ],
    )

    _apply(repo, connector)

    assert _row(repo)["status"] == "ABORTED_AMBIGUOUS_SL"
    assert connector.place_calls == []
    assert connector.cancel_calls == []
    assert repo.get_live_profit_protection("pos-1")["status"] == "CLAIMED"


# =========================================================================
# 15. Restart / crash recovery - Case A (no old SL identified yet)
# =========================================================================

def _claim_row(repo, position_id="pos-1", new_sl_price="49500", cid="existing-cid-ga"):
    repo.claim_guardian_authority_live_sl_action(position_id, new_sl_price, cid, _NOW)


def _recover(repo, connector):
    recover_claimed_live_sl_tightenings(repo, connector, "r1", _NOW)


def test_recovery_case_a_position_still_open_retries_from_scratch(tmp_path):
    """Nothing was ever placed for this attempt. Recovery reuses the row's
    stored target price and client order id - never a newly-minted id."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _recover(repo, connector)

    row = _row(repo)
    assert row["status"] == "SL_REPLACED"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.place_calls == [{
        "symbol": "BTC-USDT", "quantity": "0.002", "stop_price": "49500",
        "client_order_id": "existing-cid-ga",  # the SAME id stored at claim time
    }]
    assert connector.cancel_calls == ["old-sl-1"]


def test_recovery_case_a_position_closed_yields_position_closed_before_tightening(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    connector = _SpyConnector(positions=[])

    _recover(repo, connector)

    assert _row(repo)["status"] == "POSITION_CLOSED_BEFORE_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_a_re_verifies_the_invariant_against_the_sl_live_now(tmp_path):
    """During the downtime, Profit Protection moved the real stop above
    this row's stored target. Resuming must NOT place the now-loosening
    order just because the target was valid when it was claimed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "pp-sl-1", "stopPrice": "50000"}],
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_a_unparseable_stored_target_resolves_instead_of_sticking(tmp_path):
    """Defense in depth: a CLAIMED row whose stored target is not a
    parseable, strictly-positive price must resolve to a terminal refusal,
    not raise out of the pass and leave the row stuck CLAIMED forever."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo, new_sl_price="not-a-price")
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _recover(repo, connector)

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_b_unparseable_stored_target_resolves_instead_of_sticking(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo, new_sl_price="0")
    connector = _SpyConnector(
        positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL, lookup_order=None,
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_pass_ignores_rows_that_already_reached_a_terminal_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row(repo)
    repo.set_guardian_authority_live_sl_action_status("pos-1", "SL_REPLACED", _NOW)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_ONE_OLD_SL)

    _recover(repo, connector)

    assert connector.calls == []
    assert _row(repo)["status"] == "SL_REPLACED"


def test_recovery_pass_one_bad_row_never_blocks_the_rest_of_the_batch(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo, position_id="pos-bad", instrument="BAD-USDT")
    _open_active_live_position(repo, position_id="pos-good")
    _claim_row(repo, position_id="pos-bad")
    _claim_row(repo, position_id="pos-good", cid="cid-good")
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
        raise_on_symbol="BAD-USDT",
    )

    _recover(repo, connector)

    assert _row(repo, "pos-bad")["status"] == "CLAIMED"  # left for the next pass
    assert _row(repo, "pos-good")["status"] == "SL_REPLACED"


# =========================================================================
# 16. Restart / crash recovery - Case B (old SL known, new SL unconfirmed)
# =========================================================================

def _claim_row_with_old_sl(repo, position_id="pos-1", old_sl_order_id="old-sl-1",
                           old_sl_price="49000", new_sl_price="49500", cid="existing-cid-ga"):
    _claim_row(repo, position_id, new_sl_price, cid)
    repo.update_guardian_authority_live_sl_action_old_sl(
        position_id, old_sl_order_id=old_sl_order_id, old_sl_price=old_sl_price, updated_at=_NOW,
    )


def test_recovery_case_b_new_sl_not_found_retries_placement_and_completes(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_sequence=[None, {"orderId": "new-sl-1", "status": "NEW"}],
    )

    _recover(repo, connector)

    row = _row(repo)
    assert row["status"] == "SL_REPLACED"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.place_calls == [{
        "symbol": "BTC-USDT", "quantity": "0.002", "stop_price": "49500",
        "client_order_id": "existing-cid-ga",
    }]
    assert connector.cancel_calls == ["old-sl-1"]


def test_recovery_case_b_new_sl_found_active_resumes_at_verified_tail(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "PENDING"},
    )

    _recover(repo, connector)

    row = _row(repo)
    assert row["status"] == "SL_REPLACED"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.place_calls == []  # never re-placed - already confirmed active
    assert connector.cancel_calls == ["old-sl-1"]


def test_recovery_case_b_new_sl_found_filled_position_flat_yields_sl_replaced(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(
        position_sequence=[_LIVE_POSITION, None],
        open_orders=_ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "FILLED"},
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_b_position_closed_yields_position_closed_before_tightening(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(positions=[])

    _recover(repo, connector)

    assert _row(repo)["status"] == "POSITION_CLOSED_BEFORE_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_b_zero_open_orders_yields_anomaly_not_a_blind_placement(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=[])

    _recover(repo, connector)

    assert _row(repo)["status"] == "ANOMALY_NO_PROTECTIVE_ORDER_FOUND"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_b_open_orders_lookup_error_leaves_row_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION], open_orders_raises_from_call=1,
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "CLAIMED"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_b_new_sl_lookup_error_leaves_row_claimed_never_retries_placement(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_ONE_OLD_SL,
        lookup_raises=ConnectorUnavailableError("ConnectTimeout looking up new SL"),
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "CLAIMED"  # left for the next pass
    assert connector.place_calls == []  # a lookup error must NEVER trigger a placement
    assert connector.cancel_calls == []


def test_recovery_case_b_recorded_old_sl_no_longer_the_live_one_aborts_ambiguous(tmp_path):
    """A STOP_MARKET exists, but it is NOT the one this row recorded -
    something else (e.g. Profit Protection) replaced it during the
    downtime. The current SL cannot be identified with the recorded one,
    so no placement may happen on this evidence."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "pp-sl-1", "stopPrice": "50000"}],
        lookup_order=None,
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "ABORTED_AMBIGUOUS_SL"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_recovery_case_b_re_verifies_the_invariant_against_the_sl_live_now(tmp_path):
    """The recorded old SL is still the live one, but its stop price has
    been amended above this row's stored target. Re-verify against the
    freshly-read price, never against the price recorded at claim time."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_old_sl(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49900"}],
        lookup_order=None,
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "ABORTED_INVALID_TIGHTENING"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# =========================================================================
# 17. Restart / crash recovery - Case C (new SL already confirmed ACTIVE)
# =========================================================================

def _claim_row_with_both(repo, position_id="pos-1"):
    _claim_row_with_old_sl(repo, position_id)
    repo.update_guardian_authority_live_sl_action_new_sl(
        position_id, new_sl_order_id="new-sl-1", updated_at=_NOW,
    )


_BOTH_SL_ORDERS = [
    {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
    {"type": "STOP_MARKET", "orderId": "new-sl-1", "stopPrice": "49500"},
]


def test_recovery_case_c_both_orders_exist_cancel_succeeds(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=_BOTH_SL_ORDERS)

    _recover(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.place_calls == []  # no third order ever placed
    assert connector.cancel_calls == ["old-sl-1"]


def test_recovery_case_c_both_orders_exist_cancel_fails_again(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=_BOTH_SL_ORDERS,
        cancel_raises=ConnectorUnavailableError("still unreachable cancelling old SL"),
    )

    _recover(repo, connector)

    row = _row(repo)
    assert row["status"] == "REPLACEMENT_PARTIAL"
    assert row["old_sl_order_id"] == "old-sl-1"
    assert row["new_sl_order_id"] == "new-sl-1"
    assert connector.cancel_calls == ["old-sl-1"]  # exactly one attempt


def test_recovery_case_c_position_closes_between_identification_and_cancel_skips_cancel(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(
        # 1st: Case C's own early liveness check, 2nd: the shared pre-cancel re-check
        position_sequence=[_LIVE_POSITION, None],
        open_orders=_BOTH_SL_ORDERS,
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "POSITION_CLOSED_DURING_REPLACEMENT"
    assert connector.cancel_calls == []


def test_recovery_case_c_old_sl_already_gone_yields_sl_replaced_directly(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "new-sl-1", "stopPrice": "49500"}],
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.cancel_calls == []  # already gone - no cancel attempted


def test_recovery_case_c_new_sl_missing_does_not_cancel_blindly_resolves_uncertain(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"}],
        lookup_order=None,  # deterministic lookup also can't find it
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert connector.cancel_calls == []  # the old SL must NEVER be cancelled here
    assert connector.place_calls == []


def test_recovery_case_c_two_unrelated_stop_orders_does_not_cancel_blindly(tmp_path):
    """Identity-based, not count-based: two STOP_MARKET orders exist,
    neither of which is the recorded new SL."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[
            {"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"},
            {"type": "STOP_MARKET", "orderId": "unrelated-order-9", "stopPrice": "51000"},
        ],
        lookup_order=None,
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "UNCERTAIN_NEW_SL_STATUS"
    assert connector.cancel_calls == []
    assert connector.place_calls == []


def test_recovery_case_c_new_sl_missing_but_confirmed_active_via_lookup_completes(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(
        positions=[_LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "49000"}],
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    _recover(repo, connector)

    assert _row(repo)["status"] == "SL_REPLACED"
    assert connector.cancel_calls == ["old-sl-1"]
    assert connector.place_calls == []  # never re-placed


def test_recovery_case_c_position_closed_yields_position_closed_during_replacement(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(positions=[])

    _recover(repo, connector)

    assert _row(repo)["status"] == "POSITION_CLOSED_DURING_REPLACEMENT"
    assert connector.cancel_calls == []


def test_recovery_case_c_open_orders_lookup_error_leaves_row_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders_raises_from_call=1)

    _recover(repo, connector)

    assert _row(repo)["status"] == "CLAIMED"
    assert connector.cancel_calls == []


def test_recovery_zero_protective_orders_with_position_open_yields_anomaly(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_active_live_position(repo)
    _claim_row_with_both(repo)
    connector = _SpyConnector(positions=[_LIVE_POSITION], open_orders=[])

    _recover(repo, connector)

    assert _row(repo)["status"] == "ANOMALY_NO_PROTECTIVE_ORDER_FOUND"
    assert connector.place_calls == []
    assert connector.cancel_calls == []


# =========================================================================
# 18. Production isolation / Global Constraints
# =========================================================================

def _module_source():
    from pathlib import Path

    import crypto_trading.guardian.authority_live as module_under_test

    return Path(module_under_test.__file__).read_text(encoding="utf-8")


def test_module_never_imports_forbidden_production_modules():
    """Same discipline as live_profit_protection.py's own isolation test,
    plus the plan's Global Constraint that position_sizing.py is never
    imported anywhere in Guardian Authority."""
    import ast

    tree = ast.parse(_module_source())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = (
        "crypto_trading.paper_trading.position_closing",
        "crypto_trading.paper_trading.position_sizing",
        "crypto_trading.paper_trading.profit_protection_experiment",
        "crypto_trading.paper_trading.live_profit_protection",
        "crypto_trading.backtest",
    )
    offenders = [
        m for m in imported_modules
        if any(m == prefix or m.startswith(prefix + ".") for prefix in forbidden_prefixes)
    ]
    assert offenders == [], f"authority_live.py imports forbidden modules: {offenders}"


def test_module_never_references_set_leverage():
    """Global Constraint (verbatim from the plan): never call
    BingXLiveTradingConnector.set_leverage. The name does not appear in
    this module's source at all - not in code, not in a comment - so the
    check cannot be satisfied by a call hiding behind an alias."""
    assert "set_leverage" not in _module_source()


def test_module_never_touches_the_live_profit_protection_table_or_the_paper_path():
    """The two mechanisms' claim tables must never be shared: this module
    may never read or write live_profit_protection's row for a position
    (referring to that module by name in a comment is fine - calling any
    of its repository methods is not). Task 4's PAPER-side
    tighten_position_stop_loss is a completely separate code path and is
    likewise never called from here."""
    source = _module_source()
    forbidden_calls = (
        "claim_live_profit_protection",
        "get_live_profit_protection",
        "find_claimed_live_profit_protection",
        "update_live_profit_protection_old_sl",
        "update_live_profit_protection_new_sl",
        "set_live_profit_protection_status",
        "tighten_position_stop_loss",
    )
    offenders = [name for name in forbidden_calls if name in source]
    assert offenders == [], (
        f"authority_live.py references forbidden repository methods: {offenders}"
    )
