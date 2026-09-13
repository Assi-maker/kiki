from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.bingx_live_trading import OrderRejectedError
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.live_execution import (
    close_guardian_exit_positions,
    close_time_limit_positions,
    has_sufficient_live_capacity,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
    resolve_pending_entries,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _open_position(
    repo, position_id="pos-1", opened_at=_NOW, entry=Decimal("50000"), confirmed_at=None,
) -> Position:
    """confirmed_at defaults to opened_at - i.e. the signal is fresh at
    `_NOW` unless a test explicitly backdates it - so every pre-existing
    test in this file (which assumes immediate eligibility) keeps passing
    unchanged under the new TTL check (spec §17)."""
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=entry,
        simulated_fill_entry=entry, stop_loss=Decimal("49000"), target=Decimal("52000"),
        size=Decimal("1000"), fill_model_version="v1", opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    _confirm_signal(repo, position_id, confirmed_at if confirmed_at is not None else opened_at)
    return position


def _confirm_signal(repo, candidate_id, confirmed_at) -> None:
    """Spec §17.3's authoritative signal timestamp: a CANDIDATE_TRANSITIONED
    event with payload.to == 'CONFIRMED', sourced from the events table."""
    event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{candidate_id}:CONFIRMED:{confirmed_at.isoformat()}",
        event_type="CANDIDATE_TRANSITIONED", aggregate_type="candidate",
        aggregate_id=candidate_id, occurred_at=confirmed_at, run_id="seed",
        schema_version=1, payload={"from": "UNDER_AI_ANALYSIS", "to": "CONFIRMED"},
    )
    repo.transition_candidate_with_event(candidate_id, "CONFIRMED", confirmed_at, event)


class _SpyConnector:
    """order_status/executed_qty control what a lookup returns when it
    succeeds; place_raises/lookup_raises independently force each call to
    raise instead (simulating a timeout/network error at that specific
    point); lookup_returns_none simulates a genuine "order not found"
    response. This independence is what lets tests exercise a placement
    error followed by a DIFFERENT, distinct lookup outcome - the exact
    ambiguous scenario the Risk D fix targets."""

    def __init__(
        self, balance="123.45", all_positions=None, order_status="FILLED",
        executed_qty="0.002", place_raises=None, lookup_raises=None, lookup_returns_none=False,
    ):
        self.calls = []
        self.lookup_calls = 0
        self._balance = balance
        self._all_positions = all_positions if all_positions is not None else []
        self._order_status = order_status
        self._executed_qty = executed_qty
        self._place_raises = place_raises
        self._lookup_raises = lookup_raises
        self._lookup_returns_none = lookup_returns_none
        self.leverage_calls = []

    def set_leverage(self, symbol, leverage=10, side="LONG"):
        self.leverage_calls.append((symbol, leverage))
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.calls.append(kwargs)
        if self._place_raises is not None:
            raise self._place_raises
        return {"orderId": "ex-1", "avgPrice": kwargs.get("stop_loss_price", "0")}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        self.lookup_calls += 1
        if self._lookup_raises is not None:
            raise self._lookup_raises
        if self._lookup_returns_none:
            return None
        return {
            "orderId": "ex-1", "status": self._order_status,
            "executedQty": self._executed_qty, "avgPrice": "50030",
        }

    def get_all_positions(self):
        return self._all_positions

    def get_position(self, symbol):
        for p in self._all_positions:
            if p.get("symbol") == symbol:
                return p
        return None

    def get_balance(self):
        return {"availableMargin": self._balance}

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "0"}


class _SpyMarketDataConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "50000"}


