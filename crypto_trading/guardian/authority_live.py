"""Guardian Authority's LIVE stop-loss tightening (Task 5).

Places and cancels REAL stop-loss orders on a LIVE BingX account. This is
the most safety-critical I/O in the Guardian Authority extension, and it is
deliberately NOT a fresh design: it is a reuse-and-adapt of
``crypto_trading/paper_trading/live_profit_protection.py``'s add-before-
remove replacement sequence, which has already been through two rounds of
deep adversarial review and one Critical bug fix. The sequence structure,
the failure-mode statuses, the restart/crash-recovery decision tree and the
"never guess" discipline are copied from there rather than re-derived.

--------------------------------------------------------------------------
The one semantic difference from LIVE Profit Protection
--------------------------------------------------------------------------
The target stop price is a caller-supplied ``new_sl`` parameter instead of
the position's own break-even (``avgPrice``). Everything else - add-before-
remove ordering, the fresh position-liveness re-check immediately before
cancelling the old SL, the exhaustive failure-mode statuses, the
per-position claim isolation, the restart recovery cases A/B/C - is
preserved identically.

--------------------------------------------------------------------------
Independent re-verification of the tightening invariant (layer 2 of 2)
--------------------------------------------------------------------------
``crypto_trading/guardian/authority.py`` (the pure decision engine) already
guarantees it only ever proposes ``new_sl > current_sl``, and its caller
only invokes this module when that holds. This module trusts NEITHER of
those guarantees. Immediately before any order is placed - on EVERY
placement path, fresh path and restart-recovery path alike - it reads the
position's current real STOP_MARKET order from the exchange and
independently re-verifies ``new_sl > current_live_sl`` (LONG-only, strictly
greater). If that check fails for any reason - a stale caller, a race with
LIVE Profit Protection, an unparseable/non-positive current stop price, or
an upstream logic error - it refuses with ``ABORTED_INVALID_TIGHTENING``:
no order is placed, no order is cancelled, the existing protection is left
exactly where it is. It never clamps the value into range; a refusal is
always a refusal.

Because the invariant is re-verified from a FRESHLY-read current stop price
rather than from anything stored at claim time, tightening always ratchets
from observed truth. That is also what makes racing LIVE Profit Protection
safe: whichever mechanism placed the stop that is live right now, this
module identifies THAT order and ratchets from ITS price - it never assumes
which mechanism acted most recently.

--------------------------------------------------------------------------
Claim-table separation from LIVE Profit Protection
--------------------------------------------------------------------------
This module claims rows in ``guardian_authority_live_sl_actions``, a table
that is a shape-for-shape counterpart of ``live_profit_protection`` but is
never shared with it. The two mechanisms can therefore both hold a claim on
the same ``position_id`` simultaneously, each in its own table, each with
its own primary key, neither able to block, consume or overwrite the
other's row. This module never reads or writes LIVE Profit Protection's
table (enforced by a test).

--------------------------------------------------------------------------
Status vocabulary (PP's own names reused verbatim wherever they apply)
--------------------------------------------------------------------------
- ``CLAIMED`` - an attempt is in flight (or was interrupted mid-flight).
- ``SL_REPLACED`` - terminal success: the new SL is live and the old one is
  gone (or the position closed at/near the new stop during verification,
  which is a valid outcome of a market-triggered stop, not an error).
- ``ABORTED_INVALID_TIGHTENING`` - **the only status not present in PP**:
  the independent re-verification above refused. Nothing was placed.
- ``ABORTED_AMBIGUOUS_SL`` - the current real SL could not be identified
  unambiguously (zero or several STOP_MARKET orders, or - on a recovery
  resume - the single STOP_MARKET present is not the one this row
  recorded). Nothing was placed.
- ``ABORTED_INVALID_ENTRY_QUANTITY`` / ``ABORTED_QUANTITY_MISMATCH`` -
  PP's deep-review fixes 1 and 5, copied verbatim: a missing/non-positive
  locally-recorded quantity, or one that has diverged from the exchange's
  own ``positionAmt``, must never size a real replacement stop.
- ``ABORTED_NEW_SL_REJECTED`` - the exchange rejected the new SL
  synchronously (zero fill guaranteed, nothing to look up).
- ``UNCERTAIN_NEW_SL_STATUS`` - the new SL's true state cannot be
  determined. The old SL is NEVER cancelled in this branch.
- ``REPLACEMENT_PARTIAL`` - the new SL is live but cancelling the old one
  failed. Both order ids are recorded; the position has two stops, never
  zero. Permanently blocks further attempts (human/reconciliation review).
- ``POSITION_CLOSED_DURING_REPLACEMENT`` - the position went flat between
  verifying the new SL and cancelling the old one; neither order touched.
- ``POSITION_CLOSED_BEFORE_TIGHTENING`` - PP's ``POSITION_CLOSED_BEFORE_PP``
  under a mechanism-neutral name (the only renamed status; identical
  semantics: a restart-recovery resume found the position already flat
  before anything was placed).
- ``ANOMALY_NO_PROTECTIVE_ORDER_FOUND`` - the position is open but zero
  STOP_MARKET orders exist at all. Never auto-healed - this module never
  guesses a price and places a fresh stop.

--------------------------------------------------------------------------
Isolation
--------------------------------------------------------------------------
Like ``live_profit_protection.py``, this module is intentionally
standalone: it never imports ``live_execution.py``,
``position_closing.py``, the paper-side sizing module, or anything under
``crypto_trading/backtest/``. Where it needs the same deterministic-client-
order-id discipline and lookup-then-classify pattern those modules use,
that logic is replicated here, not imported. It also never changes
leverage and never touches a take-profit order.
"""

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

