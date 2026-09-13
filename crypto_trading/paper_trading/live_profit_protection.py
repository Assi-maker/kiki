from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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


def _run_claimed_sequence(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    breakeven_price: Decimal,
    new_sl_client_order_id: str,
    run_id: str,
    now: datetime,
) -> None:
    """Steps 4-7 of the design spec's sequence, run immediately after a
    successful claim. Add-before-remove throughout: no code path here ever
    cancels the old SL before the new SL has been positively confirmed
    NEW/PENDING."""
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

    # Step 5 (spec step 3): place the new break-even SL. Quantity is sourced
    # fresh from the repository (never a cached/stale value) - the same
    # entry_quantity recorded when this position's live entry was confirmed.
    live_execution = repo.get_live_execution(position_id)
    entry_quantity = (live_execution or {}).get("entry_quantity") or "0"
    try:
        connector.place_stop_loss_order(
            instrument, quantity=entry_quantity, stop_price=str(breakeven_price),
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
        # The price already reached breakeven between claim and placement -
        # a valid, safe outcome of a market-triggered stop, not an error.
        # Re-check real exchange position state rather than assume.
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
            # Shouldn't happen (a FILLED SL implies the position went flat)
            # - treat conservatively as uncertain rather than guess, and log
            # at a level that surfaces the anomaly for human review.
            repo.set_live_profit_protection_status(
                position_id, "UNCERTAIN_NEW_SL_STATUS", now,
                last_error="new SL reported FILLED but position is still open on the exchange",
            )
            log_event(
                run_id, event="live_pp_new_sl_status_uncertain", position_id=position_id,
                instrument=instrument, old_sl_order_id=old_sl_order_id,
                status="UNCERTAIN_NEW_SL_STATUS", anomaly="filled_but_position_open",
            )
        return  # old SL is NEVER cancelled in this branch either.

    # state == "ACTIVE": the new SL is genuinely live. Only now record it
    # and proceed to remove the old one (add-before-remove).
    new_sl_order_id = str(new_sl_order.get("orderId"))  # type: ignore[union-attr]
    repo.update_live_profit_protection_new_sl(position_id, new_sl_order_id=new_sl_order_id, updated_at=now)
    log_event(
        run_id, event="live_pp_new_sl_verified", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
    )

    # Step 7 (spec steps 5-7): cancel the old SL, now that the new one is
    # confirmed active.
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

    remaining_orders = connector.get_open_orders(instrument)
    log_event(
        run_id, event="live_pp_final_state_check", position_id=position_id, instrument=instrument,
        remaining_open_order_types=[order.get("type") for order in remaining_orders],
    )
    repo.set_live_profit_protection_status(position_id, "SL_REPLACED", now)
    log_event(
        run_id, event="live_pp_sl_replaced", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id, status="SL_REPLACED",
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
    position-closed outcome for a row it hasn't already claimed."""
    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            continue  # CLAIMED/ENTRY_SUBMITTED: not a real position yet
        position_id = row["position_id"]
        if repo.get_live_profit_protection(position_id) is not None:
            continue  # already claimed/terminal - Task 6 owns resolving this, not this scan

        position_record = repo.get_position(position_id)
        if position_record is None:
            continue  # defensive: an ACTIVE live_executions row always has a positions row
        instrument = position_record.instrument

        live_position = connector.get_position(instrument)
        if live_position is None:
            log_event(
                run_id, event="live_pp_position_gone_before_claim", position_id=position_id,
                instrument=instrument,
            )
            continue  # real exchange counterpart already gone - nothing to protect, no row created

        avg_price = Decimal(str(live_position["avgPrice"]))
        mark_price = Decimal(str(live_position["markPrice"]))
        profit_pct = unrealized_profit_pct(avg_price, mark_price)
        if profit_pct < threshold_pct:
            continue  # not yet at threshold - re-evaluated next tick, no row created

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
            continue  # race: another observation already claimed it (DB is the race defense)

        log_event(
            run_id, event="live_pp_claimed", position_id=position_id, instrument=instrument,
            trigger_mark_price=str(mark_price), breakeven_price=str(avg_price),
            threshold_pct=str(threshold_pct),
        )
        _run_claimed_sequence(
            repo, connector, position_id, instrument, avg_price, new_sl_client_order_id, run_id, now,
        )