def test_has_sufficient_live_capacity_true_when_room_and_margin(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _SpyConnector(balance="50.00", all_positions=[])

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is True


def test_has_sufficient_live_capacity_false_when_margin_short(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _SpyConnector(balance="5.00", all_positions=[])

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is False


def test_has_sufficient_live_capacity_false_when_reconciled_count_at_cap(tmp_path):
    """Local DB says only 3 active, but the exchange's real position list
    (via reconcile_active_executions) confirms all 4 are still genuinely
    open - the reconciled count, not the raw local phase, is authoritative."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(4):
        pid = f"pos-{i}"
        _open_position(repo, pid)
        repo.claim_live_execution(pid, _NOW, "10", "100", "10")
        repo.update_live_execution_submitted(
            pid, f"cid-{i}", f"ex-{i}", "0.002", "50000", None, None, _NOW
        )
    connector = _SpyConnector(
        balance="100.00",
        all_positions=[{"symbol": "BTC-USDT", "positionAmt": "0.002"}] * 4,
    )

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is False


def test_reconcile_active_executions_closes_out_positions_gone_flat_on_exchange(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector(all_positions=[])  # exchange shows nothing open - closed

    count = reconcile_active_executions(repo, connector, _SpyMarketDataConnector(), "r1", _NOW)

    assert count == 0
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"


def test_process_pending_positions_claims_and_submits_when_capacity_available(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert connector.leverage_calls == [("BTC-USDT", 10)]
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"


def _with_ttl(settings, ttl_seconds):
    return settings.model_copy(
        update={"live_execution": settings.live_execution.model_copy(
            update={"signal_ttl_seconds": ttl_seconds}
        )}
    )


def test_process_pending_positions_claims_a_fresh_signal_within_ttl(tmp_path):
    """Spec §17.9 case 1: a signal well within TTL is claimed and submitted
    exactly as before the TTL check existed - no regression to §7/§8."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, confirmed_at=_NOW - timedelta(minutes=5))
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = _with_ttl(get_settings(), ttl_seconds=1800)

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"


def test_process_pending_positions_treats_signal_exactly_at_ttl_boundary_as_eligible(tmp_path):
    """Spec §17.9 case 2: age == TTL exactly is defined as still eligible
    (inclusive boundary) - not stale."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, confirmed_at=_NOW - timedelta(seconds=1800))
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = _with_ttl(get_settings(), ttl_seconds=1800)

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"


def test_process_pending_positions_never_claims_a_signal_older_than_ttl(tmp_path):
    """Spec §17.9 case 3: one second past TTL is stale - never claimed,
    never submitted to BingX, zero AI involvement (the connector here has
    no AI-call surface at all, so len(connector.calls) == 0 structurally
    proves no order and, by construction of this module, no AI credit was
    ever at risk)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, confirmed_at=_NOW - timedelta(seconds=1801))
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = _with_ttl(get_settings(), ttl_seconds=1800)

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert repo.get_live_execution("pos-1") is None  # never claimed at all
    assert connector.calls == []  # never even attempted an order


def test_process_pending_positions_old_stale_backlog_never_blocks_a_fresh_signal(tmp_path):
    """2026-09-13 pipeline-queue bugfix, integration-level proof: a backlog
    of never-claimed, permanently-stale old positions (larger than the
    default page size) must never crowd a genuinely fresh, still-within-TTL
    signal out of being seen and claimed. Reproduces the real incident
    exactly: N old positions, all already well past TTL, plus 1 fresh one -
    the fresh one must still be claimed and go ACTIVE in a single tick."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(12):  # more than the default limit=10, matching the real backlog shape
        _open_position(
            repo, position_id=f"pos-old-{i}",
            opened_at=_NOW - timedelta(hours=6),
            confirmed_at=_NOW - timedelta(hours=6),  # far past any real TTL
        )
    _open_position(
        repo, position_id="pos-fresh",
        opened_at=_NOW, confirmed_at=_NOW - timedelta(minutes=5),  # well within TTL
    )
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = _with_ttl(get_settings(), ttl_seconds=1800)

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    fresh_row = repo.get_live_execution("pos-fresh")
    assert fresh_row is not None and fresh_row["phase"] == "ACTIVE"
    for i in range(12):
        assert repo.get_live_execution(f"pos-old-{i}") is None  # never claimed, TTL still enforced
        old_position = repo.get_position(f"pos-old-{i}")
        assert old_position.status == "OPEN_POSITION"  # history untouched, not deleted/fabricated-closed


def test_process_pending_positions_never_claims_a_very_old_signal_across_repeated_ticks(tmp_path):
    """Spec §17.9 case 4: a multi-day-old signal is never claimed, on this
    tick or any later one - the exclusion is a pure function of (now,
    confirmed_at, ttl), so it can never "eventually" succeed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, confirmed_at=_NOW - timedelta(days=2))
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = _with_ttl(get_settings(), ttl_seconds=1800)

    for tick_offset in (0, 60, 3600, 7200):
        process_pending_positions(
            repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
            {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW + timedelta(seconds=tick_offset),
        )

    assert repo.get_live_execution("pos-1") is None
    assert connector.calls == []


def test_process_pending_positions_treats_a_missing_confirmed_event_as_stale(tmp_path):
    """Fail-closed (spec §17.2/§17.3): if the authoritative CONFIRMED event
    cannot be found at all, the signal must never be treated as fresh."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    event = Event(
        event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)  # deliberately no CONFIRMED event
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = _with_ttl(get_settings(), ttl_seconds=1800)

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert repo.get_live_execution("pos-1") is None
    assert connector.calls == []


def test_process_pending_positions_restart_never_reclaims_an_already_stale_signal(tmp_path):
    """Spec §17.9 case 6 / §17.8 restart safety: a signal already stale
    before "LIVE was enabled" (modeled here as a fresh process_pending_
    positions call with no prior in-memory state - the only state that
    exists is durable DB state) must be excluded on the very first tick,
    exactly as it would be on any later one. There is no persisted
    "already considered" flag to lose across a restart, because the check
    is a pure function of (now, confirmed_at, ttl) - this test proves a
    brand-new call sees it as stale immediately, and a second, later call
    (simulating a further restart) still does."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, confirmed_at=_NOW - timedelta(hours=10))
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = _with_ttl(get_settings(), ttl_seconds=1800)

    # "first tick after restart"
    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )
    assert repo.get_live_execution("pos-1") is None

    # "a further restart" - freshly re-evaluated, still excluded
    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r2", _NOW + timedelta(hours=1),
    )
    assert repo.get_live_execution("pos-1") is None
    assert connector.calls == []