# Copied verbatim from live_profit_protection.py: any of these during a
# status lookup OR a cancel attempt means the true exchange outcome cannot
# be determined/confirmed right now. httpx.TransportError is included
# because BingXLiveTradingConnector's own tenacity retry re-raises it
# verbatim after exhausting retries. Never guess, never blind-retry a write
# whose outcome is unknown.
_UNKNOWN_OUTCOME_ERRORS = (ConnectorUnavailableError, LiveExecutionGuardError, httpx.TransportError)

_ACTIVE_SL_STATUSES = frozenset({"NEW", "PENDING"})

# Copied verbatim from live_profit_protection.py (its deep-review fix 5):
# how far entry_quantity (recorded locally at entry) may diverge from the
# exchange's own positionAmt (read fresh here) before this is treated as an
# unsafe under-sizing risk rather than ordinary Decimal-formatting noise.
_QUANTITY_MISMATCH_TOLERANCE = Decimal("0.005")


def _client_order_id(position_id: str) -> str:
    """Deterministic, restart-safe client order id - the same pattern
    live_execution.py and live_profit_protection.py use, replicated here
    (not imported) per this module's no-dependency rule. The "ga" suffix
    distinguishes a Guardian Authority tightening stop from LIVE Profit
    Protection's "pp" break-even stop and from live_execution.py's own
    "e"/"g"/"x" ids, so the two mechanisms can never collide on an id
    either - not just on a claim row."""
    return f"lv{position_id[:24]}ga"[:32]


def _lookup_new_sl_order(
    connector: BingXLiveTradingConnector, instrument: str, client_order_id: str
) -> dict | None:
    """Read-only lookup by the deterministic clientOrderID - the one and
    only way this module ever tries to learn the new SL's true state: look
    up, never resubmit blindly. Any error collapses to None here,
    deliberately identical to "order not found" - both mean "cannot
    determine the true state right now"."""
    try:
        return connector.get_order_by_client_order_id(instrument, client_order_id)
    except _UNKNOWN_OUTCOME_ERRORS:
        return None


def _classify_new_sl_state(order: dict | None) -> str:
    """Never guesses. Returns exactly one of:
    - "ACTIVE": exchange confirms status is NEW/PENDING - genuinely live.
    - "FILLED": exchange confirms status == "FILLED" - the position closed
      at/near the new stop during verification, a valid outcome, not a bug.
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
    """Never guesses. Returns a Decimal only for a genuinely parseable,
    strictly-positive value - None/""/"0"/a negative value/a non-numeric
    string/an unparseable type all return None. Used for the entry
    quantity (PP's deep-review fix 1), for the exchange's positionAmt, AND
    for both sides of the tightening invariant, where an unparseable
    current stop price must fail the check rather than pass it."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _sl_orders(open_orders: list[dict]) -> list[dict]:
    """The protective stop orders among all open orders. A take-profit (or
    any other) order type is never a candidate - it must never be mistaken
    for the SL, and therefore can never be cancelled by this module."""
    return [order for order in open_orders if order.get("type") == "STOP_MARKET"]


def _verify_tightening_invariant(
    repo: Repository,
    position_id: str,
    instrument: str,
    old_sl_order_id: str,
    current_sl_price: object,
    new_sl: Decimal,
    run_id: str,
    now: datetime,
) -> bool:
    """THE second enforcement layer (see module docstring). Returns True
    only when `new_sl` is a parseable, strictly-positive value that is
    STRICTLY GREATER than the freshly-observed `current_sl_price` of the
    stop order that is really live on the exchange right now. Otherwise it
    records ABORTED_INVALID_TIGHTENING and returns False - the caller then
    places nothing and cancels nothing.

    Deliberately takes the current price as a parameter rather than
    re-reading it: every call site passes a value it has just read from
    the exchange in the same breath, and the identified order is the exact
    order that would be cancelled, so the value compared against is
    guaranteed to be both fresh and the right one. It is never derived
    from this module's own stored row, from the position's recorded
    stop_loss, or from anything the caller supplied.

    This is the ONLY gate in front of place_stop_loss_order in this file."""
    target = _parse_positive_decimal(new_sl)
    current_sl = _parse_positive_decimal(current_sl_price)

    if target is None or current_sl is None or target <= current_sl:
        reason = (
            f"refusing to place a stop at {new_sl!r} against a current live stop of "
            f"{current_sl_price!r} (order {old_sl_order_id}): a tightening must be a "
            "parseable, strictly-positive price strictly greater than the stop that is "
            "really on the exchange right now"
        )
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ABORTED_INVALID_TIGHTENING", now, last_error=reason,
        )
        log_event(
            run_id, event="ga_live_sl_aborted_invalid_tightening", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            current_sl_price=str(current_sl_price), new_sl=str(new_sl),
            status="ABORTED_INVALID_TIGHTENING", severity="ERROR",
        )
        return False
    return True


