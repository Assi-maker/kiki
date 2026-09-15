from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.guardian.authority import resolve_pending_pre_entry_shadows
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.paper_trading.guardian_authority_shadow import (
    run_guardian_authority_shadow_tick,
    update_shadow_heuristics_from_resolved_shadow_observations,
)
from crypto_trading.paper_trading.monitoring_catchup import run_monitoring_catchup
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.paper_trading.profit_protection_experiment import (
    run_profit_protection_experiment_tick,
)
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

        open_positions = list(repo.find_open_positions())
        for position in open_positions:
            symbol = position.instrument
            if symbol in price_lookup:
                continue
            try:
                ticker = Ticker.from_raw(connector.get_ticker(symbol))
                raw_klines = connector.get_klines(symbol, interval, limit=1)
                if not raw_klines:
                    # Same "no usable data available" category as a connector
                    # failure - an empty-but-technically-successful response
                    # (live incident 2026-09-04) must never crash [-1] and
                    # abort the whole tick's checks for every OTHER open
                    # position too.
                    raise ConnectorUnavailableError(f"{symbol}: tom klines-lista")
                latest_kline = Kline.from_raw(raw_klines[-1], symbol, interval)
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

        closed = close_triggered_positions(
            repo, price_lookup, now, settings.risk_limits, run_id, guardian_config=settings.guardian
        )
        try:
            run_profit_protection_experiment_tick(
                repo, open_positions, closed, price_lookup, now, settings, run_id
            )
        except Exception as exc:
            log_event(
                run_id, event="profit_protection_experiment_tick_failed",
                error_type=type(exc).__name__, error=str(exc),
            )
        try:
            if settings.guardian.authority_shadow_enabled:
                run_guardian_authority_shadow_tick(
                    repo, open_positions, closed, price_lookup, now, settings, run_id
                )
        except Exception as exc:
            log_event(
                run_id, event="guardian_authority_shadow_tick_failed",
                error_type=type(exc).__name__, error=str(exc),
            )
        # Task 7 (Guardian Authority Shadow/Observation Mode, 2026-09-15):
        # resolve Task 6's pre-entry shadow rows against real position
        # outcomes. Its own small step, in its OWN try/except - kept
        # separate from run_guardian_authority_shadow_tick's try/except
        # immediately above (rather than folded into it) so a crash in
        # either can never be attributed to, or mask, a crash in the
        # other, matching this function's own existing "each concern gets
        # its own try/except" discipline (PP experiment vs. shadow tick,
        # above). Gated by the SAME settings.guardian.authority_shadow_
        # enabled flag, at the same call site - and, unlike the shadow
        # tick call, unconditional on `closed` (must run every tick: a
        # shadow row can become resolvable on any later tick once its
        # position happens to close, not only the tick it closes on).
        try:
            if settings.guardian.authority_shadow_enabled:
                resolve_pending_pre_entry_shadows(repo, now, run_id)
        except Exception as exc:
            log_event(
                run_id, event="guardian_authority_pre_entry_shadow_resolution_tick_failed",
                error_type=type(exc).__name__, error=str(exc),
            )
        # Task 8 (Guardian Authority Shadow/Observation Mode, 2026-09-15):
        # self-critique-from-shadow-data - derives heuristics from Task 5's
        # (tick-time) resolved shadow rows, into the SEPARATE `guardian_
        # authority_shadow_heuristics` table (never the real one). Its own
        # try/except and own log_event name, same "each concern gets its
        # own try/except" discipline as the two blocks above, gated by the
        # SAME settings.guardian.authority_shadow_enabled flag.
        #
        # Cadence: called UNCONDITIONALLY every tick (rather than the real
        # Task 9's own opportunistic "only if >= 1 new resolution this
        # tick" cadence, guardian/tick.py) - deliberately, not an oversight:
        # run_guardian_authority_shadow_tick above resolves shadows
        # internally (its own step 3) without returning a resolved-this-
        # tick count, and threading one out would mean touching Task 5's
        # already-shipped function signature for a cadence optimization
        # this task does not need. The cost of a no-op call (an empty/
        # unchanged find_resolved_guardian_authority_shadows() scan plus
        # in-memory grouping) is negligible at this feature's data scale -
        # it ships default-OFF and is never activated within this plan,
        # same "real accumulated data will initially be very low" reasoning
        # the real Task 9's own threshold comments already documented.
        try:
            if settings.guardian.authority_shadow_enabled:
                update_shadow_heuristics_from_resolved_shadow_observations(repo, now)
        except Exception as exc:
            log_event(
                run_id, event="guardian_authority_shadow_self_critique_failed",
                error_type=type(exc).__name__, error=str(exc),
            )
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
    run_monitoring_catchup(connector, repo, settings, datetime.now(UTC))
    while True:
        run_monitoring_tick(connector, repo, settings)
        time.sleep(settings.pipeline.monitoring_interval_seconds)
