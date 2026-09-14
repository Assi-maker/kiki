from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_live_trading import BingXLiveTradingConnector
from crypto_trading.guardian.tick import run_guardian_tick_body
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.storage.repository import Repository


def run_guardian_tick(
    repo: Repository,
    connector,
    runner: AgentRunner,
    settings: Settings,
    now: datetime,
    live_connector: BingXLiveTradingConnector | None = None,
) -> None:
    """One Guardian tick. Same outer fail-safe shape as monitoring_loop.py/
    demo_execution_loop.py: an unexpected exception never crashes
    run_forever()."""
    run_id = new_run_id()
    repo.start_run(run_id, "guardian", now)
    try:
        run_guardian_tick_body(repo, connector, runner, settings, run_id, now, live_connector)
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
    except Exception as exc:
        log_event(
            run_id, event="guardian_tick_failed", error_type=type(exc).__name__, error=str(exc)
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])


def run_forever(
    repo: Repository,
    connector,
    runner: AgentRunner,
    settings: Settings,
    live_connector: BingXLiveTradingConnector | None = None,
) -> None:
    while True:
        run_guardian_tick(repo, connector, runner, settings, datetime.now(UTC), live_connector)
        time.sleep(settings.guardian.check_interval_seconds)
