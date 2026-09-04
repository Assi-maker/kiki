from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from crypto_trading.connectors.bingx_demo_trading import (
    BingXDemoTradingConnector,
    DemoExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.monitoring import compute_hold_hours
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_GUARDED_ERRORS = (ConnectorUnavailableError, DemoExecutionGuardError)


def _client_order_id(position_id: str, suffix: str) -> str:
    return f"pt{position_id[:24]}{suffix}"[:32]


def _quantity_for(position: Position, quantity_precision: int) -> str:
    raw_quantity = position.size / position.simulated_fill_entry
    quantum = Decimal(1).scaleb(-quantity_precision)
    return str(raw_quantity.quantize(quantum, rounding=ROUND_DOWN))


def _submit_entry_order(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    position: Position,
    quantity_precision_by_symbol: dict[str, int],
    run_id: str,
    now: datetime,
) -> None:
    client_order_id = _client_order_id(position.position_id, "e")
    try:
        precision = quantity_precision_by_symbol.get(position.instrument, 0)
        quantity = _quantity_for(position, precision)
        connector.set_leverage(position.instrument, leverage=1)
        result = connector.place_entry_order_with_sl_tp(
            symbol=position.instrument,
            quantity=quantity,
            client_order_id=client_order_id,
            stop_loss_price=str(position.stop_loss),
            target_price=str(position.target),
        )
        repo.update_demo_execution_submitted(
            position.position_id,
            entry_client_order_id=client_order_id,
            entry_exchange_order_id=str(result.get("orderId", "")),
            entry_quantity=quantity,
            exchange_fill_entry=str(result.get("avgPrice", "")),
            # BingX exposes no independently queryable order id for the
            # attached stopLoss/takeProfit legs (confirmed live 2026-09-04 -
            # they come back as a descriptive JSON string/empty sub-object,
            # never an order id to poll). Reconciliation instead watches the
            # position itself go flat (see reconcile_active_executions()).
            sl_exchange_order_id=None,
            tp_exchange_order_id=None,
            updated_at=now,
        )
        log_event(
            run_id, event="demo_order_submitted", position_id=position.position_id,
            instrument=position.instrument, exchange_order_id=str(result.get("orderId", "")),
        )
    except _GUARDED_ERRORS as exc:
        repo.mark_demo_execution_failed(position.position_id, f"{type(exc).__name__}: {exc}", now)
        log_event(
            run_id, event="demo_order_failed", position_id=position.position_id,
            error_type=type(exc).__name__, error=str(exc),
        )


def process_pending_positions(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    quantity_precision_by_symbol: dict[str, int],
    run_id: str,
    now: datetime,
    limit: int = 10,
) -> None:
    """Claim-before-place: repo.claim_demo_execution() is an atomic INSERT
    OR IGNORE keyed on position_id. A False return means another
    run/duplicate observation already claimed this position - skip it,
    never place a second order (SPEC amendment / design doc §8)."""
    for position in repo.find_positions_pending_demo_execution(limit):
        if not repo.claim_demo_execution(position.position_id, now):
            continue
        _submit_entry_order(repo, connector, position, quantity_precision_by_symbol, run_id, now)


def recover_stale_claims(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    quantity_precision_by_symbol: dict[str, int],
    run_id: str,
    now: datetime,
    stale_after_seconds: int,
) -> None:
    """Crash recovery: a row stuck in CLAIMED past the grace window means
    the process died between claiming and confirming submission. Look the
    order up by its deterministic clientOrderID BEFORE ever resubmitting -
    never a blind retry (design doc §8)."""
    stale_before = now - timedelta(seconds=stale_after_seconds)
    for row in repo.find_stale_claimed_demo_executions(stale_before):
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        client_order_id = _client_order_id(position.position_id, "e")
        existing = connector.get_order_by_client_order_id(position.instrument, client_order_id)
        if existing is not None:
            repo.update_demo_execution_submitted(
                position.position_id,
                entry_client_order_id=client_order_id,
                entry_exchange_order_id=str(existing.get("orderId", "")),
                entry_quantity=str(existing.get("origQty", "")),
                exchange_fill_entry=str(existing.get("avgPrice", "")),
                sl_exchange_order_id=None,
                tp_exchange_order_id=None,
                updated_at=now,
            )
            continue
        _submit_entry_order(repo, connector, position, quantity_precision_by_symbol, run_id, now)


def _classify_exit_reason(position: Position, exit_price: Decimal) -> str:
    """Best-effort classification, not a fact BingX hands us directly: once
    the position is confirmed flat (reconcile_active_executions()), we know
    *that* it closed but not natively *why*. Compares the observed exit
    price's distance to our own recorded stop_loss/target - the same
    conservative "approximate from available data, always honestly labeled"
    approach the rest of this codebase already uses for PAPER (design doc
    §12/§13)."""
    distance_to_stop = abs(exit_price - position.stop_loss)
    distance_to_target = abs(exit_price - position.target)
    return "stop_loss" if distance_to_stop <= distance_to_target else "target"


def reconcile_active_executions(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    market_data_connector: object,
    run_id: str,
    now: datetime,
) -> None:
    """BingX exposes no independently queryable order id for the attached
    stopLoss/takeProfit legs (confirmed live 2026-09-04), so this cannot
    poll a leg's order status the way the original design assumed. Instead
    it watches the position itself: BingX's own matching engine closes it
    independent of whether this process is running, and once
    `connector.get_position()` reports it flat, that alone proves the
    exchange closed it (this loop never causes the close itself - the only
    other closer is close_time_limit_positions(), and a row already
    CLOSED/FAILED is filtered out by find_active_demo_executions())."""
    for row in repo.find_active_demo_executions():
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        if connector.get_position(position.instrument) is not None:
            continue  # still open on the exchange
        exit_price = Decimal(str(market_data_connector.get_ticker(position.instrument)["lastPrice"]))
        exit_reason = _classify_exit_reason(position, exit_price)
        repo.close_demo_execution(position.position_id, exit_reason, str(exit_price), now)
        log_event(
            run_id, event="demo_position_closed", position_id=position.position_id,
            exit_reason=exit_reason,
        )


def close_time_limit_positions(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    max_position_hold_hours: int,
    run_id: str,
    now: datetime,
) -> None:
    """BingX has no server-side time-based close; PAPER does. Reuses the
    exact same compute_hold_hours() PAPER's check_exit_trigger() uses, so
    the two systems never disagree on when the limit is reached (design doc
    §10). Only ever writes to demo_executions - never touches `positions`."""
    for row in repo.find_active_demo_executions():
        position = repo.get_position(row["position_id"])
        if position is None or position.status != "OPEN_POSITION":
            continue
        if compute_hold_hours(position, now) < max_position_hold_hours:
            continue
        try:
            connector.cancel_all_open_orders(position.instrument)
            client_order_id = _client_order_id(position.position_id, "x")
            result = connector.close_position_market(
                position.instrument,
                quantity=row.get("entry_quantity") or "0",
                client_order_id=client_order_id,
            )
            repo.close_demo_execution(
                position.position_id, "TIME_LIMIT", str(result.get("avgPrice", "")), now
            )
            log_event(run_id, event="demo_time_limit_closed", position_id=position.position_id)
        except _GUARDED_ERRORS as exc:
            log_event(
                run_id, event="demo_time_limit_close_failed", position_id=position.position_id,
                error_type=type(exc).__name__, error=str(exc),
            )