def _handle_new_sl_filled(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    old_sl_order_id: str,
    run_id: str,
    now: datetime,
) -> None:
    """The new SL was found FILLED (price reached the new stop before - or
    during - verification), a valid, safe outcome of a market-triggered
    stop, not an error. Shared by _place_and_verify_new_sl's own FILLED
    branch and by restart-recovery Case B - both mean exactly the same
    thing: re-check real exchange position state rather than assume, and
    never cancel the old SL from here (it is likely already gone too,
    since the position is flat)."""
    still_open = connector.get_position(instrument)
    if still_open is None:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "SL_REPLACED", now,
            last_error="position closed during new SL verification (filled at/near the new stop)",
        )
        log_event(
            run_id, event="ga_live_sl_closed_during_verification", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, status="SL_REPLACED",
        )
    else:
        # Shouldn't happen (a FILLED stop implies the position went flat) -
        # treat conservatively as uncertain rather than guess, and log at a
        # level that surfaces the anomaly for human review.
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "UNCERTAIN_NEW_SL_STATUS", now,
            last_error="new SL reported FILLED but position is still open on the exchange",
        )
        log_event(
            run_id, event="ga_live_sl_new_sl_status_uncertain", position_id=position_id,
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
    """The "verified-active tail" (copied from PP, including its
    deep-review fixes 3 and 4): record the new SL, re-check the real
    exchange position immediately before cancelling the old SL, cancel it,
    and finalize. Reached by the fresh path's ACTIVE branch and by
    restart-recovery Cases B and C - in all of them the new SL is
    positively confirmed live and only the removal of the old one remains,
    so the remaining work is identical.

    This is the ONLY place in this file that ever cancels an order."""
    repo.update_guardian_authority_live_sl_action_new_sl(
        position_id, new_sl_order_id=new_sl_order_id, updated_at=now,
    )
    log_event(
        run_id, event="ga_live_sl_new_sl_verified", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
    )

    # Orphan-SL race guard: re-check the REAL exchange position immediately
    # before cancelling the old SL. Placement + verification can span a real
    # window (retries, network latency); if the position closed by some
    # other means (e.g. TP filled) during that window, cancelling the old SL
    # now would be a guess about an order that may no longer matter, made on
    # the unverified assumption that BingX safely rejects/no-ops stop orders
    # against a flat position. Touch neither order further; flag for
    # human/reconciliation review instead.
    still_open_before_cancel = connector.get_position(instrument)
    if still_open_before_cancel is None:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "POSITION_CLOSED_DURING_REPLACEMENT", now,
            last_error=(
                "position closed on the exchange between new SL verification and "
                "old SL cancellation; neither order was touched further"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_position_closed_before_cancel", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            new_sl_order_id=new_sl_order_id, status="POSITION_CLOSED_DURING_REPLACEMENT",
        )
        return

    # Cancel the old SL, now that the new one is confirmed active AND the
    # position is confirmed still open.
    try:
        connector.cancel_order(instrument, old_sl_order_id)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Both order IDs are already durably recorded above - the position
        # has >=1 real protective order at all times. No third order is ever
        # placed; this status permanently blocks further attempts for this
        # position (human/reconciliation review only).
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "REPLACEMENT_PARTIAL", now, last_error=str(exc),
        )
        log_event(
            run_id, event="ga_live_sl_replacement_partial", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            new_sl_order_id=new_sl_order_id, status="REPLACEMENT_PARTIAL", error=str(exc),
        )
        return

    # The cancel above is the last IRREVERSIBLE step - the replacement is
    # already a real, complete success at this point. Record that success
    # FIRST. The final-state read below is purely informational (for log
    # visibility); it must never be able to leave a genuinely successful
    # replacement recorded as anything other than SL_REPLACED if it fails.
    repo.set_guardian_authority_live_sl_action_status(position_id, "SL_REPLACED", now)
    log_event(
        run_id, event="ga_live_sl_replaced", position_id=position_id, instrument=instrument,
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id, status="SL_REPLACED",
    )
    try:
        remaining_orders = connector.get_open_orders(instrument)
        log_event(
            run_id, event="ga_live_sl_final_state_check", position_id=position_id,
            instrument=instrument,
            remaining_open_order_types=[order.get("type") for order in remaining_orders],
        )
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        log_event(
            run_id, event="ga_live_sl_final_state_check_failed", position_id=position_id,
            instrument=instrument, error_type=type(exc).__name__, error=str(exc),
        )


