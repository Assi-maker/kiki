from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.schemas.market import FundingRate, Kline, Ticker
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository


class LivePriceSource(Protocol):
    def get_ticker(self, symbol: str) -> dict: ...
    def get_klines(self, symbol: str, interval: str, limit: int = 1) -> list[dict]: ...
    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]: ...


def run_monitoring_tick(
    connector: LivePriceSource, repo: Repository, settings: Settings
) -> list[Position]:
    """En övervaknings-tick (SPEC §7, PLAN_CRYPTO_PHASE5.md Task 8): hämtar
    live pris/candle/funding för varje öppen positions instrument och kör
    dem genom Fas 4:s redan bevisade `close_triggered_positions()` (samma
    konservativa gap-fill-logik som replay, oförändrad).

    Två fail-safe-lager, medvetet olika omfång:
    - Inre `except ConnectorUnavailableError` (oförändrad från planens
      ursprungliga pseudokod): ett enskilt instruments datahämtning
      misslyckas -> det instrumentets position lämnas kvar öppen denna
      tick (aldrig en gissad stängning), övriga positioner påverkas inte.
    - Yttre `except Exception` (Conflict-fix 2026-08-27, se
      PLAN_CRYPTO_PHASE5.md Task 8): ett OVÄNTAT fel - t.ex. ett genuint
      ofullständigt rådata-svar som får `Ticker.from_raw()`/`Kline.from_raw()`/
      `FundingRate.from_raw()` att kasta `KeyError`/`ValueError` istället för
      `ConnectorUnavailableError` - kraschar annars hela funktionen och,
      via `run_forever()`s triviala `while True`-loop, hela
      övervakningsprocessen. Detta bröt mot Global Constraints redan innan
      denna fix (`"ett oväntat undantag i en enskild run_discovery_tick()/
      run_monitoring_tick() får aldrig krascha run_forever()"`) - samma
      redan etablerade och testade mönster som
      `discovery_loop.run_discovery_tick()` (Task 7)."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "monitoring", now)
    try:
        interval = settings.pipeline.screener_timeframes[0]  # Beslut 6
        price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        errors: list[str] = []

        for position in repo.find_open_positions():
            symbol = position.instrument
            if symbol in price_lookup:
                continue
            try:
                ticker = Ticker.from_raw(connector.get_ticker(symbol))
                latest_kline = Kline.from_raw(
                    connector.get_klines(symbol, interval, limit=1)[-1], symbol, interval
                )
                raw_funding = connector.get_funding_rate(symbol, limit=1)
                funding_rate = (
                    FundingRate.from_raw(raw_funding[-1]).funding_rate
                    if raw_funding
                    else Decimal("0")
                )
            except ConnectorUnavailableError as exc:
                errors.append(f"{type(exc).__name__}: {exc} ({symbol})")
                continue
            price_lookup[symbol] = (
                latest_kline.low,
                latest_kline.high,
                ticker.last_price,
                funding_rate,
            )

        closed = close_triggered_positions(repo, price_lookup, now, settings.risk_limits, run_id)
        repo.complete_run(
            run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors
        )
        return closed
    except Exception as exc:
        log_event(
            run_id, event="monitoring_tick_failed", error_type=type(exc).__name__, error=str(exc)
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])
        return []


def run_forever(connector: LivePriceSource, repo: Repository, settings: Settings) -> None:
    while True:
        run_monitoring_tick(connector, repo, settings)
        time.sleep(settings.pipeline.monitoring_interval_seconds)
