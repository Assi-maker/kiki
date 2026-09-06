from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

import httpx

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_live_trading import (
    BingXLiveTradingConnector,
    LiveExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.monitoring import compute_hold_hours
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_GUARDED_ERRORS = (ConnectorUnavailableError, LiveExecutionGuardError)

# 2026-09-06 safety audit (Risk D fix): any of these during a placement
# attempt OR a status lookup means the true exchange outcome cannot be
# determined right now - httpx.TransportError included because
# BingXLiveTradingConnector's own tenacity retry re-raises it verbatim
# after exhausting retries (unlike ConnectorUnavailableError, which is
# raised only once an actual response was received). Every caller treats
# all three identically: never conclude FAILED or FILLED from an error,
# never resubmit - see _resolve_uncertain_entry().
_ORDER_STATE_UNKNOWN_ERRORS = (ConnectorUnavailableError, LiveExecutionGuardError, httpx.TransportError)

# A confirmed, terminal, zero-fill outcome - the ONLY basis on which an
# entry is ever marked FAILED after having been placed. Anything else
# (NEW, PARTIALLY_FILLED, an unrecognized/future status string, or no
# order found at all) is deliberately classified UNKNOWN, never guessed.
_TERMINAL_NEGATIVE_STATUSES = frozenset({"CANCELED", "REJECTED", "EXPIRED"})


def _client_order_id(position_id: str, suffix: str) -> str:
    return f"lv{position_id[:24]}{suffix}"[:32]


def _quantity_for_live(entry_price: Decimal, margin_usdt: Decimal, leverage: int, precision: int) -> Decimal:
    """Fixed sizing, independent of PAPER's dynamic position.size (spec §4/§8):
    quantity = (margin * leverage) / entry_price, rounded down."""
    notional = margin_usdt * leverage
    raw_quantity = notional / entry_price
    quantum = Decimal(1).scaleb(-precision)
    return raw_quantity.quantize(quantum, rounding=ROUND_DOWN)


def reconcile_active_executions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    run_id: str,
    now: datetime,
) -> int:
    """Closes out any locally-ACTIVE row the exchange has already gone flat
    on (same "exchange going flat is the proof" principle as Demo's own
    reconcile_active_executions), then returns the resulting reconciled
    active count - the ONLY authoritative source for capacity checks
    (user-mandated: never trust local DB phase alone, spec §7)."""
    active_count = 0
    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            active_count += 1  # CLAIMED/ENTRY_SUBMITTED: in-flight, still reserves a slot
            continue
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        if connector.get_position(position.instrument) is not None:
            active_count += 1  # still genuinely open on the exchange
            continue
        exit_price = Decimal(str(market_data_connector.get_ticker(position.instrument)["lastPrice"]))
        distance_to_stop = abs(exit_price - position.stop_loss)
        distance_to_target = abs(exit_price - position.target)
        exit_reason = "stop_loss" if distance_to_stop <= distance_to_target else "target"
        repo.close_live_execution(position.position_id, exit_reason, str(exit_price), now)
        log_event(
            run_id, event="live_position_closed", position_id=position.position_id,
            exit_reason=exit_reason,
        )
    return active_count


