from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from crypto_trading.connectors.bingx_live_trading import (
    BingXLiveTradingConnector,
    LiveExecutionGuardError,
    OrderRejectedError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event
from crypto_trading.storage.repository import Repository

# This module is intentionally standalone (design spec "Integration point"):
# it NEVER imports from or modifies live_execution.py, position_closing.py,
# profit_protection_experiment.py, or anything under crypto_trading/backtest/.
# Where it needs the exact same deterministic-client-order-id discipline and
# lookup-then-classify pattern live_execution.py already uses for entry
# orders, that logic is replicated here (not imported) so this module has no
# dependency edge onto live_execution.py at all.

# Any of these during a status lookup OR a cancel attempt means the true
# exchange outcome cannot be determined/confirmed right now - identical
# reasoning to live_execution.py's own _ORDER_STATE_UNKNOWN_ERRORS
# (2026-09-06 safety audit, Risk D fix): httpx.TransportError is included
# because BingXLiveTradingConnector's own tenacity retry re-raises it
# verbatim after exhausting retries. Never guess, never blind-retry a write
# whose outcome is unknown.
_UNKNOWN_OUTCOME_ERRORS = (ConnectorUnavailableError, LiveExecutionGuardError, httpx.TransportError)

_ACTIVE_SL_STATUSES = frozenset({"NEW", "PENDING"})

# 2026-09-13 deep-review fix 5: how far entry_quantity (recorded locally at
# entry) may diverge from the exchange's own positionAmt (read fresh at PP
# time) before this is treated as an unsafe under-sizing risk rather than
# ordinary Decimal-formatting noise. Exchange state is always the source of
# truth (design spec) - this tolerance exists only to avoid aborting on
# harmless representation differences, never to paper over a genuine
# partial-fill/partial-close divergence.
_QUANTITY_MISMATCH_TOLERANCE = Decimal("0.005")


def unrealized_profit_pct(avg_price: Decimal, mark_price: Decimal) -> Decimal:
    """LONG-only (this codebase's sole supported direction, see
    paper_trading/position_opening.py::_DIRECTION). avg_price is the
    real exchange fill entry (get_position()'s own 'avgPrice' field -
    exchange state, never this module's locally-stored data), mark_price
    is the real exchange mark price (get_position()'s 'markPrice' -
    consistent with this project's SL/TP orders, which already trigger
    on workingType=MARK_PRICE, not last-traded price)."""
    return (mark_price - avg_price) / avg_price


def _client_order_id(position_id: str, suffix: str) -> str:
    """Deterministic, restart-safe client order id - same pattern as
    live_execution.py's _client_order_id(), replicated here (not imported)
    per this module's no-dependency-on-live_execution.py rule. Suffix "pp"
    distinguishes a Profit Protection break-even SL from live_execution.py's
    own "e"/"g"/"x" suffixed order ids."""
    return f"lv{position_id[:24]}{suffix}"[:32]


def _lookup_new_sl_order(
    connector: BingXLiveTradingConnector, instrument: str, client_order_id: str
) -> dict | None:
    """Read-only lookup by the deterministic clientOrderID - the one and
    only way this module ever tries to learn the new SL's true state,
    exactly mirroring live_execution.py's own _lookup_order()/
    _resolve_uncertain_entry() discipline: look up, never resubmit blindly.
    Any error collapses to None here, deliberately identical to "order not
    found" - both mean "cannot determine the true state right now"."""
    try:
        return connector.get_order_by_client_order_id(instrument, client_order_id)
    except _UNKNOWN_OUTCOME_ERRORS:
        return None


def _classify_new_sl_state(order: dict | None) -> str:
    """Never guesses. Returns exactly one of:
    - "ACTIVE": exchange confirms status is NEW/PENDING - genuinely live.
    - "FILLED": exchange confirms status == "FILLED" - the position closed
      at/near breakeven during verification, a valid outcome, not a bug.
    - "UNKNOWN": everything else - no order found, a lookup error, or any
      unrecognized status string. UNKNOWN must never be treated as either
      outcome above; the caller only ever maps it to UNCERTAIN_NEW_SL_STATUS
      and never cancels the old SL in that branch."""
    if order is None:
        return "UNKNOWN"
    status = order.get("status", "")
    if status in _ACTIVE_SL_STATUSES:
        return "ACTIVE"
    if status == "FILLED":
        return "FILLED"
    return "UNKNOWN"


def _parse_positive_decimal(value: object) -> Decimal | None:
    """2026-09-13 deep-review fix 1: never guesses. Returns a Decimal only
    for a genuinely parseable, strictly-positive value - None/""/"0"/a
    negative value/a non-numeric string/an unparseable type all return
    None. The caller MUST abort rather than send a non-positive or
    unparseable quantity to a real order (the exact bug this closes:
    live_execution.py's own _resolve_uncertain_entry can leave
    entry_quantity as "0" when a fill-lookup response is missing
    executedQty; that "0" must never reach place_stop_loss_order)."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _handle_new_sl_filled(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    old_sl_order_id: str,
    run_id: str,
    now: datetime,
) -> None:
    """The new SL was found FILLED (price already reached breakeven before -
    or during - verification), a valid, safe outcome of a market-triggered
    stop, not an error. Shared by _place_and_verify_new_sl's own FILLED
    branch and Task 6 restart-recovery's Case B (new SL found FILLED on
    resume) - both mean exactly the same thing: re-check real exchange
    position state rather than assume, and never cancel the old SL from
    here (it is likely already gone too, since the position is flat)."""
    still_open = connector.get_position(instrument)
    if still_open is None:
        repo.set_live_profit_protection_status(
            position_id, "SL_REPLACED", now,
            last_error="position closed during new SL verification (filled at/near breakeven)",
        )
        log_event(
            run_id, event="live_pp_closed_during_verification", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, status="SL_REPLACED",
        )
    else:
        # Shouldn't happen (a FILLED SL implies the position went flat) -
        # treat conservatively as uncertain rather than guess, and log at a
        # level that surfaces the anomaly for human review.
        repo.set_live_profit_protection_status(
            position_id, "UNCERTAIN_NEW_SL_STATUS", now,
            last_error="new SL reported FILLED but position is still open on the exchange",
        )
        log_event(
            run_id, event="live_pp_new_sl_status_uncertain", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="UNCERTAIN_NEW_SL_STATUS", anomaly="filled_but_position_open",
        )
    # old SL is NEVER cancelled in either branch above.


def _finalize_verified_active_new_sl(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    new_sl_order_id: str,
    old_sl_order_id: str,
    run_id: str,
    now: datetime,
) -> None:
    """The "verified-active tail": record the new SL, re-check the real
    exchange position immediately before cancelling the old SL (deep-review
    fix 3's orphan-SL race guard), cancel it, and finalize. Reached both by
    _place_and_verify_new_sl's own ACTIVE branch (a fresh placement just
    confirmed active) and by Task 6 restart-recovery's Case B (new SL found
    already ACTIVE on resume) - in both cases the new SL is positively
    confirmed live and only the removal of the old one remains, so the
    remaining work is identical."""
    repo.update_live_profit_protection_new_sl(position_id, new_sl_order_id=new_sl_order_id, updated_at=now)
    log_event(
        run_id, event="live_pp_new_sl_verified", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
    )

    # Deep-review fix 3 (orphan-SL race): re-check the REAL exchange
    # position immediately before cancelling the old SL. Placement +
    # verification can span a real window (retries, network latency); if
    # the position closed by some other means (e.g. TP filled) during that
    # window, cancelling the old SL now would be a guess about an order
    # that may no longer matter, made on the unverified assumption that
    # BingX safely rejects/no-ops stop orders against a flat position - an
    # assumption the design spec explicitly refuses to make. Touch neither
    # order further; flag for human/reconciliation review instead.
    still_open_before_cancel = connector.get_position(instrument)
    if still_open_before_cancel is None:
        repo.set_live_profit_protection_status(
            position_id, "POSITION_CLOSED_DURING_REPLACEMENT", now,
            last_error=(
                "position closed on the exchange between new SL verification and "
                "old SL cancellation; neither order was touched further"
            ),
        )
        log_event(
            run_id, event="live_pp_position_closed_before_cancel", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="POSITION_CLOSED_DURING_REPLACEMENT",
        )
        return

    # Step 7 (spec steps 5-7): cancel the old SL, now that the new one is
    # confirmed active AND the position is confirmed still open.
    try:
        connector.cancel_order(instrument, old_sl_order_id)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Both order IDs are already durably recorded above - the position
        # has >=1 real protective order at all times. No third order is
        # ever placed; this status permanently blocks further PP attempts
        # for this position (human/reconciliation review only).
        repo.set_live_profit_protection_status(
            position_id, "REPLACEMENT_PARTIAL", now, last_error=str(exc),
        )
        log_event(
            run_id, event="live_pp_replacement_partial", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="REPLACEMENT_PARTIAL", error=str(exc),
        )
        return

    # Deep-review fix 4: the cancel above is the last IRREVERSIBLE step -
    # the replacement is already a real, complete success at this point.
    # Record that success FIRST. The final-state read below is purely
    # informational (for log visibility); it must never be able to leave a
    # genuinely successful replacement recorded as anything other than
    # SL_REPLACED if it happens to fail.
    repo.set_live_profit_protection_status(position_id, "SL_REPLACED", now)
    log_event(
        run_id, event="live_pp_sl_replaced", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id, status="SL_REPLACED",
    )
    try:
        remaining_orders = connector.get_open_orders(instrument)
        log_event(
            run_id, event="live_pp_final_state_check", position_id=position_id, instrument=instrument,
            remaining_open_order_types=[order.get("type") for order in remaining_orders],
        )
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        log_event(
            run_id, event="live_pp_final_state_check_failed", position_id=position_id,
            instrument=instrument, error_type=type(exc).__name__, error=str(exc),
        )


def _place_and_verify_new_sl(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    old_sl_order_id: str,
    breakeven_price: Decimal,
    new_sl_client_order_id: str,
    position_amt: object,
    run_id: str,
    now: datetime,
) -> None:
    """Spec steps 3-4 (place the break-even SL, then verify it is genuinely
    active) - shared by a fresh claim's _run_claimed_sequence (old SL just
    identified in the same call) and by Task 6 restart-recovery's Case A
    (full resume) and Case B's not-found-on-resume branch (old SL already
    known from the row; placement was never confirmed before the crash, so
    it is safe to retry under the SAME deterministic client order id - that
    determinism is exactly what makes a lookup-first retry safe, never a
    newly-minted id)."""
    # Quantity is sourced fresh from the repository (never a cached/stale
    # value) - the same entry_quantity recorded when this position's live
    # entry was confirmed.
    live_execution = repo.get_live_execution(position_id)
    raw_entry_quantity = (live_execution or {}).get("entry_quantity")
    entry_quantity = _parse_positive_decimal(raw_entry_quantity)

    # Deep-review fix 1: a missing/zero/unparseable entry_quantity (a real,
    # reachable state - see live_execution.py's _resolve_uncertain_entry)
    # must abort HERE, before any placement - never fall back to "0" and
    # send a zero-quantity stop to a real exchange. Old SL is untouched.
    if entry_quantity is None:
        repo.set_live_profit_protection_status(
            position_id, "ABORTED_INVALID_ENTRY_QUANTITY", now,
            last_error=f"entry_quantity is missing/non-positive/unparseable: {raw_entry_quantity!r}",
        )
        log_event(
            run_id, event="live_pp_aborted_invalid_entry_quantity", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="ABORTED_INVALID_ENTRY_QUANTITY",
        )
        return

    # Deep-review fix 5: cross-check against the exchange's own, freshly-read
    # positionAmt (already in hand from the caller's own get_position() call)
    # - exchange state is the source of truth. A stale/diverged local
    # entry_quantity (e.g. after a partial close) would under-size the
    # replacement SL and leave part of a real position unprotected once the
    # old SL is cancelled - abort rather than guess. Old SL is untouched.
    position_amt_decimal = _parse_positive_decimal(position_amt)
    quantity_mismatch = (
        position_amt_decimal is None
        or abs(entry_quantity - position_amt_decimal) / position_amt_decimal > _QUANTITY_MISMATCH_TOLERANCE
    )
    if quantity_mismatch:
        repo.set_live_profit_protection_status(
            position_id, "ABORTED_QUANTITY_MISMATCH", now,
            last_error=(
                f"entry_quantity {entry_quantity} vs exchange positionAmt {position_amt!r} "
                f"exceeds tolerance {_QUANTITY_MISMATCH_TOLERANCE}"
            ),
        )
        log_event(
            run_id, event="live_pp_aborted_quantity_mismatch", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            entry_quantity=str(entry_quantity), position_amt=str(position_amt),
            status="ABORTED_QUANTITY_MISMATCH",
        )
        return

    try:
        connector.place_stop_loss_order(
            instrument, quantity=str(entry_quantity), stop_price=str(breakeven_price),
            client_order_id=new_sl_client_order_id,
        )
    except OrderRejectedError as exc:
        # Synchronous, structured rejection - zero fill guaranteed, nothing
        # to look up. Old SL was never touched.
        repo.set_live_profit_protection_status(
            position_id, "ABORTED_NEW_SL_REJECTED", now, last_error=str(exc),
        )
        log_event(
            run_id, event="live_pp_new_sl_rejected", position_id=position_id, instrument=instrument,
            old_sl_order_id=old_sl_order_id, breakeven_price=str(breakeven_price),
            status="ABORTED_NEW_SL_REJECTED", error=str(exc),
        )
        return

    # Step 6 (spec step 4): verify the new SL is genuinely active before
    # ever touching the old one - the single most important invariant here.
    new_sl_order = _lookup_new_sl_order(connector, instrument, new_sl_client_order_id)
    state = _classify_new_sl_state(new_sl_order)

    if state == "UNKNOWN":
        repo.set_live_profit_protection_status(
            position_id, "UNCERTAIN_NEW_SL_STATUS", now,
            last_error=f"new SL status could not be determined (lookup result: {new_sl_order!r})",
        )
        log_event(
            run_id, event="live_pp_new_sl_status_uncertain", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="UNCERTAIN_NEW_SL_STATUS",
        )
        return  # old SL is NEVER cancelled in this branch.

    if state == "FILLED":
        _handle_new_sl_filled(repo, connector, position_id, instrument, old_sl_order_id, run_id, now)
        return

    # state == "ACTIVE": the new SL is genuinely live. Only now record it
    # and proceed to remove the old one (add-before-remove).
    new_sl_order_id = str(new_sl_order.get("orderId"))  # type: ignore[union-attr]
    _finalize_verified_active_new_sl(
        repo, connector, position_id, instrument, new_sl_order_id, old_sl_order_id, run_id, now,
    )


def _run_claimed_sequence(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    breakeven_price: Decimal,
    new_sl_client_order_id: str,
    position_amt: object,
    run_id: str,
    now: datetime,
) -> None:
    """Steps 4-7 of the design spec's sequence, run immediately after a
    successful claim (and reused verbatim by Task 6 restart-recovery's Case
    A, which has exactly the same starting shape: no old SL identified yet).
    Add-before-remove throughout: no code path here ever cancels the old SL
    before the new SL has been positively confirmed NEW/PENDING."""
    # Step 4 (spec step 2): identify exactly one existing protective SL.
    open_orders = connector.get_open_orders(instrument)
    sl_orders = [order for order in open_orders if order.get("type") == "STOP_MARKET"]
    if len(sl_orders) != 1:
        repo.set_live_profit_protection_status(
            position_id, "ABORTED_AMBIGUOUS_SL", now,
            last_error=f"found {len(sl_orders)} STOP_MARKET orders, expected exactly 1",
        )
        log_event(
            run_id, event="live_pp_aborted_ambiguous_sl", position_id=position_id,
            instrument=instrument, sl_order_count=len(sl_orders), status="ABORTED_AMBIGUOUS_SL",
        )
        return

    old_sl = sl_orders[0]
    old_sl_order_id = str(old_sl.get("orderId"))
    old_sl_price = str(old_sl.get("stopPrice"))
    repo.update_live_profit_protection_old_sl(
        position_id, old_sl_order_id=old_sl_order_id, old_sl_price=old_sl_price, updated_at=now,
    )
    log_event(
        run_id, event="live_pp_old_sl_identified", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, old_sl_price=old_sl_price,
    )

    # Step 5-6 (spec steps 3-4): place the new break-even SL and verify it.
    _place_and_verify_new_sl(
        repo, connector, position_id, instrument, old_sl_order_id, breakeven_price,
        new_sl_client_order_id, position_amt, run_id, now,
    )


def _recover_case_a(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    row: dict,
    instrument: str,
    run_id: str,
    now: datetime,
) -> None:
    """Case A: old_sl_order_id is None on the row - the crash happened
    before step 4 ever completed, so nothing was ever placed on the
    exchange for this attempt. This is exactly the situation
    _run_claimed_sequence already handles correctly from scratch (no old SL
    was identified yet, so re-running step 4 onward carries zero risk of
    misidentifying which SL is "old"). The row's already-stored
    breakeven_price/new_sl_client_order_id are reused verbatim - never a
    newly-generated client order id, since the deterministic id is what
    makes retry-by-lookup safe."""
    position_id = row["position_id"]
    live_position = connector.get_position(instrument)
    if live_position is None:
        repo.set_live_profit_protection_status(
            position_id, "POSITION_CLOSED_BEFORE_PP", now,
            last_error="restart recovery (Case A): position already closed; no old SL had been identified",
        )
        log_event(
            run_id, event="live_pp_recovery_position_closed_before_pp", position_id=position_id,
            instrument=instrument, status="POSITION_CLOSED_BEFORE_PP", recovery_case="A",
        )
        return

    position_amt = live_position.get("positionAmt", "0")
    breakeven_price = Decimal(str(row["breakeven_price"]))
    _run_claimed_sequence(
        repo, connector, position_id, instrument, breakeven_price,
        row["new_sl_client_order_id"], position_amt, run_id, now,
    )


def _recover_case_b(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    row: dict,
    instrument: str,
    old_sl_order_id: str,
    run_id: str,
    now: datetime,
) -> None:
    """Case B: old_sl_order_id is set but new_sl_order_id is None on the
    row - step 4 completed, but the crash happened somewhere in
    placement/verification, before, during, or after placing the new SL,
    but before it was confirmed ACTIVE. The new SL's true state is looked
    up by its deterministic client order id (never a freshly-generated
    one) - exactly what makes this safe."""
    position_id = row["position_id"]
    live_position = connector.get_position(instrument)
    if live_position is None:
        repo.set_live_profit_protection_status(
            position_id, "POSITION_CLOSED_BEFORE_PP", now,
            last_error=(
                "restart recovery (Case B): position already closed; new SL was never "
                "confirmed placed"
            ),
        )
        log_event(
            run_id, event="live_pp_recovery_position_closed_before_pp", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="POSITION_CLOSED_BEFORE_PP", recovery_case="B",
        )
        return

    position_amt = live_position.get("positionAmt", "0")

    # Fix 2 (deep review): the old SL can be gone by the time this recovery
    # path runs (externally cancelled, or triggered/removed during the
    # exact downtime this recovery path exists to handle) with the new SL
    # never placed either - zero STOP_MARKET orders at all while the
    # position is still open. Detect this the SAME way Case C does, before
    # ever considering a placement or relying on a later cancel_order call
    # against an order whose existence was never verified in this tick.
    try:
        open_orders = connector.get_open_orders(instrument)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Cannot determine which orders remain right now - do not guess.
        # The row stays CLAIMED; a later tick's recovery pass will retry
        # once the exchange is reachable again.
        log_event(
            run_id, event="live_pp_recovery_open_orders_lookup_failed", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            error_type=type(exc).__name__, error=str(exc), recovery_case="B",
        )
        return

    sl_orders = [order for order in open_orders if order.get("type") == "STOP_MARKET"]
    if not sl_orders:
        repo.set_live_profit_protection_status(
            position_id, "ANOMALY_NO_PROTECTIVE_ORDER_FOUND", now,
            last_error=(
                "restart recovery (Case B): position still open but zero STOP_MARKET "
                "orders exist at all (neither old nor a new one was ever confirmed placed)"
            ),
        )
        log_event(
            run_id, event="live_pp_anomaly_no_protective_order_found", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="ANOMALY_NO_PROTECTIVE_ORDER_FOUND", severity="ERROR", recovery_case="B",
        )
        return

    new_sl_client_order_id = row["new_sl_client_order_id"]
    # Fix 3 (deep review): a genuine lookup error must NEVER be collapsed
    # into "not found" here - that would silently downgrade an unknown
    # outcome into a real placement attempt, a severity regression from
    # Task 5's original fail-closed discipline (_resolve_uncertain_entry:
    # look up, never resubmit, when the true state can't be determined).
    # Deliberately does NOT use _lookup_new_sl_order (which swallows every
    # _UNKNOWN_OUTCOME_ERRORS into None, identical to "not found") - the
    # raise is caught here, explicitly, so it can be handled differently.
    try:
        new_sl_order = connector.get_order_by_client_order_id(instrument, new_sl_client_order_id)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Leave the row CLAIMED; a later tick's recovery pass will retry
        # the lookup once the exchange is reachable again. Never guess.
        log_event(
            run_id, event="live_pp_recovery_new_sl_lookup_failed", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            error_type=type(exc).__name__, error=str(exc), recovery_case="B",
        )
        return

    state = _classify_new_sl_state(new_sl_order)

    if state == "ACTIVE":
        # The new SL WAS placed successfully before the crash - resume from
        # right after verification.
        new_sl_order_id = str(new_sl_order.get("orderId"))  # type: ignore[union-attr]
        _finalize_verified_active_new_sl(
            repo, connector, position_id, instrument, new_sl_order_id, old_sl_order_id, run_id, now,
        )
        return

    if state == "FILLED":
        _handle_new_sl_filled(repo, connector, position_id, instrument, old_sl_order_id, run_id, now)
        return

    # state == "UNKNOWN" here means the lookup call itself SUCCEEDED (a
    # genuine error already returned above, never reaching this line) and
    # returned either no order at all or an unrecognized status - a real,
    # determinate "not found" response. The new SL was never confirmed
    # placed - safe to retry placement using the row's stored
    # old_sl_order_id/breakeven_price/new_sl_client_order_id (the SAME id,
    # never a new one).
    breakeven_price = Decimal(str(row["breakeven_price"]))
    _place_and_verify_new_sl(
        repo, connector, position_id, instrument, old_sl_order_id, breakeven_price,
        new_sl_client_order_id, position_amt, run_id, now,
    )


def _recover_case_c(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    row: dict,
    instrument: str,
    old_sl_order_id: str,
    new_sl_order_id: str,
    run_id: str,
    now: datetime,
) -> None:
    """Case C: new_sl_order_id is set on the row - step 6 completed and was
    confirmed ACTIVE before the crash; only the pre-cancel position
    re-check, the cancel itself, or the final status write didn't complete.
    """
    position_id = row["position_id"]
    live_position = connector.get_position(instrument)
    if live_position is None:
        # Both orders are already durably recorded. Per the established
        # policy elsewhere in this module, do not attempt to touch either
        # order without a verified need - the position is already flat, so
        # there is nothing left to protect, and touching orders on a flat
        # position now would be exactly the "guess based on an unverified
        # assumption" the design forbids.
        repo.set_live_profit_protection_status(
            position_id, "POSITION_CLOSED_DURING_REPLACEMENT", now,
            last_error=(
                "restart recovery (Case C): position already closed; both orders were "
                "already recorded, neither touched"
            ),
        )
        log_event(
            run_id, event="live_pp_recovery_position_closed_during_replacement", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="POSITION_CLOSED_DURING_REPLACEMENT", recovery_case="C",
        )
        return

    try:
        open_orders = connector.get_open_orders(instrument)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Cannot determine which orders remain right now - do not guess.
        # The row stays CLAIMED; a later tick's recovery pass will retry
        # once the exchange is reachable again.
        log_event(
            run_id, event="live_pp_recovery_open_orders_lookup_failed", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            error_type=type(exc).__name__, error=str(exc), recovery_case="C",
        )
        return

    sl_orders = [order for order in open_orders if order.get("type") == "STOP_MARKET"]
    sl_order_ids = {str(order.get("orderId")) for order in sl_orders}
    old_present = old_sl_order_id in sl_order_ids
    new_present = new_sl_order_id in sl_order_ids

    if not old_present and not new_present:
        # Neither of OUR OWN recorded protective orders is present - an
        # anomaly this codebase's own logic should never itself produce
        # (add-before-remove guarantees >=1 protective order at every
        # self-caused transition), regardless of any other unrelated orders
        # that might exist on the instrument. Do NOT auto-heal (do not
        # guess a price and place a fresh SL). Logged with a clearly
        # ERROR-signalling event name/field for human attention (log_event
        # itself always logs at INFO - there is no lower-level primitive
        # available in this module).
        repo.set_live_profit_protection_status(
            position_id, "ANOMALY_NO_PROTECTIVE_ORDER_FOUND", now,
            last_error=(
                "restart recovery (Case C): position still open but neither the recorded "
                "old nor new SL order is present among open STOP_MARKET orders"
            ),
        )
        log_event(
            run_id, event="live_pp_anomaly_no_protective_order_found", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="ANOMALY_NO_PROTECTIVE_ORDER_FOUND", severity="ERROR", recovery_case="C",
        )
        return

    if not old_present:
        # new_present is True here (the both-absent case returned above) -
        # the old SL is already gone - the operation had actually already
        # fully succeeded before the crash; only the final status write
        # never landed.
        repo.set_live_profit_protection_status(
            position_id, "SL_REPLACED", now,
            last_error="old SL already cancelled before restart; only the status write was missing",
        )
        log_event(
            run_id, event="live_pp_sl_replaced", position_id=position_id, instrument=instrument,
            old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="SL_REPLACED", recovered=True, recovery_case="C",
        )
        return

    if not new_present:
        # CRITICAL fix (deep review): old_sl_order_id is present, but the
        # row's OWN recorded new_sl_order_id is NOT among the freshly-read
        # open orders - e.g. externally cancelled, or BingX auto-cancelling
        # a duplicate STOP_MARKET (exactly the exchange behavior the spec
        # refuses to assume). The cancel gate must be identity-based, never
        # presence/count-based: reaching the cancel below on this evidence
        # alone would risk leaving a real, still-open position with ZERO
        # stops recorded as a permanent SL_REPLACED success. Resolve the
        # new SL's actual state the same way Case B does - via its
        # deterministic client order id - and branch from there. The old
        # SL is NEVER cancelled purely because it happens to still be
        # present; only a freshly, positively confirmed new SL unlocks that.
        new_sl_client_order_id = row["new_sl_client_order_id"]
        new_sl_order = _lookup_new_sl_order(connector, instrument, new_sl_client_order_id)
        state = _classify_new_sl_state(new_sl_order)

        if state == "ACTIVE":
            # Authoritative direct confirmation overrides the (possibly
            # stale) open-orders read above - resume the tail normally.
            _finalize_verified_active_new_sl(
                repo, connector, position_id, instrument, new_sl_order_id, old_sl_order_id,
                run_id, now,
            )
            return

        if state == "FILLED":
            _handle_new_sl_filled(repo, connector, position_id, instrument, old_sl_order_id, run_id, now)
            return

        # state == "UNKNOWN" (not found / lookup failed / unrecognized
        # status): a new SL our own row claims was already confirmed ACTIVE
        # can no longer be positively verified, while the old SL is still
        # there (still protected, add-before-remove was never violated).
        # Never blindly retry a write here (this is not "never confirmed
        # placed" like Case B's UNKNOWN - our row already recorded it
        # ACTIVE once) - block for human/reconciliation review instead.
        repo.set_live_profit_protection_status(
            position_id, "UNCERTAIN_NEW_SL_STATUS", now,
            last_error=(
                "restart recovery (Case C): recorded new SL is missing from open orders and "
                f"could not be re-confirmed by lookup (result: {new_sl_order!r}); old SL left untouched"
            ),
        )
        log_event(
            run_id, event="live_pp_new_sl_status_uncertain", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="UNCERTAIN_NEW_SL_STATUS", recovery_case="C",
        )
        return

    # Both old_present and new_present are freshly, positively confirmed -
    # the ONLY safe cancel branch. Attempt the cancel exactly one more
    # time. This finite, evidence-based retry is safe: we have direct
    # proof from THIS read that a cancel is the only remaining, safe
    # action.
    try:
        connector.cancel_order(instrument, old_sl_order_id)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        repo.set_live_profit_protection_status(
            position_id, "REPLACEMENT_PARTIAL", now, last_error=str(exc),
        )
        log_event(
            run_id, event="live_pp_replacement_partial", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="REPLACEMENT_PARTIAL", error=str(exc), recovery_case="C",
        )
        return

    repo.set_live_profit_protection_status(position_id, "SL_REPLACED", now)
    log_event(
        run_id, event="live_pp_sl_replaced", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
        status="SL_REPLACED", recovery_case="C",
    )


def _recover_claimed_position(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    row: dict,
    run_id: str,
    now: datetime,
) -> None:
    """Resolves one CLAIMED live_profit_protection row left over from an
    interrupted prior tick (crash/restart), per the design spec's
    "Restart / crash recovery" section. Always re-derives truth from the
    exchange - never assumes, never blindly resumes mid-recipe. Exactly one
    of three cases applies, determined solely by which fields are already
    populated on the row: the sequence this module runs is strictly
    single-threaded and sequential, so old_sl_order_id can only ever be set
    before the new SL placement was attempted, and new_sl_order_id can only
    ever be set after the new SL was confirmed ACTIVE (which is always
    after old_sl_order_id was already set)."""
    position_id = row["position_id"]
    position_record = repo.get_position(position_id)
    if position_record is None:
        return  # defensive: a claimed PP row always has a positions row
    instrument = position_record.instrument

    old_sl_order_id = row.get("old_sl_order_id")
    new_sl_order_id = row.get("new_sl_order_id")

    if old_sl_order_id is None:
        _recover_case_a(repo, connector, row, instrument, run_id, now)
    elif new_sl_order_id is None:
        _recover_case_b(repo, connector, row, instrument, old_sl_order_id, run_id, now)
    else:
        _recover_case_c(repo, connector, row, instrument, old_sl_order_id, new_sl_order_id, run_id, now)


def _process_position(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    threshold_pct: Decimal,
    run_id: str,
    now: datetime,
) -> None:
    """One ACTIVE, not-yet-PP-claimed position's worth of work for a single
    tick. Deliberately factored out of run_live_profit_protection_tick's
    loop so the whole thing can be wrapped in one try/except per position
    (deep-review fix 2) - see that function's docstring for why."""
    position_record = repo.get_position(position_id)
    if position_record is None:
        return  # defensive: an ACTIVE live_executions row always has a positions row
    instrument = position_record.instrument

    live_position = connector.get_position(instrument)
    if live_position is None:
        log_event(
            run_id, event="live_pp_position_gone_before_claim", position_id=position_id,
            instrument=instrument,
        )
        return  # real exchange counterpart already gone - nothing to protect, no row created

    # Deep-review fix 2: defensive .get(..., "0") reads, matching every
    # other caller's convention in bingx_live_trading.py/live_execution.py -
    # a malformed/partial position payload must never raise a bare KeyError
    # here (it may still raise elsewhere, e.g. a ZeroDivisionError from a
    # "0" avgPrice - the per-position try/except in the caller is what
    # actually makes THAT safe; this change alone only prevents the most
    # basic missing-key crash).
    avg_price = Decimal(str(live_position.get("avgPrice", "0")))
    mark_price = Decimal(str(live_position.get("markPrice", "0")))
    position_amt = live_position.get("positionAmt", "0")
    profit_pct = unrealized_profit_pct(avg_price, mark_price)
    if profit_pct < threshold_pct:
        return  # not yet at threshold - re-evaluated next tick, no row created

    new_sl_client_order_id = _client_order_id(position_id, "pp")
    claimed = repo.claim_live_profit_protection(
        position_id,
        threshold_pct=str(threshold_pct),
        trigger_mark_price=str(mark_price),
        breakeven_price=str(avg_price),
        new_sl_client_order_id=new_sl_client_order_id,
        claimed_at=now,
    )
    if not claimed:
        return  # race: another observation already claimed it (DB is the race defense)

    log_event(
        run_id, event="live_pp_claimed", position_id=position_id, instrument=instrument,
        trigger_mark_price=str(mark_price), breakeven_price=str(avg_price),
        threshold_pct=str(threshold_pct),
    )
    _run_claimed_sequence(
        repo, connector, position_id, instrument, avg_price, new_sl_client_order_id,
        position_amt, run_id, now,
    )