def _place_and_verify_new_sl(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    old_sl_order_id: str,
    current_sl_price: object,
    new_sl: Decimal,
    new_sl_client_order_id: str,
    position_amt: object,
    run_id: str,
    now: datetime,
) -> None:
    """Place the new SL at the caller-supplied `new_sl`, then verify it is
    genuinely active. EVERY placement path in this module funnels through
    here - the fresh path, restart-recovery Case A (via
    _run_claimed_sequence) and Case B's not-found-on-resume branch - which
    is precisely why the tightening invariant is re-verified as this
    function's very first act: there is no way to reach
    place_stop_loss_order without passing that gate, and `current_sl_price`
    is a value the caller has just read from the exchange.

    A retry after an interrupted attempt reuses the SAME deterministic
    client order id - that determinism is exactly what makes a
    lookup-first retry safe, never a newly-minted id."""
    if not _verify_tightening_invariant(
        repo, position_id, instrument, old_sl_order_id, current_sl_price, new_sl, run_id, now,
    ):
        return  # nothing placed, nothing cancelled, existing protection untouched

    # Quantity is sourced fresh from the repository (never a cached/stale
    # value) - the same entry_quantity recorded when this position's live
    # entry was confirmed.
    live_execution = repo.get_live_execution(position_id)
    raw_entry_quantity = (live_execution or {}).get("entry_quantity")
    entry_quantity = _parse_positive_decimal(raw_entry_quantity)

    # PP deep-review fix 1: a missing/zero/unparseable entry_quantity (a
    # real, reachable state - see live_execution.py's
    # _resolve_uncertain_entry) must abort HERE, before any placement -
    # never fall back to "0" and send a zero-quantity stop to a real
    # exchange. Old SL is untouched.
    if entry_quantity is None:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ABORTED_INVALID_ENTRY_QUANTITY", now,
            last_error=(
                "entry_quantity is missing/non-positive/unparseable: "
                f"{raw_entry_quantity!r}"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_aborted_invalid_entry_quantity", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="ABORTED_INVALID_ENTRY_QUANTITY",
        )
        return

    # PP deep-review fix 5: cross-check against the exchange's own,
    # freshly-read positionAmt (already in hand from the caller's own
    # get_position() call) - exchange state is the source of truth. A
    # stale/diverged local entry_quantity (e.g. after a partial close)
    # would under-size the replacement SL and leave part of a real position
    # unprotected once the old SL is cancelled - abort rather than guess.
    position_amt_decimal = _parse_positive_decimal(position_amt)
    quantity_mismatch = (
        position_amt_decimal is None
        or abs(entry_quantity - position_amt_decimal) / position_amt_decimal
        > _QUANTITY_MISMATCH_TOLERANCE
    )
    if quantity_mismatch:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ABORTED_QUANTITY_MISMATCH", now,
            last_error=(
                f"entry_quantity {entry_quantity} vs exchange positionAmt {position_amt!r} "
                f"exceeds tolerance {_QUANTITY_MISMATCH_TOLERANCE}"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_aborted_quantity_mismatch", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            entry_quantity=str(entry_quantity), position_amt=str(position_amt),
            status="ABORTED_QUANTITY_MISMATCH",
        )
        return

    try:
        connector.place_stop_loss_order(
            instrument, quantity=str(entry_quantity), stop_price=str(new_sl),
            client_order_id=new_sl_client_order_id,
        )
    except OrderRejectedError as exc:
        # Synchronous, structured rejection - zero fill guaranteed, nothing
        # to look up. Old SL was never touched.
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ABORTED_NEW_SL_REJECTED", now, last_error=str(exc),
        )
        log_event(
            run_id, event="ga_live_sl_new_sl_rejected", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id, new_sl=str(new_sl),
            status="ABORTED_NEW_SL_REJECTED", error=str(exc),
        )
        return

    # Verify the new SL is genuinely active before ever touching the old
    # one - together with the invariant gate above, the two most important
    # invariants in this file.
    new_sl_order = _lookup_new_sl_order(connector, instrument, new_sl_client_order_id)
    state = _classify_new_sl_state(new_sl_order)

    if state == "UNKNOWN":
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "UNCERTAIN_NEW_SL_STATUS", now,
            last_error=f"new SL status could not be determined (lookup result: {new_sl_order!r})",
        )
        log_event(
            run_id, event="ga_live_sl_new_sl_status_uncertain", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="UNCERTAIN_NEW_SL_STATUS",
        )
        return  # old SL is NEVER cancelled in this branch.

    if state == "FILLED":
        _handle_new_sl_filled(
            repo, connector, position_id, instrument, old_sl_order_id, run_id, now,
        )
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
    new_sl: Decimal,
    new_sl_client_order_id: str,
    position_amt: object,
    run_id: str,
    now: datetime,
) -> None:
    """The full replacement sequence, run immediately after a successful
    claim (and reused verbatim by restart-recovery Case A, which has
    exactly the same starting shape: no old SL identified yet).
    Add-before-remove throughout: no code path here ever cancels the old SL
    before the new SL has been positively confirmed NEW/PENDING."""
    # Identify exactly one existing protective SL. This read IS the "current
    # real stop loss, freshly from the exchange" that the tightening
    # invariant is then verified against - whichever mechanism (LIVE Profit
    # Protection, this module, or the original entry) placed it.
    open_orders = connector.get_open_orders(instrument)
    sl_orders = _sl_orders(open_orders)
    if len(sl_orders) != 1:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ABORTED_AMBIGUOUS_SL", now,
            last_error=f"found {len(sl_orders)} STOP_MARKET orders, expected exactly 1",
        )
        log_event(
            run_id, event="ga_live_sl_aborted_ambiguous_sl", position_id=position_id,
            instrument=instrument, sl_order_count=len(sl_orders), status="ABORTED_AMBIGUOUS_SL",
        )
        return

    old_sl = sl_orders[0]
    old_sl_order_id = str(old_sl.get("orderId"))
    old_sl_price = str(old_sl.get("stopPrice"))
    repo.update_guardian_authority_live_sl_action_old_sl(
        position_id, old_sl_order_id=old_sl_order_id, old_sl_price=old_sl_price, updated_at=now,
    )
    log_event(
        run_id, event="ga_live_sl_old_sl_identified", position_id=position_id,
        instrument=instrument, old_sl_order_id=old_sl_order_id, old_sl_price=old_sl_price,
    )

    _place_and_verify_new_sl(
        repo, connector, position_id, instrument, old_sl_order_id, old_sl.get("stopPrice"),
        new_sl, new_sl_client_order_id, position_amt, run_id, now,
    )