def has_sufficient_live_capacity(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    max_concurrent_positions: int,
    required_margin_usdt: Decimal,
    run_id: str,
    now: datetime,
) -> bool:
    """The single shared gate both discovery_loop.py (coarse, pre-AI-cost)
    and this module's own process_pending_positions (authoritative,
    immediately pre-order) call - two call sites, one implementation, per
    spec §7. Reconciliation runs first so the count is never based on
    stale local state alone."""
    try:
        active_count = reconcile_active_executions(repo, connector, market_data_connector, run_id, now)
        if active_count >= max_concurrent_positions:
            log_event(run_id, event="live_capacity_full", active_count=active_count)
            return False
        balance = connector.get_balance()
        available_margin = Decimal(str(balance.get("availableMargin", "0")))
        if available_margin < required_margin_usdt:
            log_event(
                run_id, event="live_margin_insufficient",
                available_margin=str(available_margin), required=str(required_margin_usdt),
            )
            return False
        return True
    except _GUARDED_ERRORS as exc:
        log_event(
            run_id, event="live_capacity_check_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
        return False  # fail-closed: never open a position when capacity/balance is unknown


def _lookup_order(
    connector: BingXLiveTradingConnector, symbol: str, client_order_id: str
) -> dict | None:
    """Read-only lookup by the deterministic clientOrderID - the one and
    only way this module ever tries to learn an order's true state. Any
    error collapses to None here, deliberately identical to "order not
    found": both mean "cannot determine the true state right now", and
    every caller must treat them the same way (see _classify_order_state)."""
    try:
        return connector.get_order_by_client_order_id(symbol, client_order_id)
    except _ORDER_STATE_UNKNOWN_ERRORS:
        return None


def _classify_order_state(order: dict | None) -> str:
    """Never guesses. Returns exactly one of:
    - "FILLED": exchange confirms status == "FILLED".
    - "REJECTED": exchange confirms a terminal negative status
      (CANCELED/REJECTED/EXPIRED) with zero executed quantity - a genuine,
      unambiguous "this order will never fill".
    - "UNKNOWN": everything else - no order found, a lookup error, a still-
      open status (NEW), a PARTIALLY_FILLED status, or any unrecognized
      status string. UNKNOWN must never be treated as either outcome by
      any caller (2026-09-06 safety audit, Risk D fix)."""
    if order is None:
        return "UNKNOWN"
    status = order.get("status", "")
    executed_qty = Decimal(str(order.get("executedQty", "0") or "0"))
    if status == "FILLED":
        return "FILLED"
    if status in _TERMINAL_NEGATIVE_STATUSES and executed_qty == 0:
        return "REJECTED"
    return "UNKNOWN"


def _resolve_uncertain_entry(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position: Position,
    client_order_id: str,
    run_id: str,
    now: datetime,
    origin: str,
    placement_error: Exception | None = None,
) -> None:
    """The single place this module ever decides an entry's fate from a
    status lookup - used both right after a fresh placement attempt and by
    every later recovery/resolution pass, so there is exactly one
    classification policy, never a second, looser one. On UNKNOWN, this
    function writes nothing at all: the row is left exactly as it already
    is (CLAIMED or ENTRY_SUBMITTED), to be looked up again next tick.
    Preferring a stuck, manually-recoverable pending position over any risk
    of a duplicate live order is an explicit, user-mandated trade-off
    (2026-09-06 safety audit, Risk D fix)."""
    order = _lookup_order(connector, position.instrument, client_order_id)
    state = _classify_order_state(order)
    if state == "FILLED":
        repo.update_live_execution_submitted(
            position.position_id,
            entry_client_order_id=client_order_id,
            entry_exchange_order_id=client_order_id,
            entry_quantity=str(order.get("executedQty", "0")),  # type: ignore[union-attr]
            exchange_fill_entry=str(order.get("avgPrice", "0")),  # type: ignore[union-attr]
            sl_exchange_order_id=None,
            tp_exchange_order_id=None,
            updated_at=now,
        )
        log_event(
            run_id, event="live_order_confirmed_filled", position_id=position.position_id,
            instrument=position.instrument, origin=origin,
        )
        return
    if state == "REJECTED":
        detail = f"order confirmed rejected/canceled by exchange (status={order.get('status')})"  # type: ignore[union-attr]
        if placement_error is not None:
            detail = f"{detail}; placement error was {type(placement_error).__name__}: {placement_error}"
        repo.mark_live_execution_failed(position.position_id, detail, now)
        log_event(
            run_id, event="live_order_rejected", position_id=position.position_id,
            instrument=position.instrument, origin=origin,
        )
        return
    log_event(
        run_id, event="live_order_status_uncertain", position_id=position.position_id,
        instrument=position.instrument, origin=origin,
        placement_error_type=type(placement_error).__name__ if placement_error is not None else None,
    )


def _submit_entry_order(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position: Position,
    quantity: Decimal,
    margin_usdt: Decimal,
    notional_usdt: Decimal,
    leverage: int,
    run_id: str,
    now: datetime,
) -> None:
    client_order_id = _client_order_id(position.position_id, "e")
    try:
        connector.set_leverage(position.instrument, leverage=leverage)
        connector.place_entry_order_with_sl_tp(
            symbol=position.instrument,
            quantity=str(quantity),
            client_order_id=client_order_id,
            stop_loss_price=str(position.stop_loss),
            target_price=str(position.target),
        )
    except _ORDER_STATE_UNKNOWN_ERRORS as exc:
        # Placement itself errored (including a network timeout) - the
        # exchange may have received and processed the order despite the
        # error on our side. NEVER resubmit blindly: look up the
        # deterministic clientOrderID first (2026-09-06 safety audit,
        # Risk D fix, user-mandated).
        log_event(
            run_id, event="live_order_placement_uncertain", position_id=position.position_id,
            instrument=position.instrument, error_type=type(exc).__name__, error=str(exc),
        )
        _resolve_uncertain_entry(
            repo, connector, position, client_order_id, run_id, now,
            origin="placement_error", placement_error=exc,
        )
        return
    # Placement call itself succeeded - durably record that fact BEFORE
    # attempting to confirm the fill, so a crash here is recoverable as "an
    # order was submitted, only its outcome needs resolving" rather than
    # being indistinguishable from "never submitted" (restart-safety).
    repo.mark_live_execution_entry_submitted(position.position_id, client_order_id, now)
    log_event(
        run_id, event="live_order_placed", position_id=position.position_id,
        instrument=position.instrument, margin_usdt=str(margin_usdt),
        notional_usdt=str(notional_usdt), leverage=str(leverage),
    )
    _resolve_uncertain_entry(repo, connector, position, client_order_id, run_id, now, origin="post_submit")


def process_pending_positions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    quantity_precision_by_symbol: dict[str, int],
    min_notional_by_symbol: dict[str, Decimal],
    settings: Settings,
    run_id: str,
    now: datetime,
    limit: int = 10,
) -> None:
    """Layer 2, authoritative gate (spec §7): re-checks capacity/margin
    immediately before EACH claim, not once for the whole batch - this is
    what closes the race Gate can create by confirming multiple candidates
    in one discovery cycle. The moment capacity/margin is exhausted, this
    stops entirely for the rest of the tick; unclaimed positions are
    retried next tick, picked up automatically once a slot frees."""
    cfg = settings.live_execution
    for position in repo.find_positions_pending_live_execution(limit):
        if not has_sufficient_live_capacity(
            repo, connector, market_data_connector, cfg.max_concurrent_positions,
            cfg.margin_per_trade_usdt + cfg.margin_safety_buffer_usdt, run_id, now,
        ):
            break  # stop trying more this tick; PAPER's leg is unaffected
        precision = quantity_precision_by_symbol.get(position.instrument, 0)
        quantity = _quantity_for_live(
            position.simulated_fill_entry, cfg.margin_per_trade_usdt, cfg.leverage, precision
        )
        min_notional = min_notional_by_symbol.get(position.instrument, Decimal("0"))
        notional = quantity * position.simulated_fill_entry
        if quantity <= 0 or notional < min_notional:
            if not repo.claim_live_execution(
                position.position_id, now, str(cfg.margin_per_trade_usdt),
                str(cfg.margin_per_trade_usdt * cfg.leverage), str(cfg.leverage),
            ):
                continue
            repo.mark_live_execution_skipped(position.position_id, "below_exchange_minimum", now)
            log_event(
                run_id, event="live_skipped_below_minimum", position_id=position.position_id,
                instrument=position.instrument,
            )
            continue
        if not repo.claim_live_execution(
            position.position_id, now, str(cfg.margin_per_trade_usdt),
            str(cfg.margin_per_trade_usdt * cfg.leverage), str(cfg.leverage),
        ):
            continue  # another run/duplicate observation already claimed it
        _submit_entry_order(
            repo, connector, position, quantity, cfg.margin_per_trade_usdt,
            cfg.margin_per_trade_usdt * cfg.leverage, cfg.leverage, run_id, now,
        )