def test_process_pending_positions_skips_when_capacity_full(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-existing")
    repo.claim_live_execution("pos-existing", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-existing", "cid-0", "ex-0", "0.002", "50000", None, None, _NOW
    )
    _open_position(repo, "pos-new")
    connector = _SpyConnector(
        balance="100.00",
        all_positions=[{"symbol": "BTC-USDT", "positionAmt": "0.002"}],
    )
    settings = get_settings()
    # force max_concurrent_positions=1 for this test via a settings copy
    settings = settings.model_copy(
        update={"live_execution": settings.live_execution.model_copy(
            update={"max_concurrent_positions": 1}
        )}
    )

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert repo.get_live_execution("pos-new") is None  # never claimed
    assert connector.calls == []  # never even attempted an order


def test_process_pending_positions_skips_safely_below_exchange_minimum(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("1000")},  # exchange minimum notional far above 100 USDT
        settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "SKIPPED"
    assert row["last_error"] == "below_exchange_minimum"
    assert connector.calls == []


def test_process_pending_positions_marks_failed_on_confirmed_rejection(tmp_path):
    """A CONFIRMED terminal negative status (zero executed quantity) is the
    only basis for FAILED after an order was placed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(
        balance="100.00", all_positions=[], order_status="REJECTED", executed_qty="0",
    )
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "rejected" in row["last_error"].lower()
    assert len(connector.calls) == 1  # exactly one placement attempt, never a duplicate


def test_process_pending_positions_leaves_row_uncertain_on_unrecognized_status(tmp_path):
    """An order that's placed but shows a status that's neither FILLED nor
    a confirmed terminal negative (e.g. still "NEW"/open) must never be
    guessed either way - stays ENTRY_SUBMITTED, retried later."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[], order_status="NEW", executed_qty="0")
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ENTRY_SUBMITTED"
    assert row["entry_client_order_id"] is not None
    assert len(connector.calls) == 1


def test_process_pending_positions_leaves_row_uncertain_on_partial_fill(tmp_path):
    """A PARTIALLY_FILLED status must never trigger a duplicate entry -
    stays uncertain, exactly like an unrecognized status."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(
        balance="100.00", all_positions=[], order_status="PARTIALLY_FILLED", executed_qty="0.001",
    )
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ENTRY_SUBMITTED"
    assert len(connector.calls) == 1

    # A later resolution pass, still partially filled, must still not
    # resubmit - the duplicate-retry guard applies across ticks too.
    resolve_pending_entries(repo, connector, "r1", _NOW + timedelta(seconds=30))
    row_again = repo.get_live_execution("pos-1")
    assert row_again["phase"] == "ENTRY_SUBMITTED"
    assert len(connector.calls) == 1


def test_process_pending_positions_leaves_claimed_on_placement_timeout_with_unresolvable_lookup(tmp_path):
    """A timeout (transport-level error) during placement is the classic
    ambiguous case: the exchange may have received the order anyway. If the
    immediate follow-up lookup is ALSO unresolvable (another timeout), the
    row must stay CLAIMED - never FAILED, never ACTIVE, and no order is
    ever resubmitted for it in this same call."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(
        balance="100.00", all_positions=[],
        place_raises=httpx.TransportError("timed out"),
        lookup_raises=httpx.TransportError("timed out"),
    )
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLAIMED"
    assert len(connector.calls) == 1  # the one attempt - never retried within this call


