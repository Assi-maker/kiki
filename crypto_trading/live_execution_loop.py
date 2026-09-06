from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_live_trading import BingXLiveTradingConnector
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.paper_trading.live_execution import (
    close_guardian_exit_positions,
    close_time_limit_positions,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.storage.repository import Repository


def run_live_execution_tick(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    quantity_precision_by_symbol: dict[str, int],
    min_notional_by_symbol: dict[str, Decimal],
    settings: Settings,
    now: datetime,
) -> None:
    """One live-execution tick. Reconciliation-first ordering (spec §7,
    the reverse of demo_execution_loop's order): recover_stale_claims ->
    reconcile_active_executions -> close_guardian_exit_positions ->
    close_time_limit_positions -> process_pending_positions LAST, so any
    new claim's capacity check already reflects this tick's own fresh
    reconciliation. Same outer fail-safe principle as every other loop in
    this codebase: an unexpected exception never crashes run_forever()."""
    run_id = new_run_id()
    repo.start_run(run_id, "live_execution", now)
    try:
        recover_stale_claims(
            repo, connector, quantity_precision_by_symbol, min_notional_by_symbol, settings,
            run_id, now, stale_after_seconds=settings.live_execution.claim_stale_after_seconds,
        )
        reconcile_active_executions(repo, connector, market_data_connector, run_id, now)
        close_guardian_exit_positions(repo, connector, run_id, now)
        close_time_limit_positions(
            repo, connector, settings.live_execution.max_position_hold_hours, run_id, now
        )
        process_pending_positions(
            repo, connector, market_data_connector, quantity_precision_by_symbol,
            min_notional_by_symbol, settings, run_id, now,
        )
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
    except Exception as exc:
        log_event(
            run_id, event="live_execution_tick_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])


def run_forever(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    quantity_precision_by_symbol: dict[str, int],
    min_notional_by_symbol: dict[str, Decimal],
    settings: Settings,
) -> None:
    while True:
        run_live_execution_tick(
            repo, connector, market_data_connector, quantity_precision_by_symbol,
            min_notional_by_symbol, settings, datetime.now(UTC),
        )
        time.sleep(settings.live_execution.check_interval_seconds)