def _stored_target_or_abort(
    repo: Repository,
    row: dict,
    instrument: str,
    recovery_case: str,
    run_id: str,
    now: datetime,
) -> Decimal | None:
    """Parses the target price stored on a CLAIMED row, for the two
    recovery cases that can still place an order. Returns None - having
    recorded ABORTED_INVALID_TIGHTENING - if the stored value is not a
    parseable, strictly-positive price.

    A stored target can only ever have been written by this module's own
    claim (which parses it first), so this is pure defense in depth. It
    exists because the alternative - letting Decimal() raise out of the
    recovery pass - would leave the row permanently CLAIMED, retried and
    failing identically on every later pass: a stuck row, which restart
    recovery exists precisely to prevent. Resolving it to a terminal
    refusal keeps the guarantee that a CLAIMED row is always resolved
    forward, and it refuses in the safe direction (nothing is placed)."""
    target = _parse_positive_decimal(row["new_sl_price"])
    if target is None:
        position_id = row["position_id"]
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ABORTED_INVALID_TIGHTENING", now,
            last_error=(
                f"restart recovery (Case {recovery_case}): the stored new_sl_price "
                f"{row['new_sl_price']!r} is not a parseable, strictly-positive price; "
                "nothing was placed"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_aborted_invalid_tightening", position_id=position_id,
            instrument=instrument, stored_new_sl_price=str(row["new_sl_price"]),
            status="ABORTED_INVALID_TIGHTENING", severity="ERROR", recovery_case=recovery_case,
        )
    return target