def recover_stale_claims(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    run_id: str,
    now: datetime,
    stale_after_seconds: int,
) -> None:
    """Crash recovery for rows stuck in CLAIMED past the grace window: looks
    the order up by its deterministic clientOrderID - NEVER resubmits.

    2026-09-06 safety audit (Risk D fix): the previous version fell through
    to a blind `_submit_entry_order()` call whenever the lookup wasn't an
    exact "FILLED" match - including when the order was merely still
    pending, partially filled, or the lookup itself failed - which could
    have placed a genuine duplicate live order. Now routed through the same
    `_resolve_uncertain_entry()` every other recovery path uses: a
    definite FILLED promotes to ACTIVE, a definite REJECTED marks FAILED,
    and anything else leaves the row exactly as CLAIMED, retried again next
    tick. No longer needs quantity/precision/settings - it never places an
    order, only ever looks one up."""
    stale_before = now - timedelta(seconds=stale_after_seconds)
    for row in repo.find_stale_claimed_live_executions(stale_before):
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        client_order_id = _client_order_id(position.position_id, "e")
        _resolve_uncertain_entry(
            repo, connector, position, client_order_id, run_id, now, origin="stale_claim_recovery",
        )


def resolve_pending_entries(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    run_id: str,
    now: datetime,
) -> None:
    """Re-resolves any ENTRY_SUBMITTED row whose fill outcome wasn't yet
    determined when it was first submitted - the counterpart to
    recover_stale_claims() for orders already known to have reached the
    exchange (2026-09-06 safety audit, Risk D fix). Never resubmits, only
    re-runs the same deterministic clientOrderID lookup. Not gated by a
    staleness window like recover_stale_claims's CLAIMED rows: an order
    that has already, definitely reached the exchange should be resolved
    the moment its outcome becomes knowable, not delayed."""
    for row in repo.find_active_live_executions():
        if row["phase"] != "ENTRY_SUBMITTED":
            continue
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        client_order_id = row.get("entry_client_order_id") or _client_order_id(position.position_id, "e")
        _resolve_uncertain_entry(
            repo, connector, position, client_order_id, run_id, now, origin="pending_entry_resolution",
        )


