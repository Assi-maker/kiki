from __future__ import annotations

from datetime import datetime

from crypto_trading.config.loader import RiskLimitsConfig, Settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.guardian.authority import maybe_open_position_for_candidate
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.position_opening import open_position_for_candidate
from crypto_trading.schemas.market import Ticker
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository


def sweep_confirmed_candidates_without_position(
    repo: Repository,
    connector: object,
    risk_limits: RiskLimitsConfig,
    now: datetime,
    run_id: str,
    settings: Settings | None = None,
) -> list[Position]:
    """P2 remediation (2026-09-11): forward-only recovery for CONFIRMED
    candidates that never got a PAPER position - e.g. a crash between Gate
    confirming the candidate and replay.py's own same-tick immediate-open
    loop reaching it. Intended to run at the start of every discovery cycle
    (discovery_loop.py::run_discovery_tick()).

    Reuses the already-persisted risk assessment via the existing, already-
    tested open_position_for_candidate() - never re-runs Gate/AI/Risk, zero
    new AI cost. Idempotent: open_position_for_candidate() itself refuses to
    create a second position for the same candidate_id.

    `settings` (2026-09-14, Task 6 - Guardian Authority pre-entry veto
    wiring): optional, additive, threaded through purely so this call site
    can gate the same maybe_open_position_for_candidate() wrapper
    replay.py's own immediate-open loop uses - see guardian/authority.py.
    Defaults to None for backward compatibility with every pre-Task-6
    caller/test that does not pass it (this function's own signature is a
    dependency other code/tests already call positionally); when None, this
    sweep calls open_position_for_candidate() directly, byte-identical to
    every prior behavior of this function - the wrapper's own
    authority_enabled gate is never even consulted in that case.

    Deliberately forward-only: a one-time activation watermark
    (storage/repository.py's schema_meta key 'recovery_sweep_activated_at')
    excludes any candidate CONFIRMED before this feature's first-ever call
    on this database. Historical orphans that already existed in production
    before this deploy are intentionally left alone - a separate, explicit
    cleanup decision, never auto-opened just because this sweep now
    exists."""
    repo.set_recovery_sweep_activated_at_if_missing(now)
    activated_at = repo.get_recovery_sweep_activated_at()

    opened: list[Position] = []
    for candidate in repo.find_candidates_by_status("CONFIRMED"):
        if repo.get_position(candidate.candidate_id) is not None:
            continue  # already has a position - nothing to recover
        confirmed_at = repo.get_candidate_confirmed_at(candidate.candidate_id)
        if confirmed_at is None or confirmed_at < activated_at:
            continue  # historical orphan, or no CONFIRMED event on record - out of scope
        try:
            ticker = Ticker.from_raw(connector.get_ticker(candidate.instrument))
        except ConnectorUnavailableError as exc:
            log_event(
                run_id, event="recovery_sweep_ticker_unavailable",
                candidate_id=candidate.candidate_id, instrument=candidate.instrument,
                error=str(exc),
            )
            continue
        if settings is not None:
            position = maybe_open_position_for_candidate(
                repo, candidate, risk_limits, ticker.last_price, now, run_id, settings
            )
        else:
            position = open_position_for_candidate(
                candidate, repo, risk_limits, ticker.last_price, now, run_id
            )
        if position is not None:
            opened.append(position)
            log_event(
                run_id, event="recovery_sweep_position_opened",
                candidate_id=candidate.candidate_id, position_id=position.position_id,
            )
    return opened