def test_process_pending_positions_promotes_to_active_when_timeout_but_lookup_confirms_filled(tmp_path):
    """A timeout during placement does NOT mean the order failed - if the
    follow-up lookup by the deterministic clientOrderID discovers it was
    actually filled, the position is promoted to ACTIVE directly, and no
    second order is ever sent."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(
        balance="100.00", all_positions=[],
        place_raises=httpx.TransportError("timed out"),
        order_status="FILLED", executed_qty="0.002",
    )
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert row["entry_quantity"] == "0.002"
    assert len(connector.calls) == 1  # never a second placement attempt


def test_process_pending_positions_leaves_claimed_on_application_level_placement_error(tmp_path):
    """A genuinely ambiguous ConnectorUnavailableError during placement
    (e.g. a non-JSON response or a bare HTTP status error - real
    transport/format ambiguity, NOT a parsed exchange rejection, which is
    OrderRejectedError since the 2026-09-06 fix) is treated identically to
    a transport-level timeout: never conclude anything without a lookup,
    never resubmit."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(
        balance="100.00", all_positions=[],
        place_raises=ConnectorUnavailableError("boom"),
        lookup_returns_none=True,
    )
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLAIMED"
    assert len(connector.calls) == 1


def test_process_pending_positions_marks_failed_immediately_on_order_rejected_error(tmp_path):
    """OrderRejectedError (the exchange's own definitive, structured
    rejection of this exact submission, e.g. "TP Price must be greater than
    Last Price") must conclude FAILED immediately, with zero lookup calls -
    there is nothing to look up, since no order was ever created. This is
    the 2026-09-06 fix: previously this case was indistinguishable from a
    genuine timeout and left CLAIMED forever, permanently blocking a LIVE
    capacity slot."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(
        balance="100.00", all_positions=[],
        place_raises=OrderRejectedError(
            "BingX Live API error 101400: TP Price must be greater than Last Price "
            "(/openApi/swap/v2/trade/order)"
        ),
    )
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "rejected" in row["last_error"].lower()
    assert "TP Price must be greater than Last Price" in row["last_error"]
    assert len(connector.calls) == 1  # exactly one placement attempt, never resubmitted
    assert connector.lookup_calls == 0  # no lookup needed - nothing was ever created to find


def test_a_rejected_entry_frees_the_live_capacity_slot_for_the_next_candidate(tmp_path):
    """The concrete incident this fix targets: a rejected entry must not
    permanently consume one of the 4 hard-capped LIVE slots. Two pending
    positions, capacity for only one - the first is rejected, and the
    second must then be able to claim the freed slot in the same tick."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, position_id="pos-1")
    _open_position(repo, position_id="pos-2")
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = get_settings()
    settings = settings.model_copy(
        update={"live_execution": settings.live_execution.model_copy(
            update={"max_concurrent_positions": 1}
        )}
    )

    # pos-1's placement is rejected outright; pos-2 (still pending after)
    # must see the freed slot and go on to succeed.
    original_place = connector.place_entry_order_with_sl_tp
    calls = {"n": 0}

    def place_with_first_rejected(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OrderRejectedError(
                "BingX Live API error 101400: TP Price must be greater than Last Price"
            )
        return original_place(**kwargs)

    connector.place_entry_order_with_sl_tp = place_with_first_rejected

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert repo.get_live_execution("pos-1")["phase"] == "FAILED"
    assert repo.get_live_execution("pos-2")["phase"] == "ACTIVE"
    # the reconciled count used for gating never counts the rejected row -
    # confirm pos-2 (genuinely open on the exchange) is the only one counted
    connector._all_positions = [{"symbol": "BTC-USDT", "positionAmt": "1"}]
    assert reconcile_active_executions(
        repo, connector, _SpyMarketDataConnector(), "r1", _NOW
    ) == 1


def test_resolve_pending_entries_never_resubmits_across_repeated_uncertain_ticks(tmp_path):
    """Duplicate-retry guard: calling the resolution pass multiple times
    while the exchange status remains uncertain must never place a second
    order for the same position."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[], order_status="NEW", executed_qty="0")
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )
    assert repo.get_live_execution("pos-1")["phase"] == "ENTRY_SUBMITTED"
    assert len(connector.calls) == 1

    resolve_pending_entries(repo, connector, "r1", _NOW + timedelta(seconds=30))
    resolve_pending_entries(repo, connector, "r1", _NOW + timedelta(seconds=60))
    resolve_pending_entries(repo, connector, "r1", _NOW + timedelta(seconds=90))

    assert repo.get_live_execution("pos-1")["phase"] == "ENTRY_SUBMITTED"
    assert len(connector.calls) == 1  # still exactly one - three resolution passes, zero resubmissions
    assert connector.lookup_calls >= 4  # the original post-submit check plus three more


def test_close_guardian_exit_positions_mirrors_only_after_paper_already_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector()

    close_guardian_exit_positions(repo, connector, "r1", _NOW)
    assert repo.get_live_execution("pos-1")["phase"] == "ACTIVE"  # untouched, PAPER not closed yet

    repo.close_position_with_event(
        position_id="pos-1", theoretical_exit=Decimal("49500"),
        simulated_fill_exit=Decimal("49500"), exit_reason="guardian_exit",
        fees=Decimal("0"), funding=Decimal("0"), closed_at=_NOW,
        event=Event(event_id="POSITION_CLOSED:pos-1", event_type="POSITION_CLOSED",
                    aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
                    run_id="seed", schema_version=1, payload={}),
    )
    close_guardian_exit_positions(repo, connector, "r1", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "GUARDIAN_EXIT"


def test_close_time_limit_positions_uses_the_passed_in_hold_hours(tmp_path):
    """LIVE's own 6h limit, independent of whatever PAPER's is - proven by
    passing a value (2h) that would never trigger under PAPER's 24h."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, opened_at=_NOW - timedelta(hours=3))
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector()

    close_time_limit_positions(repo, connector, max_position_hold_hours=2, run_id="r1", now=_NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "TIME_LIMIT"


def test_recover_stale_claims_promotes_to_active_when_lookup_confirms_filled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW - timedelta(seconds=60), "10", "100", "10")
    connector = _SpyConnector(balance="100.00")

    recover_stale_claims(repo, connector, "r1", _NOW, stale_after_seconds=30)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert connector.calls == []  # never places an order - lookup only


def test_recover_stale_claims_never_resubmits_when_status_is_unresolvable(tmp_path):
    """The core Risk D regression: a stale CLAIMED row whose lookup can't
    be resolved (order not found / lookup error) must stay CLAIMED - the
    OLD behavior blindly resubmitted here, risking a genuine duplicate
    live position."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW - timedelta(seconds=60), "10", "100", "10")
    connector = _SpyConnector(balance="100.00", lookup_returns_none=True)

    recover_stale_claims(repo, connector, "r1", _NOW, stale_after_seconds=30)
    recover_stale_claims(repo, connector, "r1", _NOW + timedelta(seconds=30), stale_after_seconds=30)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLAIMED"
    assert connector.calls == []  # zero placement attempts across both recovery passes


def test_recover_stale_claims_marks_failed_only_on_confirmed_rejection(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW - timedelta(seconds=60), "10", "100", "10")
    connector = _SpyConnector(balance="100.00", order_status="CANCELED", executed_qty="0")

    recover_stale_claims(repo, connector, "r1", _NOW, stale_after_seconds=30)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert connector.calls == []
