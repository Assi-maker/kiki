from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
# 2026-09-25 (Fas 2A.1): catch-up used to fetch only the LATEST 1000 1m
# candles, so any outage longer than ~16.7 h silently skipped its own
# beginning - the part where a stop was most likely to have been crossed.
# It now walks the WHOLE gap forward from its start in pages of the
# BingX per-call cap (1440 candles, live-verified 2026-09-12, the same cap
# backtest/historical_fetch.py uses): exact coverage instead of a bigger
# buffer. Minutes the exchange itself does not return are reported as
# `kline_history_gap` errors (run status partial_error), never assumed.
_PAGE_CANDLES = 1440
# The minute that is still forming at `now`, and the boundary minute at
# `since`, are legitimately absent - not history gaps.
_BOUNDARY_TOLERANCE = timedelta(minutes=2)


class LivePriceSource(Protocol):
    def get_klines(
        self, symbol: str, interval: str, limit: int = 1,
        start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> list[dict]: ...
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
                missed, history_gaps = _fetch_missed_candles(connector, symbol, since, now)
                funding_rate = _latest_funding_rate(connector, symbol)
            except ConnectorUnavailableError as exc:
                errors.append(f"{type(exc).__name__}: {exc} ({symbol})")
                continue
            for gap_start, gap_end in history_gaps:
                errors.append(
                    f"kline_history_gap {gap_start.isoformat()}..{gap_end.isoformat()} ({symbol})"
                )
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


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _fetch_missed_candles(
    connector: LivePriceSource, symbol: str, since: datetime, now: datetime
) -> tuple[list[Kline], list[tuple[datetime, datetime]]]:
    """Every 1m candle in (since, now], paged forward from `since`, plus
    the windows the exchange did not return. Pages are bounded by
    startTime/endTime, so nothing after `now` can be requested."""
    by_time: dict[datetime, Kline] = {}
    cursor = since
    while cursor < now:
        page_end = min(cursor + timedelta(minutes=_PAGE_CANDLES), now)
        raw_klines = connector.get_klines(
            symbol, _CATCHUP_INTERVAL, limit=_PAGE_CANDLES,
            start_time_ms=_ms(cursor), end_time_ms=_ms(page_end),
        )
        for raw in raw_klines:
            kline = Kline.from_raw(raw, symbol, _CATCHUP_INTERVAL)
            if since < kline.observed_at <= now:
                by_time[kline.observed_at] = kline
        cursor = page_end
    candles = [by_time[moment] for moment in sorted(by_time)]
    return candles, _history_gaps(candles, since, now)


def _history_gaps(
    candles: list[Kline], since: datetime, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Windows longer than one candle with no candle, including a missing
    start or end of the gap (minus the boundary minutes)."""
    edges = [since] + [k.observed_at for k in candles] + [now]
    gaps: list[tuple[datetime, datetime]] = []
    for index, (a, b) in enumerate(zip(edges, edges[1:], strict=False)):
        allowed = timedelta(minutes=1)
        if index == 0 or index == len(edges) - 2:
            allowed += _BOUNDARY_TOLERANCE
        if b - a > allowed:
            gaps.append((a, b))
    return gaps


def _latest_funding_rate(connector: LivePriceSource, symbol: str) -> Decimal:
    raw_funding = connector.get_funding_rate(symbol, limit=1)
    return FundingRate.from_raw(raw_funding[-1]).funding_rate if raw_funding else Decimal("0")