def close_guardian_exit_positions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    run_id: str,
    now: datetime,
) -> None:
    """LIVE's equivalent of demo_execution.py's close_guardian_exit_positions:
    never re-runs Guardian's classification (zero extra AI cost, zero
    divergence risk), only mirrors a PAPER position ALREADY closed with
    exit_reason='guardian_exit'. Guardian can close a LIVE position earlier
    than the 6h limit; it structurally cannot extend past it, because
    close_time_limit_positions() runs before process_pending_positions()
    ever considers a NEW claim, and this function only ever fires on a
    position that already exists as ACTIVE - PAPER's own 6h-independent
    guardian_exit decision is the only trigger, never a live re-evaluation."""
    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            continue
        position = repo.get_position(row["position_id"])
        if position is None or position.status != "CLOSED" or position.exit_reason != "guardian_exit":
            continue
        try:
            connector.cancel_all_open_orders(position.instrument)
            client_order_id = _client_order_id(position.position_id, "g")
            result = connector.close_position_market(
                position.instrument, quantity=row.get("entry_quantity") or "0",
                client_order_id=client_order_id,
            )
            repo.close_live_execution(
                position.position_id, "GUARDIAN_EXIT", str(result.get("avgPrice", "")), now
            )
            log_event(run_id, event="live_guardian_exit_closed", position_id=position.position_id)
        except _GUARDED_ERRORS as exc:
            log_event(
                run_id, event="live_guardian_exit_close_failed", position_id=position.position_id,
                error_type=type(exc).__name__, error=str(exc),
            )


def close_time_limit_positions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    max_position_hold_hours: int,
    run_id: str,
    now: datetime,
) -> None:
    """LIVE's own hard time limit (default 6h, live_execution.yaml),
    completely independent of PAPER's 24h - the caller passes
    settings.live_execution.max_position_hold_hours, never
    settings.risk_limits.max_position_hold_hours. Reuses compute_hold_hours
    so PAPER/Demo/Live never disagree on elapsed time for the SAME
    position, only on the threshold each applies to it."""
    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            continue
        position = repo.get_position(row["position_id"])
        if position is None or position.status != "OPEN_POSITION":
            continue
        if compute_hold_hours(position, now) < max_position_hold_hours:
            continue
        try:
            connector.cancel_all_open_orders(position.instrument)
            client_order_id = _client_order_id(position.position_id, "x")
            result = connector.close_position_market(
                position.instrument, quantity=row.get("entry_quantity") or "0",
                client_order_id=client_order_id,
            )
            repo.close_live_execution(
                position.position_id, "TIME_LIMIT", str(result.get("avgPrice", "")), now
            )
            log_event(run_id, event="live_time_limit_closed", position_id=position.position_id)
        except _GUARDED_ERRORS as exc:
            log_event(
                run_id, event="live_time_limit_close_failed", position_id=position.position_id,
                error_type=type(exc).__name__, error=str(exc),
            )