def run_live_profit_protection_tick(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    threshold_pct: Decimal,
    run_id: str,
    now: datetime,
) -> None:
    """LIVE Profit Protection (+threshold_pct -> break-even), see
    docs/superpowers/specs/2026-09-13-live-profit-protection-design.md.

    Scans ACTIVE-phase LIVE positions that do not yet have a
    live_profit_protection row (a position with ANY existing row - CLAIMED
    or a terminal status - is never re-examined here; resolving a
    crash-interrupted CLAIMED row is Task 6's restart-recovery pass, run
    before this scan, not this function's job). For each eligible position:
    checks the real exchange position and its unrealized profit vs
    threshold_pct, and if at/above threshold, claims and runs the full
    break-even SL replacement sequence.

    A position whose real exchange counterpart is already gone (`None`)
    when first checked here - i.e. BEFORE a live_profit_protection row has
    ever been claimed for it - is simply skipped: no row is created for a
    position that was never eligible to begin with, per the data model's
    "created the moment a PP attempt starts - never before" rule. The
    `POSITION_CLOSED_BEFORE_PP` status exists for Task 6's restart recovery
    (a row that already exists, found CLAIMED, whose position has since gone
    flat) - this function never writes it, since it never observes a
    position-closed outcome for a row it hasn't already claimed.

    Deep-review fix 2 (2026-09-13): each position's processing is isolated
    in its own try/except. This module is inserted before
    close_time_limit_positions/process_pending_positions in the real tick
    (see the design spec's "Integration point") - an uncaught exception
    from any one malformed position would otherwise abort the whole scan
    and silently skip time-limit exits for every OTHER live position that
    tick too. Since a failure here can occur BEFORE any claim is made, the
    same malformed position may keep failing every subsequent tick - that
    is an accepted, logged, non-blocking outcome (visible via the
    "live_pp_tick_error" event for human review), never one that is allowed
    to take down the rest of the batch.

    Task 6 (2026-09-13): restart/crash recovery runs FIRST, before the scan
    below - every row still CLAIMED from an interrupted prior tick is
    resolved forward to a terminal (or, for a fresh Case A retry, possibly
    all the way through the sequence) status by re-deriving truth from the
    exchange, per the design spec's "Restart / crash recovery" section. A
    CLAIMED row is never left CLAIMED after a tick observes it. Same
    per-position isolation discipline as the scan below: one bad recovery
    must never block recovering or scanning any other position."""
    for row in repo.find_claimed_live_profit_protection():
        position_id = row["position_id"]
        try:
            _recover_claimed_position(repo, connector, row, run_id, now)
        except Exception as exc:  # noqa: BLE001 - one bad recovery must never block the batch
            log_event(
                run_id, event="live_pp_recovery_error", position_id=position_id,
                error_type=type(exc).__name__, error=str(exc),
            )
            continue

    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            continue  # CLAIMED/ENTRY_SUBMITTED: not a real position yet
        position_id = row["position_id"]
        if repo.get_live_profit_protection(position_id) is not None:
            continue  # already claimed/terminal - Task 6 owns resolving this, not this scan
        try:
            _process_position(repo, connector, position_id, threshold_pct, run_id, now)
        except Exception as exc:  # noqa: BLE001 - one bad position must never block the batch
            log_event(
                run_id, event="live_pp_tick_error", position_id=position_id,
                error_type=type(exc).__name__, error=str(exc),
            )
            continue
