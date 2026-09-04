from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.detective.batch import run_detective_batch
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.storage.repository import Repository


def run_detective_tick(repo: Repository, runner: AgentRunner, settings: Settings) -> None:
    """En Detective-tick (Post-Trade Analyst, separat från discovery/
    monitoring): kollar om tillräckligt många nya stängda PAPER-trades
    väntar (se detective/batch.py::run_detective_batch()) och kör i så fall
    EN batchanalys. Helt frikopplad från realtidsbeslut - anropar aldrig
    Orchestrator/Gate/paper_trading.position_opening/position_closing, och
    kan därför aldrig påverka CONFIRMED/NO_TRADE/REJECTED eller en positions
    öppning/stängning (se .claude/agents/crypto-detective.md/schemas/
    detective.py för den fulla gränsen).

    Samma yttre fail-safe-princip som discovery_loop.run_discovery_tick()/
    monitoring_loop.run_monitoring_tick(): ett oväntat undantag kraschar
    aldrig run_forever(), loggas och skrivs till runs.errors istället."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "detective", now)
    try:
        run_detective_batch(repo, runner, settings, run_id, now)
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
    except Exception as exc:
        log_event(
            run_id, event="detective_tick_failed", error_type=type(exc).__name__, error=str(exc)
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])


def run_forever(repo: Repository, runner: AgentRunner, settings: Settings) -> None:
    while True:
        run_detective_tick(repo, runner, settings)
        time.sleep(settings.detective.check_interval_seconds)
