"""Full-chain chronological historical replay (2026-09-18 LIVE-readiness
verification, requested ahead of the very first real LIVE trade).

Unlike `backtest/run_tier1_backtest.py` (which replays already-recorded REAL
positions' forward price path against Profit Protection shadow logic only)
or `paper_trading/replay.py::run_replay` (which drives the discovery/entry
half of the pipeline against hand-constructed snapshots, with no position
management/exit/self-improvement wiring), THIS module drives the WHOLE
chain - screening -> ranking -> entry (Gate/PRE_ENTRY_VETO) -> position
management (Guardian Authority TIGHTEN_SL/CLOSE_EARLY/TAKE_PROFIT) -> exits
(SL/TP/time-limit/Profit Protection) -> GODFATHER self-improvement
(propose/validate/promote/demote, both Guardian Authority's own pipeline and
the priority-boost overlay) - chronologically, from real historical BingX
market data, into one shared, disposable backtest repository, so GODFATHER
can accumulate real experience exactly as it would in production and this
can be measured for genuine (not overfit) improvement before real capital is
risked.

Every existing production function this module calls
(`paper_trading.replay.run_single_cycle`, `guardian.tick.run_guardian_tick_body`,
`paper_trading.position_closing.close_triggered_positions`,
`paper_trading.profit_protection_experiment.run_profit_protection_experiment_tick`,
`guardian.self_improvement.run_godfather_self_improvement_tick`,
`godfather.priority_boost.run_priority_boost_self_improvement_tick`) is
reused UNMODIFIED - this module is orchestration/adapter glue only, per the
user's explicit instruction not to redesign the trading architecture.

--------------------------------------------------------------------------
No-look-ahead guarantee (the single most safety-critical property here)
--------------------------------------------------------------------------
`HistoricalDataSource` (below) is the ONLY way any pipeline code reaches
historical market data in this module. Its `advance_to(now)` sets an
internal cursor, and every one of its methods filters the pre-fetched
dataset to `observed_at <= cursor` before returning anything - never a
separate "future" view, never a second code path. `run_full_chain_
historical_replay` only ever calls `advance_to` with a monotonically
non-decreasing `now` (the walk-forward loop below). GODFATHER's own
self-improvement pipelines need no additional no-look-ahead enforcement of
their own: `find_closed_positions()`/`find_resolved_...()` calls inside them
can only ever see rows this SAME backtest repo has already written, and
nothing in the future of the simulated walk has been created yet - the
no-look-ahead property is structural (a consequence of chronological
writing into one shared repo), not a filter that could be forgotten.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.agents.runner import AgentRunner
from crypto_trading.backtest.historical_fetch import (
    HistoricalMarketDataSource,
    fetch_historical_funding,
    fetch_historical_klines,
)
from crypto_trading.config.loader import Settings
from crypto_trading.godfather.priority_boost import run_priority_boost_self_improvement_tick
from crypto_trading.guardian.self_improvement import run_godfather_self_improvement_tick
from crypto_trading.guardian.tick import run_guardian_tick_body
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.paper_trading.profit_protection_experiment import (
    run_profit_protection_experiment_tick,
)
from crypto_trading.paper_trading.replay import MarketSnapshot, run_single_cycle
from crypto_trading.schemas.market import FundingRate, InstrumentMetadata, Kline, Ticker
from crypto_trading.screening.eligibility_filter import check_eligibility, select_top_n
from crypto_trading.storage.repository import Repository

# Stated simplification (universe selection): BingX's historical endpoints
# have no point-in-time 24h-liquidity ranking - only `get_all_tickers()`'s
# CURRENT snapshot does. A single present-day liquidity snapshot is used as
# a fixed proxy universe for the whole historical window. Large-cap USDT
# perpetuals are consistently liquid across short (weeks) windows, so this
# is a reasonable, explicitly-stated approximation, not a silent one.
HISTORICAL_UNIVERSE_SIZE = 25

# Historical resolution used for open-position management (Guardian ticks,
# SL/TP/time-limit checks) - the finest granularity BingX klines offer,
# mirroring backtest/replay_engine.py's own identical choice (`_KLINE_INTERVAL
# = "1m"`) for exactly the same reason.
MANAGEMENT_KLINE_INTERVAL = "1m"

# Trailing window used to approximate a "24h quote volume" ticker field from
# summed kline turnover (close * volume) - BingX klines report base-asset
# volume, not the exchange's own separately-computed quoteVolume, so this is
# an approximation, not a lookup. Documented, not silent.
_QUOTE_VOLUME_TRAILING_HOURS = 24

# Synthetic bid/ask spread used to build a historical ticker's ask/bid pair
# (real historical bid/ask microstructure is not recoverable from OHLCV
# candles). Deliberately small (well under any realistic
# eligibility_max_spread_pct) so this never spuriously fails the spread
# eligibility check - it exists only to give compute_spread_pct() a genuine,
# non-degenerate pair, not to model real historical liquidity conditions.
_SYNTHETIC_SPREAD_PCT = Decimal("0.0002")

# Guardian/monitoring tick cadence used for OPEN POSITION management during
# replay - production runs these on independent 30-60s real-time loops;
# 1 simulated minute is the finest granularity available from historical
# 1-minute klines and preserves the real "much tighter than discovery"
# relative cadence.
MANAGEMENT_TICK_MINUTES = 1


@dataclass
class HistoricalDataset:
    """Pre-fetched, in-memory historical market data for a fixed instrument
    universe, built once by `prefetch_historical_dataset` (see
    full_chain_data_prep.py) and shared read-only across an entire replay
    run. Never mutated after construction."""

    klines: dict[tuple[str, str], list[Kline]] = field(default_factory=dict)
    funding: dict[str, list[FundingRate]] = field(default_factory=dict)
    contracts_raw: dict[str, dict] = field(default_factory=dict)


def _kline_to_raw(kline: Kline) -> dict:
    return {
        "open": str(kline.open),
        "high": str(kline.high),
        "low": str(kline.low),
        "close": str(kline.close),
        "volume": str(kline.volume),
        "time": int(kline.observed_at.timestamp() * 1000),
    }


def _funding_to_raw(funding_rate: FundingRate) -> dict:
    return {
        "symbol": funding_rate.instrument,
        "fundingRate": str(funding_rate.funding_rate),
        "markPrice": str(funding_rate.mark_price),
        "fundingTime": int(funding_rate.observed_at.timestamp() * 1000),
    }


class HistoricalDataSource:
    """Adapter satisfying `market_snapshot.LiveMarketDataSource`,
    `guardian.data.GuardianDataSource` and `monitoring_loop.LivePriceSource`
    simultaneously (their method sets overlap: `get_ticker`/`get_klines`/
    `get_funding_rate` are shape-identical raw-dict Protocols across all
    three) - one object plays all three roles during replay, exactly as
    `BingXMarketDataConnector` does in production.

    ALL data is filtered to `observed_at <= self._now` before being
    returned - see module docstring's "No-look-ahead guarantee" section.
    `advance_to` is the ONLY way `self._now` ever changes, and the replay
    driver below only ever calls it with non-decreasing timestamps."""

    def __init__(self, dataset: HistoricalDataset, universe: list[str]) -> None:
        self._dataset = dataset
        self._universe = universe
        self._now: datetime = datetime.min.replace(tzinfo=UTC)

    def advance_to(self, now: datetime) -> None:
        self._now = now

    @property
    def now(self) -> datetime:
        return self._now

    def get_contracts(self) -> list[dict]:
        contracts_raw = self._dataset.contracts_raw
        return [contracts_raw[s] for s in self._universe if s in contracts_raw]

    def _visible_klines(self, symbol: str, interval: str) -> list[Kline]:
        """O(log n) via `bisect_right` on the pre-sorted-ascending series
        (guaranteed by `fetch_historical_fetch.py::fetch_historical_klines`'s
        own sort) - required for real production-scale data (a 21-day 1m
        series is ~30k candles; a linear filter per call, called on every
        tick, does not scale to a multi-week replay)."""
        series = self._dataset.klines.get((symbol, interval), [])
        idx = bisect_right(series, self._now, key=lambda k: k.observed_at)
        return series[:idx]

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[dict]:
        visible = self._visible_klines(symbol, interval)
        if start_time_ms is not None:
            visible = [k for k in visible if int(k.observed_at.timestamp() * 1000) >= start_time_ms]
        if end_time_ms is not None:
            visible = [k for k in visible if int(k.observed_at.timestamp() * 1000) <= end_time_ms]
        # `visible` is already ascending - slice the last `limit` then
        # reverse (not re-sort) to match real BingX's newest-first response
        # order (see market_snapshot.py's own documented discovery of this);
        # every real caller already re-sorts ascending itself regardless.
        # Python slicing handles limit >= len(visible) gracefully (returns
        # everything), so no separate branch is needed.
        newest_first = list(reversed(visible[-limit:])) if limit > 0 else []
        return [_kline_to_raw(k) for k in newest_first]

    def _visible_funding(self, symbol: str) -> list[FundingRate]:
        series = self._dataset.funding.get(symbol, [])
        idx = bisect_right(series, self._now, key=lambda f: f.observed_at)
        return series[:idx]

    def get_funding_rate(
        self,
        symbol: str,
        limit: int = 1,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[dict]:
        visible = self._visible_funding(symbol)
        if start_time_ms is not None:
            visible = [f for f in visible if int(f.observed_at.timestamp() * 1000) >= start_time_ms]
        if end_time_ms is not None:
            visible = [f for f in visible if int(f.observed_at.timestamp() * 1000) <= end_time_ms]
        newest_first = sorted(visible, key=lambda f: f.observed_at, reverse=True)
        return [_funding_to_raw(f) for f in newest_first[:limit]]

    def _latest_management_kline(self, symbol: str) -> Kline | None:
        visible = self._visible_klines(symbol, MANAGEMENT_KLINE_INTERVAL)
        return max(visible, key=lambda k: k.observed_at) if visible else None

    def _synthetic_ticker_raw(self, symbol: str) -> dict | None:
        """Built from the latest completed 1m candle at-or-before the
        cursor - never a later one. `quoteVolume` approximates real
        exchange-reported 24h turnover as sum(close*volume) over the
        trailing 24h of visible 1m candles (BingX klines report base-asset
        volume, not the exchange's own separately-computed quoteVolume -
        this is a stated approximation). `askPrice`/`bidPrice` use a fixed
        small synthetic spread (see _SYNTHETIC_SPREAD_PCT) - real historical
        bid/ask microstructure is not recoverable from OHLCV candles.
        `closeTime` is the SOURCE CANDLE's own observed_at (never `self._now`
        itself) so a genuine data gap still fails staleness checks
        correctly, exactly like a real feed going stale would."""
        latest = self._latest_management_kline(symbol)
        if latest is None:
            return None
        window_start = self._now - timedelta(hours=_QUOTE_VOLUME_TRAILING_HOURS)
        trailing = [
            k for k in self._visible_klines(symbol, MANAGEMENT_KLINE_INTERVAL)
            if k.observed_at > window_start
        ]
        quote_volume = sum((k.close * k.volume for k in trailing), Decimal("0"))
        half_spread = latest.close * _SYNTHETIC_SPREAD_PCT / 2
        return {
            "symbol": symbol,
            "lastPrice": str(latest.close),
            "priceChange": "0",
            "priceChangePercent": "0",
            "highPrice": str(latest.high),
            "lowPrice": str(latest.low),
            "volume": str(latest.volume),
            "quoteVolume": str(quote_volume),
            "openPrice": str(latest.open),
            "askPrice": str(latest.close + half_spread),
            "askQty": "1000",
            "bidPrice": str(latest.close - half_spread),
            "bidQty": "1000",
            "closeTime": int(latest.observed_at.timestamp() * 1000),
        }

    def get_ticker(self, symbol: str) -> dict:
        raw = self._synthetic_ticker_raw(symbol)
        if raw is None:
            from crypto_trading.connectors.exceptions import ConnectorUnavailableError

            raise ConnectorUnavailableError(f"no historical data visible yet for {symbol!r}")
        return raw

    def get_all_tickers(self) -> list[dict]:
        out = []
        for symbol in self._universe:
            raw = self._synthetic_ticker_raw(symbol)
            if raw is not None:
                out.append(raw)
        return out

    def get_open_interest(self, symbol: str) -> dict:
        """Stated simplification: BingX's historical endpoints expose no
        open-interest time series at all (only a current snapshot -
        `screening/quant_screener.py::build_funding_oi_evidence`'s own
        comment already documents that OI contributes nothing to that
        metric's numeric baseline in this codebase, funding-rate history
        does all the work). A synthetic, always-fresh OI reading is
        returned purely so `data_quality_status`'s completeness/staleness
        classification (a dimension OI feeds into but never drives the
        actual candidate score) doesn't spuriously fail for a reason with
        no real historical data to check against."""
        return {"symbol": symbol, "openInterest": "0", "time": int(self._now.timestamp() * 1000)}


def select_historical_universe(
    connector: HistoricalMarketDataSource, settings: Settings, size: int = HISTORICAL_UNIVERSE_SIZE
) -> tuple[list[str], dict[str, dict]]:
    """ONE real, current `get_contracts()`/`get_all_tickers()` call, ranked
    by `quote_volume` via the unmodified `select_top_n` - see
    HISTORICAL_UNIVERSE_SIZE's own docstring for why this is a stated
    proxy, not a real point-in-time historical ranking. Returns
    `(symbols, contracts_raw_by_symbol)`."""
    contracts_raw = connector.get_contracts()
    contracts_by_symbol = {c["symbol"]: c for c in contracts_raw}
    now = datetime.now(UTC)
    tickers = []
    for raw in connector.get_all_tickers():
        symbol = raw.get("symbol")
        if symbol not in contracts_by_symbol:
            continue
        try:
            ticker = Ticker.from_raw(raw)
        except (KeyError, ValueError, TypeError):
            continue
        instrument = InstrumentMetadata.from_raw(contracts_by_symbol[symbol], now)
        eligible, _reason = check_eligibility(
            instrument, ticker, "ok",
            settings.pipeline.eligibility_min_quote_volume_24h_usdt,
            settings.pipeline.eligibility_max_spread_pct,
        )
        if eligible:
            tickers.append(ticker)
    symbols = select_top_n(tickers, size)
    return symbols, {s: contracts_by_symbol[s] for s in symbols}


def prefetch_historical_dataset(
    connector: HistoricalMarketDataSource,
    universe: list[str],
    contracts_raw: dict[str, dict],
    settings: Settings,
    start: datetime,
    end: datetime,
    cache_dir,
) -> HistoricalDataset:
    """One-time real historical market-data fetch (free - BingX market data,
    no Anthropic cost) for every (symbol, interval) pair the replay needs:
    the primary and (if configured) secondary screener timeframes (for
    discovery/screening fidelity) plus `MANAGEMENT_KLINE_INTERVAL` (for
    open-position management fidelity). Reuses `fetch_historical_klines`/
    `fetch_historical_funding` UNMODIFIED - both already disk-cache by
    (symbol, interval, start, end), so a second call with the same
    arguments issues zero further network requests."""
    intervals = [settings.pipeline.screener_timeframes[0]]
    if len(settings.pipeline.screener_timeframes) > 1:
        intervals.append(settings.pipeline.screener_timeframes[1])
    if MANAGEMENT_KLINE_INTERVAL not in intervals:
        intervals.append(MANAGEMENT_KLINE_INTERVAL)

    dataset = HistoricalDataset(contracts_raw=dict(contracts_raw))
    for symbol in universe:
        for interval in intervals:
            dataset.klines[(symbol, interval)] = fetch_historical_klines(
                connector, symbol, interval, start, end, cache_dir
            )
        dataset.funding[symbol] = fetch_historical_funding(connector, symbol, start, end, cache_dir)
    return dataset


def _latest_price_lookup(
    source: HistoricalDataSource, open_positions, now: datetime
) -> dict[str, tuple[Decimal, Decimal, Decimal, Decimal]]:
    """price_lookup shape close_triggered_positions/run_profit_protection_
    experiment_tick already require: instrument -> (candle_low, candle_high,
    current_price, funding_rate), built from the SAME as-of-`now`
    HistoricalDataSource used everywhere else this tick."""
    lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for position in open_positions:
        symbol = position.instrument
        if symbol in lookup:
            continue
        klines = source._visible_klines(symbol, MANAGEMENT_KLINE_INTERVAL)
        if not klines:
            continue
        latest = max(klines, key=lambda k: k.observed_at)
        funding_raw = source.get_funding_rate(symbol, limit=1)
        funding_rate = Decimal(str(funding_raw[0]["fundingRate"])) if funding_raw else Decimal("0")
        lookup[symbol] = (latest.low, latest.high, latest.close, funding_rate)
    return lookup


def build_historical_snapshot(
    source: HistoricalDataSource, settings: Settings, now: datetime
) -> MarketSnapshot:
    """Historical analog of `market_snapshot.py::build_live_snapshot` -
    same eligibility/top-N/data-quality assembly logic (reusing
    `check_eligibility`/`select_top_n` unmodified), sourced entirely from
    `source` (already advanced to `now`) instead of live network calls. No
    retry/staleness-retry loop is needed (unlike the live version): historical
    data either exists as of `now` or it does not, there is nothing to retry."""
    contracts_raw = source.get_contracts()
    instruments = {c["symbol"]: InstrumentMetadata.from_raw(c, now) for c in contracts_raw}

    tickers: dict[str, Ticker] = {}
    for raw in source.get_all_tickers():
        symbol = raw["symbol"]
        try:
            tickers[symbol] = Ticker.from_raw(raw)
        except (KeyError, ValueError, TypeError):
            continue

    eligible = []
    for symbol, ticker in tickers.items():
        ok, _reason = check_eligibility(
            instruments[symbol], ticker, "ok",
            settings.pipeline.eligibility_min_quote_volume_24h_usdt,
            settings.pipeline.eligibility_max_spread_pct,
        )
        if ok:
            eligible.append(ticker)
    top_n_symbols = set(select_top_n(eligible, settings.pipeline.top_n))

    interval = settings.pipeline.screener_timeframes[0]
    secondary_interval = (
        settings.pipeline.screener_timeframes[1]
        if len(settings.pipeline.screener_timeframes) > 1
        else None
    )
    klines: dict[str, list[Kline]] = {}
    funding_rates: dict[str, list[FundingRate]] = {}
    secondary_klines: dict[str, list[Kline]] = {}
    secondary_funding_rates: dict[str, list[FundingRate]] = {}
    data_quality_status: dict[str, str] = {}

    lookback = settings.pipeline.screener_lookback_periods + 5
    funding_limit = settings.pipeline.screener_funding_history_limit
    for symbol in top_n_symbols:
        raw_klines = source.get_klines(symbol, interval, limit=lookback)
        parsed_klines = sorted(
            (Kline.from_raw(k, symbol, interval) for k in raw_klines), key=lambda k: k.observed_at
        )
        klines[symbol] = parsed_klines
        raw_funding = source.get_funding_rate(symbol, limit=funding_limit)
        funding_rates[symbol] = sorted(
            (FundingRate.from_raw(f) for f in raw_funding), key=lambda f: f.observed_at
        )
        data_quality_status[symbol] = "ok" if parsed_klines and funding_rates[symbol] else "invalid"

        secondary_klines[symbol] = []
        secondary_funding_rates[symbol] = []
        if secondary_interval is not None:
            raw_secondary = source.get_klines(symbol, secondary_interval, limit=lookback)
            secondary_klines[symbol] = [
                Kline.from_raw(k, symbol, secondary_interval) for k in raw_secondary
            ]
            raw_secondary_funding = source.get_funding_rate(symbol, limit=funding_limit)
            secondary_funding_rates[symbol] = [
                FundingRate.from_raw(f) for f in raw_secondary_funding
            ]

    missing_status = {
        s: "invalid" for s in instruments if s not in top_n_symbols and s not in data_quality_status
    }
    return MarketSnapshot(
        simulated_now=now,
        instruments=instruments,
        tickers=tickers,
        klines=klines,
        funding_rates=funding_rates,
        secondary_klines=secondary_klines,
        secondary_funding_rates=secondary_funding_rates,
        data_quality_status=data_quality_status | missing_status,
    )


def historical_replay_budget_exhausted(
    repo: Repository,
    replay_started_at: datetime,
    total_budget_usd: Decimal,
    headroom_fraction: float = 0.9,
) -> bool:
    """Reuses the EXISTING `sum_ai_cost_since` ledger (no new cost-tracking
    table) to cap CUMULATIVE real AI spend across the WHOLE replay (all
    simulated days combined), not just the existing per-day gate
    (`guardian/tick.py::_budget_allows_one_more_call`, which already caps
    spend PER simulated UTC day and keeps working unmodified underneath
    this - the two are complementary, not duplicative: the daily gate stops
    one day from overspending, this stops the WHOLE multi-day run from
    overspending)."""
    spent = repo.sum_ai_cost_since(replay_started_at)
    return spent > total_budget_usd * Decimal(str(headroom_fraction))


def _run_management_tick(
    repo: Repository,
    source: HistoricalDataSource,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    now: datetime,
) -> None:
    """One minute's worth of open-position management: Guardian Authority's
    tick-time decisions (TIGHTEN_SL/CLOSE_EARLY/TAKE_PROFIT) plus the
    deterministic SL/TP/time-limit close check and Profit Protection -
    exactly the calls `guardian_loop.py`/`monitoring_loop.py` make in
    production, reused unmodified. `live_connector=None` always - this
    replay is PAPER-only by construction, never capable of touching a real
    LIVE order regardless of any config flag."""
    run_guardian_tick_body(repo, source, runner, settings, run_id, now, live_connector=None)

    open_positions = repo.find_open_positions()
    if not open_positions:
        return
    price_lookup = _latest_price_lookup(source, open_positions, now)
    if not price_lookup:
        return
    closed = close_triggered_positions(
        repo, price_lookup, now, settings.risk_limits, run_id, guardian_config=settings.guardian
    )
    run_profit_protection_experiment_tick(
        repo, open_positions, closed, price_lookup, now, settings, run_id
    )


def run_full_chain_historical_replay(
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    source: HistoricalDataSource,
    start: datetime,
    end: datetime,
    run_id: str,
    screener_runner: AgentRunner | None = None,
    discovery_interval_minutes: int | None = None,
    management_interval_minutes: int = MANAGEMENT_TICK_MINUTES,
    total_ai_budget_usd: Decimal | None = None,
) -> dict:
    """Chronological walk-forward from `start` to `end`. See module
    docstring for the no-look-ahead guarantee and which existing production
    functions are reused unmodified at each step. Never writes to
    `data/crypto_trading.db` - `repo` must be a fresh, disposable
    repository the caller constructs (mirrors `run_tier1_backtest`'s own
    discipline).

    Returns a small summary dict (`status`, `stopped_at`, `n_discovery_ticks`,
    `n_management_ticks`, `n_self_improvement_days`) - full metrics
    (win rate, P/L, heuristic lifecycle, AI cost) are computed by a SEPARATE
    analysis pass querying `repo` directly, not by this function (see module
    docstring)."""
    discovery_minutes = discovery_interval_minutes or settings.pipeline.discovery_interval_minutes
    replay_started_at = start
    last_self_improvement_day: str | None = None
    n_discovery_ticks = 0
    n_management_ticks = 0
    n_self_improvement_days = 0
    status = "completed"
    stopped_at = end

    now = start
    while now < end:
        if (
            total_ai_budget_usd is not None
            and historical_replay_budget_exhausted(repo, replay_started_at, total_ai_budget_usd)
        ):
            status = "budget_exhausted_stopped_early"
            stopped_at = now
            log_event(
                run_id, event="full_chain_replay_budget_exhausted", stopped_at=now.isoformat(),
                total_ai_budget_usd=str(total_ai_budget_usd),
                spent_so_far=str(repo.sum_ai_cost_since(replay_started_at)),
            )
            break

        source.advance_to(now)
        day_key = now.date().isoformat()
        if day_key != last_self_improvement_day:
            last_self_improvement_day = day_key
            # Externally gated (not just relying on the function's own
            # internal authority_enabled check) to match discovery_loop.py's
            # exact call-site convention - see
            # test_self_improvement_isolation.py::test_run_godfather_self_
            # improvement_tick_wiring_is_gated_by_authority_enabled, which
            # requires EVERY real call site to carry this syntactic guard,
            # not just existence of one gated site elsewhere.
            if settings.guardian.authority_enabled:
                try:
                    run_godfather_self_improvement_tick(repo, runner, settings, run_id, now)
                except Exception as exc:  # noqa: BLE001 - one concern's failure must never block another
                    log_event(
                        run_id, event="full_chain_replay_guardian_self_improvement_failed",
                        error_type=type(exc).__name__, error=str(exc),
                    )
            try:
                run_priority_boost_self_improvement_tick(repo, runner, settings, run_id, now)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    run_id, event="full_chain_replay_priority_boost_failed",
                    error_type=type(exc).__name__, error=str(exc),
                )
            n_self_improvement_days += 1

        try:
            snapshot = build_historical_snapshot(source, settings, now)
            run_single_cycle(
                snapshot, repo, runner, settings, run_id, screener_runner=screener_runner
            )
        except Exception as exc:  # noqa: BLE001
            log_event(
                run_id, event="full_chain_replay_discovery_tick_failed", now=now.isoformat(),
                error_type=type(exc).__name__, error=str(exc),
            )
        n_discovery_ticks += 1

        management_end = min(now + timedelta(minutes=discovery_minutes), end)
        minute_cursor = now
        while minute_cursor < management_end:
            source.advance_to(minute_cursor)
            try:
                _run_management_tick(repo, source, runner, settings, run_id, minute_cursor)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    run_id, event="full_chain_replay_management_tick_failed",
                    now=minute_cursor.isoformat(), error_type=type(exc).__name__, error=str(exc),
                )
            n_management_ticks += 1
            minute_cursor += timedelta(minutes=management_interval_minutes)

        now += timedelta(minutes=discovery_minutes)

    return {
        "status": status,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "stopped_at": stopped_at.isoformat(),
        "n_discovery_ticks": n_discovery_ticks,
        "n_management_ticks": n_management_ticks,
        "n_self_improvement_days": n_self_improvement_days,
    }
