from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_demo_trading import BingXDemoTradingConnector
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.paper_trading.demo_execution import (
    close_time_limit_positions,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.storage.repository import Repository


def run_demo_execution_tick(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    market_data_connector: BingXMarketDataConnector,
    quantity_precision_by_symbol: dict[str, int],
    settings: Settings,
    now: datetime,
) -> None:
    """One demo-execution tick. Same outer fail-safe principle as
    discovery_loop.run_discovery_tick()/monitoring_loop.run_monitoring_tick():
    an unexpected exception never crashes run_forever()."""
    run_id = new_run_id()
    repo.start_run(run_id, "demo_execution", now)
    try:
        process_pending_positions(repo, connector, quantity_precision_by_symbol, run_id, now)
        recover_stale_claims(
            repo, connector, quantity_precision_by_symbol, run_id, now,
            stale_after_seconds=settings.demo_execution.claim_stale_after_seconds,
        )
        reconcile_active_executions(repo, connector, market_data_connector, run_id, now)
        close_time_limit_positions(
            repo, connector, settings.risk_limits.max_position_hold_hours, run_id, now
        )
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
    except Exception as exc:
        log_event(
            run_id, event="demo_execution_tick_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])


def run_forever(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    market_data_connector: BingXMarketDataConnector,
    quantity_precision_by_symbol: dict[str, int],
    settings: Settings,
) -> None:
    while True:
        run_demo_execution_tick(
            repo, connector, market_data_connector, quantity_precision_by_symbol, settings,
            datetime.now(UTC),
        )
        time.sleep(settings.demo_execution.check_interval_seconds)