def _recover_case_a(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    row: dict,
    instrument: str,
    run_id: str,
    now: datetime,
) -> None:
    """Case A: old_sl_order_id is None on the row - the crash happened
    before the old SL was ever identified, so nothing was ever placed on
    the exchange for this attempt. This is exactly the situation
    _run_claimed_sequence already handles correctly from scratch (no old SL
    was identified yet, so re-running identification onward carries zero
    risk of misidentifying which SL is "old"), including re-verifying the
    tightening invariant against whatever stop is live NOW - which may
    differ from the one that was live when this row was claimed. The row's
    already-stored new_sl_price/new_sl_client_order_id are reused verbatim."""
    position_id = row["position_id"]
    live_position = connector.get_position(instrument)
    if live_position is None:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "POSITION_CLOSED_BEFORE_TIGHTENING", now,
            last_error=(
                "restart recovery (Case A): position already closed; no old SL had been "
                "identified"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_recovery_position_closed_before_tightening",
            position_id=position_id, instrument=instrument,
            status="POSITION_CLOSED_BEFORE_TIGHTENING", recovery_case="A",
        )
        return

    new_sl = _stored_target_or_abort(repo, row, instrument, "A", run_id, now)
    if new_sl is None:
        return

    position_amt = live_position.get("positionAmt", "0")
    _run_claimed_sequence(
        repo, connector, position_id, instrument, new_sl,
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
    row - the old SL was identified, but the crash happened somewhere in
    placement/verification, before, during, or after placing the new SL,
    and before it was confirmed ACTIVE. The new SL's true state is looked
    up by its deterministic client order id (never a freshly-generated
    one) - exactly what makes this safe."""
    position_id = row["position_id"]
    live_position = connector.get_position(instrument)
    if live_position is None:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "POSITION_CLOSED_BEFORE_TIGHTENING", now,
            last_error=(
                "restart recovery (Case B): position already closed; new SL was never "
                "confirmed placed"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_recovery_position_closed_before_tightening",
            position_id=position_id, instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="POSITION_CLOSED_BEFORE_TIGHTENING", recovery_case="B",
        )
        return

    position_amt = live_position.get("positionAmt", "0")

    # PP's Case B fix 2: the old SL can be gone by the time this recovery
    # path runs (externally cancelled, or triggered/removed during the exact
    # downtime this path exists to handle) with the new SL never placed
    # either - zero STOP_MARKET orders at all while the position is still
    # open. Detect this before ever considering a placement or relying on a
    # later cancel_order against an order whose existence was never verified
    # in this pass.
    try:
        open_orders = connector.get_open_orders(instrument)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Cannot determine which orders remain right now - do not guess.
        # The row stays CLAIMED; a later recovery pass will retry once the
        # exchange is reachable again.
        log_event(
            run_id, event="ga_live_sl_recovery_open_orders_lookup_failed", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            error_type=type(exc).__name__, error=str(exc), recovery_case="B",
        )
        return

    sl_orders = _sl_orders(open_orders)
    if not sl_orders:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ANOMALY_NO_PROTECTIVE_ORDER_FOUND", now,
            last_error=(
                "restart recovery (Case B): position still open but zero STOP_MARKET "
                "orders exist at all (neither old nor a new one was ever confirmed placed)"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_anomaly_no_protective_order_found", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            status="ANOMALY_NO_PROTECTIVE_ORDER_FOUND", severity="ERROR", recovery_case="B",
        )
        return

    new_sl_client_order_id = row["new_sl_client_order_id"]
    # PP's Case B fix 3: a genuine lookup error must NEVER be collapsed into
    # "not found" here - that would silently downgrade an unknown outcome
    # into a real placement attempt. Deliberately does NOT use
    # _lookup_new_sl_order (which swallows every _UNKNOWN_OUTCOME_ERRORS
    # into None, identical to "not found") - the raise is caught here,
    # explicitly, so it can be handled differently.
    try:
        new_sl_order = connector.get_order_by_client_order_id(instrument, new_sl_client_order_id)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Leave the row CLAIMED; a later recovery pass will retry the lookup
        # once the exchange is reachable again. Never guess.
        log_event(
            run_id, event="ga_live_sl_recovery_new_sl_lookup_failed", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            error_type=type(exc).__name__, error=str(exc), recovery_case="B",
        )
        return

    state = _classify_new_sl_state(new_sl_order)

    if state == "ACTIVE":
        # The new SL WAS placed successfully before the crash - resume from
        # right after verification. No new order is placed, so the
        # tightening invariant (a gate on PLACEMENT) is not re-run: the
        # order whose price it would govern already exists and was already
        # gated before it was placed.
        new_sl_order_id = str(new_sl_order.get("orderId"))  # type: ignore[union-attr]
        _finalize_verified_active_new_sl(
            repo, connector, position_id, instrument, new_sl_order_id, old_sl_order_id, run_id, now,
        )
        return

    if state == "FILLED":
        _handle_new_sl_filled(
            repo, connector, position_id, instrument, old_sl_order_id, run_id, now,
        )
        return

    # state == "UNKNOWN" here means the lookup call itself SUCCEEDED (a
    # genuine error already returned above, never reaching this line) and
    # returned either no order at all or an unrecognized status - a real,
    # determinate "not found" response. The new SL was never confirmed
    # placed, so a placement retry is on the table - which means the
    # tightening invariant must be re-verified against the stop that is
    # live NOW, not the price stored on the row when it was claimed.
    #
    # Stricter than PP's own Case B (which only counts the STOP_MARKET
    # orders): the retry is allowed ONLY when the freshly-read protective
    # orders are exactly the one order this row recorded as the old SL.
    # Anything else - a different id (something replaced our old SL during
    # the downtime, e.g. LIVE Profit Protection), or several orders - means
    # the current SL cannot be unambiguously identified with the one this
    # attempt is replacing, and this module must not place an order against
    # an ambiguous picture.
    live_old_sl = next(
        (order for order in sl_orders if str(order.get("orderId")) == old_sl_order_id), None
    )
    if live_old_sl is None or len(sl_orders) != 1:
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ABORTED_AMBIGUOUS_SL", now,
            last_error=(
                "restart recovery (Case B): the new SL was never confirmed placed, but the "
                f"freshly-read STOP_MARKET orders ({[str(o.get('orderId')) for o in sl_orders]}) "
                f"are not exactly the recorded old SL ({old_sl_order_id}) - the current stop "
                "cannot be unambiguously identified, so nothing is placed"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_aborted_ambiguous_sl", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            sl_order_count=len(sl_orders), status="ABORTED_AMBIGUOUS_SL", recovery_case="B",
        )
        return

    # Re-record the freshly-observed stop price (it can have been amended
    # since the claim) so the row always reflects what was actually seen.
    live_old_sl_price = str(live_old_sl.get("stopPrice"))
    repo.update_guardian_authority_live_sl_action_old_sl(
        position_id, old_sl_order_id=old_sl_order_id, old_sl_price=live_old_sl_price,
        updated_at=now,
    )

    new_sl = _stored_target_or_abort(repo, row, instrument, "B", run_id, now)
    if new_sl is None:
        return

    _place_and_verify_new_sl(
        repo, connector, position_id, instrument, old_sl_order_id, live_old_sl.get("stopPrice"),
        new_sl, new_sl_client_order_id, position_amt, run_id, now,
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
    """Case C: new_sl_order_id is set on the row - the new SL was confirmed
    ACTIVE before the crash; only the pre-cancel position re-check, the
    cancel itself, or the final status write didn't complete. No placement
    can happen from here at all, so the tightening invariant has nothing
    left to gate: the order it governs already exists and was gated before
    it was placed."""
    position_id = row["position_id"]
    live_position = connector.get_position(instrument)
    if live_position is None:
        # Both orders are already durably recorded. Do not touch either
        # order without a verified need - the position is already flat, so
        # there is nothing left to protect, and touching orders on a flat
        # position now would be exactly the "guess based on an unverified
        # assumption" the design forbids.
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "POSITION_CLOSED_DURING_REPLACEMENT", now,
            last_error=(
                "restart recovery (Case C): position already closed; both orders were "
                "already recorded, neither touched"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_recovery_position_closed_during_replacement",
            position_id=position_id, instrument=instrument, old_sl_order_id=old_sl_order_id,
            new_sl_order_id=new_sl_order_id, status="POSITION_CLOSED_DURING_REPLACEMENT",
            recovery_case="C",
        )
        return

    try:
        open_orders = connector.get_open_orders(instrument)
    except _UNKNOWN_OUTCOME_ERRORS as exc:
        # Cannot determine which orders remain right now - do not guess.
        # The row stays CLAIMED; a later recovery pass will retry.
        log_event(
            run_id, event="ga_live_sl_recovery_open_orders_lookup_failed", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            new_sl_order_id=new_sl_order_id, error_type=type(exc).__name__, error=str(exc),
            recovery_case="C",
        )
        return

    sl_order_ids = {str(order.get("orderId")) for order in _sl_orders(open_orders)}
    old_present = old_sl_order_id in sl_order_ids
    new_present = new_sl_order_id in sl_order_ids

    if not old_present and not new_present:
        # Neither of OUR OWN recorded protective orders is present - an
        # anomaly this codebase's own logic should never itself produce
        # (add-before-remove guarantees >=1 protective order at every
        # self-caused transition), regardless of any other unrelated orders
        # that might exist on the instrument. Do NOT auto-heal (do not guess
        # a price and place a fresh SL).
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "ANOMALY_NO_PROTECTIVE_ORDER_FOUND", now,
            last_error=(
                "restart recovery (Case C): position still open but neither the recorded "
                "old nor new SL order is present among open STOP_MARKET orders"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_anomaly_no_protective_order_found", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            new_sl_order_id=new_sl_order_id, status="ANOMALY_NO_PROTECTIVE_ORDER_FOUND",
            severity="ERROR", recovery_case="C",
        )
        return

    if not old_present:
        # new_present is True here (the both-absent case returned above) -
        # the old SL is already gone: the operation had actually already
        # fully succeeded before the crash; only the final status write
        # never landed.
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "SL_REPLACED", now,
            last_error="old SL already cancelled before restart; only the status write was missing",
        )
        log_event(
            run_id, event="ga_live_sl_replaced", position_id=position_id, instrument=instrument,
            old_sl_order_id=old_sl_order_id, new_sl_order_id=new_sl_order_id,
            status="SL_REPLACED", recovered=True, recovery_case="C",
        )
        return

    if not new_present:
        # PP's CRITICAL Case C fix, copied verbatim in shape: old_sl_order_id
        # is present, but the row's OWN recorded new_sl_order_id is NOT among
        # the freshly-read open orders (e.g. externally cancelled, or BingX
        # auto-cancelling a duplicate STOP_MARKET - exactly the exchange
        # behavior the design refuses to assume). The cancel gate must be
        # identity-based, never presence/count-based: cancelling on this
        # evidence alone would risk leaving a real, still-open position with
        # ZERO stops recorded as a permanent SL_REPLACED success. Resolve the
        # new SL's actual state the same way Case B does - via its
        # deterministic client order id - and branch from there. The old SL
        # is NEVER cancelled purely because it happens to still be present.
        new_sl_order = _lookup_new_sl_order(connector, instrument, row["new_sl_client_order_id"])
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
            _handle_new_sl_filled(
                repo, connector, position_id, instrument, old_sl_order_id, run_id, now,
            )
            return

        # state == "UNKNOWN" (not found / lookup failed / unrecognized
        # status): a new SL our own row claims was already confirmed ACTIVE
        # can no longer be positively verified, while the old SL is still
        # there (still protected, add-before-remove was never violated).
        # Never blindly retry a write here - block for human/reconciliation
        # review instead.
        repo.set_guardian_authority_live_sl_action_status(
            position_id, "UNCERTAIN_NEW_SL_STATUS", now,
            last_error=(
                "restart recovery (Case C): recorded new SL is missing from open orders and "
                f"could not be re-confirmed by lookup (result: {new_sl_order!r}); old SL "
                "left untouched"
            ),
        )
        log_event(
            run_id, event="ga_live_sl_new_sl_status_uncertain", position_id=position_id,
            instrument=instrument, old_sl_order_id=old_sl_order_id,
            new_sl_order_id=new_sl_order_id, status="UNCERTAIN_NEW_SL_STATUS", recovery_case="C",
        )
        return

    # Both old_present and new_present are freshly, positively confirmed -
    # the ONLY safe cancel branch. Routed through the SAME shared
    # verified-active tail the fresh path and Case B's ACTIVE branch use,
    # rather than inlining another cancel here: that tail's pre-cancel
    # get_position re-check is exactly what closes the window between Case
    # C's own early position-liveness check above and the actual cancel.
    _finalize_verified_active_new_sl(
        repo, connector, position_id, instrument, new_sl_order_id, old_sl_order_id, run_id, now,
    )


def _recover_claimed_position(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    row: dict,
    run_id: str,
    now: datetime,
) -> None:
    """Resolves one CLAIMED guardian_authority_live_sl_actions row left over
    from an interrupted prior attempt (crash/restart). Always re-derives
    truth from the exchange - never assumes, never blindly resumes
    mid-recipe. Exactly one of three cases applies, determined solely by
    which fields are already populated on the row: the sequence this module
    runs is strictly single-threaded and sequential, so old_sl_order_id can
    only ever be set before the new SL placement was attempted, and
    new_sl_order_id can only ever be set after the new SL was confirmed
    ACTIVE (which is always after old_sl_order_id was already set)."""
    position_id = row["position_id"]
    position_record = repo.get_position(position_id)
    if position_record is None:
        return  # defensive: a claimed row always has a positions row
    instrument = position_record.instrument

    old_sl_order_id = row.get("old_sl_order_id")
    new_sl_order_id = row.get("new_sl_order_id")

    if old_sl_order_id is None:
        _recover_case_a(repo, connector, row, instrument, run_id, now)
    elif new_sl_order_id is None:
        _recover_case_b(repo, connector, row, instrument, old_sl_order_id, run_id, now)
    else:
        _recover_case_c(
            repo, connector, row, instrument, old_sl_order_id, new_sl_order_id, run_id, now,
        )


def recover_claimed_live_sl_tightenings(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    run_id: str,
    now: datetime,
) -> None:
    """Restart/crash recovery pass. Every row still CLAIMED from an
    interrupted prior attempt is resolved forward by re-deriving truth from
    the exchange. MUST be run before any new apply_live_sl_tightening call
    in the same tick: apply_live_sl_tightening deliberately refuses to touch
    a position that already has a row (of any status), so this pass is the
    only thing that can move a CLAIMED row forward.

    Same per-position isolation discipline as live_profit_protection.py's
    own tick: one bad recovery must never block recovering any other
    position - the failing row simply stays CLAIMED for the next pass."""
    for row in repo.find_claimed_guardian_authority_live_sl_actions():
        position_id = row["position_id"]
        try:
            _recover_claimed_position(repo, connector, row, run_id, now)
        except Exception as exc:  # noqa: BLE001 - one bad recovery must never block the batch
            log_event(
                run_id, event="ga_live_sl_recovery_error", position_id=position_id,
                error_type=type(exc).__name__, error=str(exc),
            )
            continue


def apply_live_sl_tightening(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position_id: str,
    instrument: str,
    new_sl: Decimal,
    run_id: str,
    now: datetime,
) -> None:
    """Tighten ONE live position's real stop loss to `new_sl`, using the
    add-before-remove replacement sequence (see module docstring).

    Idempotency/claim isolation: the position's row in
    guardian_authority_live_sl_actions is the gate. A position that already
    has a row - CLAIMED or terminal - is never touched here at all (the
    connector is not even contacted); resolving a CLAIMED row is
    recover_claimed_live_sl_tightenings' job, and a terminal row means this
    position's one tightening attempt is already finished. The claim itself
    is an INSERT OR IGNORE on the position_id primary key, so two concurrent
    observations can never both proceed.

    No row is created at all - and the exchange is never contacted - for a
    non-positive/unparseable `new_sl` (a caller bug must not permanently
    consume this position's single claim), and no row is created for a
    position whose real exchange counterpart is already gone.

    Exceptions are NOT swallowed here: this function handles exactly one
    position, so the caller owns batch isolation and must wrap each
    position's call in its own try/except, exactly as
    live_profit_protection.py's tick wraps its own per-position work.
    """
    existing = repo.get_guardian_authority_live_sl_action(position_id)
    if existing is not None:
        log_event(
            run_id, event="ga_live_sl_already_has_row", position_id=position_id,
            instrument=instrument, existing_status=existing["status"],
        )
        return

    target = _parse_positive_decimal(new_sl)
    if target is None:
        # Refuse before the claim and before any exchange call: this is a
        # caller-side error, not an observed exchange state, and it must not
        # consume the position's one claim row. Nothing is written.
        log_event(
            run_id, event="ga_live_sl_refused_invalid_target", position_id=position_id,
            instrument=instrument, new_sl=str(new_sl), severity="ERROR",
        )
        return

    live_position = connector.get_position(instrument)
    if live_position is None:
        log_event(
            run_id, event="ga_live_sl_position_gone_before_claim", position_id=position_id,
            instrument=instrument,
        )
        return  # real exchange counterpart already gone - nothing to protect, no row created

    position_amt = live_position.get("positionAmt", "0")
    new_sl_client_order_id = _client_order_id(position_id)
    claimed = repo.claim_guardian_authority_live_sl_action(
        position_id,
        new_sl_price=str(target),
        new_sl_client_order_id=new_sl_client_order_id,
        claimed_at=now,
    )
    if not claimed:
        return  # race: another observation already claimed it (the DB is the race defense)

    log_event(
        run_id, event="ga_live_sl_claimed", position_id=position_id, instrument=instrument,
        new_sl=str(target),
    )
    _run_claimed_sequence(
        repo, connector, position_id, instrument, target, new_sl_client_order_id,
        position_amt, run_id, now,
    )
