from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.schemas.market import FundingRate, Kline
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_CATCHUP_INTERVAL = "1m"
# Conservative bound on how many 1m candles a single catch-up pass fetches
# per instrument. A gap longer than this is only partially recovered - the
# very next NORMAL monitoring tick still correctly re-evaluates the current
# SL/TP/time-limit state using real wall-clock time regardless, so this
# bound trades "perfect backfill of an arbitrarily long outage" for "bounded,
# predictable exchange API load at startup", never for correctness of the
# ongoing, ordinary monitoring loop.
_MAX_CATCHUP_KLINES = 1000


class LivePriceSource(Protocol):
    def get_klines(self, symbol: str, interval: str, limit: int = 1) -> list[dict]: ...
    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]: ...


def run_monitoring_catchup(
    connector: LivePriceSource, repo: Repository, settings: Settings, now: datetime
) -> list[Position]:
    """P1 remediation (2026-09-11): heals the window between the last
    GUARANTEED-completed monitoring run (runs.completed_at IS NOT NULL -
    Repository.find_latest_completed_run(), never a row stuck at
    status='running', which means the process died mid-tick) and `now`.
    Intended to run exactly once, at monitoring-thread startup
    (monitoring_loop.py::run_forever(), before its periodic while-True tick
    loop resumes) - e.g. after a crash/restart left a gap of missed 1m
    candles.

    Replays every missed candle CHRONOLOGICALLY through the SAME already-
    tested close_triggered_positions()/check_exit_trigger() every normal
    tick uses - identical exit priority (stop_loss -> target -> time_limit
    -> guardian_exit) and identical SL/TP/time-limit math, zero changes to
    either. Each candle is evaluated with `now` set to THAT candle's own
    observed_at (not wall-clock time), so a position that closes on an
    early missed candle is correctly left alone by later candles - matching
    exactly what would have happened had monitoring never stopped. No AI
    call of any kind is made here.

    run_type='monitoring_catchup' (distinct from 'monitoring') so a second
    consecutive crash before any normal tick completes still re-anchors to
    the same last-known-good 'monitoring' row next restart, rather than
    silently narrowing the catch-up window to the catch-up's own partial
    progress - re-covering an overlapping range is harmless (close_
    triggered_positions is idempotent per position) and strictly safer than
    risking a silently skipped gap."""
    last_completed = repo.find_latest_completed_run("monitoring")
    if last_completed is None or last_completed["completed_at"] is None:
        return []  # first ever monitoring run this database has seen - nothing to catch up
    since = datetime.fromisoformat(last_completed["completed_at"])
    if since >= now:
        return []

    run_id = new_run_id()
    repo.start_run(run_id, "monitoring_catchup", now)
    closed: list[Position] = []
    errors: list[str] = []
    try:
        seen_instruments: set[str] = set()
        for position in repo.find_open_positions():
            symbol = position.instrument
            if symbol in seen_instruments:
                continue
            seen_instruments.add(symbol)
            try:
                missed = _fetch_missed_candles(connector, symbol, since, now)
                funding_rate = _latest_funding_rate(connector, symbol)
            except ConnectorUnavailableError as exc:
                errors.append(f"{type(exc).__name__}: {exc} ({symbol})")
                continue
            for kline in missed:
                price_lookup = {symbol: (kline.low, kline.high, kline.close, funding_rate)}
                closed.extend(
                    close_triggered_positions(
                        repo, price_lookup, kline.observed_at, settings.risk_limits,
                        run_id, guardian_config=settings.guardian,
                    )
                )
        repo.complete_run(
            run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors
        )
        return closed
    except Exception as exc:
        log_event(
            run_id, event="monitoring_catchup_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])
        return closed


def _fetch_missed_candles(
    connector: LivePriceSource, symbol: str, since: datetime, now: datetime
) -> list[Kline]:
    minutes_gap = max(1, math.ceil((now - since).total_seconds() / 60))
    limit = min(minutes_gap + 2, _MAX_CATCHUP_KLINES)  # +2: cover boundary/partial-minute rounding
    raw_klines = connector.get_klines(symbol, _CATCHUP_INTERVAL, limit=limit)
    candles = sorted(
        (Kline.from_raw(raw, symbol, _CATCHUP_INTERVAL) for raw in raw_klines),
        key=lambda k: k.observed_at,
    )
    return [k for k in candles if since < k.observed_at <= now]


def _latest_funding_rate(connector: LivePriceSource, symbol: str) -> Decimal:
    raw_funding = connector.get_funding_rate(symbol, limit=1)
    return FundingRate.from_raw(raw_funding[-1]).funding_rate if raw_funding else Decimal("0")
