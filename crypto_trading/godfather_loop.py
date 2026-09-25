"""Background loop for the GODFATHER Intelligence Layer.

Same shape as `detective_loop.py`: its own thread, its own run rows, and
an outer fail-safe so an unexpected exception is logged and recorded in
`runs.errors` instead of killing the process.

It is separate from the Detective thread on purpose even though both
analyse closed trades. Detective produces narrative observations with an
LLM and is budget-bound by AI cost; this tick is entirely deterministic,
makes no AI calls at all, and therefore has no cost ceiling to respect
and no reason to be throttled by one. Sharing a thread would couple the
two and let an AI-budget stall block the deterministic analysis that the
rest of the layer depends on.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.config.loader import Settings
from crypto_trading.godfather.pipeline import run_godfather_intelligence_tick
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.storage.repository import Repository


def run_godfather_tick(repo: Repository, settings: Settings) -> dict:
    """One analysis tick. Never raises - a failure here must never be
    able to disturb discovery, monitoring, Guardian or execution, which
    are the threads that actually handle money."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "godfather_intelligence", now)
    try:
        summary = run_godfather_intelligence_tick(
            repo,
            settings,
            now,
            run_id,
            batch_limit=settings.godfather.intelligence_batch_limit,
        )
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
        return summary
    except Exception as exc:
        log_event(
            run_id,
            event="godfather_intelligence_tick_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        repo.complete_run(
            run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"]
        )
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_forever(repo: Repository, settings: Settings) -> None:
    while True:
        run_godfather_tick(repo, settings)
        time.sleep(settings.godfather.intelligence_check_interval_seconds)
