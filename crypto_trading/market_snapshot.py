from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.data_quality import (
    DataQualityResult,
    check_completeness,
    check_kline_consistency,
    check_staleness,
    classify,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.replay import MarketSnapshot
from crypto_trading.schemas.market import (
    FundingRate,
    InstrumentMetadata,
    Kline,
    OpenInterest,
    Ticker,
)
from crypto_trading.screening.eligibility_filter import check_eligibility, select_top_n


class LiveMarketDataSource(Protocol):
    def get_contracts(self) -> list[dict]: ...
    def get_ticker(self, symbol: str) -> dict: ...
    def get_all_tickers(self) -> list[dict]: ...
    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]: ...
    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]: ...
    def get_open_interest(self, symbol: str) -> dict: ...


def _fetch_klines_with_retry(
    connector: LiveMarketDataSource,
    symbol: str,
    interval: str,
    limit: int,
    required_fields: list[str],
    max_age_seconds: float,
    fetch_clock: Callable[[], datetime],
    max_retries: int,
    sleep_fn: Callable[[float], None],
    run_id: str,
) -> tuple[DataQualityResult, list[Kline], DataQualityResult]:
    """Begränsad retry (bugfix 2026-08-31, del 2): `get_klines()` kunde
    tidigare uppfattas ge intermittent gammal data. **Rotorsaken
    korrigerad 2026-09-01**: BingX:s /quote/klines returnerar konsekvent
    nyast-först (verifierat: 6 upprepade anrop, identisk ordning varje
    gång) - `parsed_klines[-1]` utan sortering plockade den ÄLDSTA candlen
    i batchen (t.ex. äkta 24h gammal vid limit=25/1h), inte den senaste.
    2026-08-31-hypotesen ("inkonsekvent cache mellan BingX:s backend-
    noder") var en felaktig förklaring av samma symptom - se
    sorteringen nedan för den faktiska fixen. Retry-loopen behålls som ett
    fail-safe-lager för genuint förekommande gles/temporärt gammal data
    (samma backoff-form som `_get_with_retry()`: 0.5 * 2^försök, tak 5s).
    Ger upp efter `max_retries` försök (aldrig oändligt) och loggar
    explicit - den redan fail-closed staleness-kontrollen (SPEC §8.3)
    fångar då korrekt upp kvarvarande, verkligen gammal data som
    `invalid`."""
    completeness: DataQualityResult = "invalid"
    parsed_klines: list[Kline] = []
    staleness: DataQualityResult = "invalid"
    for attempt in range(max_retries):
        raw_klines = connector.get_klines(symbol, interval, limit=limit)
        fetch_time = fetch_clock()
        completeness = (
            classify(*(check_completeness(raw, required_fields) for raw in raw_klines))
            if raw_klines
            else "invalid"
        )
        parsed_klines = (
            # BingX's /quote/klines (verifierat live 2026-09-01, v3-
            # endpointen, konsekvent över upprepade anrop) returnerar
            # NYAST-FÖRST, inte kronologisk ordning - `[-1]` utan denna sort
            # plockade tidigare den ÄLDSTA candlen i batchen (t.ex. 24h
            # gammal vid limit=25/1h), inte den senaste. Detta var den
            # faktiska rotorsaken bakom stalenessfelen som 2026-08-31-fixen
            # (se docstring ovan) felaktigt tillskrev "inkonsekvent cache
            # mellan BingX:s backend-noder" - samma reproducerbara fel varje
            # gång, inte intermittent.
            sorted(
                (Kline.from_raw(k, symbol, interval) for k in raw_klines),
                key=lambda k: k.observed_at,
            )
            if completeness == "ok"
            else []
        )
        staleness = (
            check_staleness(parsed_klines[-1].observed_at, fetch_time, max_age_seconds)
            if parsed_klines
            else "invalid"
        )
        if staleness == "ok":
            return completeness, parsed_klines, staleness
        if attempt < max_retries - 1:
            sleep_fn(min(0.5 * (2**attempt), 5))
    log_event(
        run_id,
        event="kline_staleness_retries_exhausted",
        symbol=symbol,
        attempts=max_retries,
    )
    return completeness, parsed_klines, staleness


def _fetch_funding_with_retry(
    connector: LiveMarketDataSource,
    symbol: str,
    limit: int,
    required_fields: list[str],
    max_age_seconds: float,
    fetch_clock: Callable[[], datetime],
    max_retries: int,
    sleep_fn: Callable[[float], None],
    run_id: str,
) -> tuple[DataQualityResult, list[FundingRate], DataQualityResult]:
    """Samma reproducerade fel och samma begränsade retry-princip som
    `_fetch_klines_with_retry()` ovan, för `get_funding_rate()`."""
    completeness: DataQualityResult = "invalid"
    parsed_funding: list[FundingRate] = []
    staleness: DataQualityResult = "invalid"
    for attempt in range(max_retries):
        raw_funding = connector.get_funding_rate(symbol, limit=limit)
        fetch_time = fetch_clock()
        completeness = (
            classify(*(check_completeness(raw, required_fields) for raw in raw_funding))
            if raw_funding
            else "invalid"
        )
        parsed_funding = (
            # Samma verkliga rotorsak/fix som klines ovan - BingX:s
            # /quote/fundingRate returnerar också nyast-först.
            sorted(
                (FundingRate.from_raw(f) for f in raw_funding), key=lambda f: f.observed_at
            )
            if completeness == "ok"
            else []
        )
        staleness = (
            check_staleness(parsed_funding[-1].observed_at, fetch_time, max_age_seconds)
            if parsed_funding
            else "invalid"
        )
        if staleness == "ok":
            return completeness, parsed_funding, staleness
        if attempt < max_retries - 1:
            sleep_fn(min(0.5 * (2**attempt), 5))
    log_event(
        run_id,
        event="funding_staleness_retries_exhausted",
        symbol=symbol,
        attempts=max_retries,
    )
    return completeness, parsed_funding, staleness


def build_live_snapshot(
    connector: LiveMarketDataSource,
    settings: Settings,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    run_id: str = "market_snapshot",
) -> MarketSnapshot:
    """Bygger en `MarketSnapshot` (Fas 4:s schema) från riktiga BingX-anrop
    och kör SPEC §8.1:s data-quality-klassificering på riktig, flerfälts
    data för första gången (PLAN_CRYPTO_PHASE5.md Task 6/Beslut 4).

    Avviker från planens ursprungliga Task 6-pseudokod på en punkt, löst
    av användaren 2026-08-27 (samma konflikt för samtliga fyra datatyper
    ticker/kline/funding/open interest): `check_completeness()` körs alltid
    på RÅDATAN innan något `.from_raw()`-anrop övervägs, aldrig efteråt.
    `Ticker.from_raw()`/`Kline.from_raw()`/`FundingRate.from_raw()`/
    `OpenInterest.from_raw()` indexerar alla direkt i sin rådata-dict utan
    `.get()`-fallback - en genuint saknad nyckel ger `KeyError`, inte ett
    tolkningsbart värde. Detta är redan ett etablerat kontrakt sedan Fas 1
    (`test_incomplete_ticker_is_invalid_before_even_reaching_pydantic`) -
    ofullständig rådata får aldrig nå en eager-parsande `.from_raw()`.
    En ofullständig ticker exkluderas helt ur `tickers` (ingen gissad/
    fabricerad `Ticker`), men får ändå en explicit `data_quality_status`-
    post via instrument-universumet från `get_contracts()` (alltid
    tillgängligt oavsett om tickern kunde parsas) - inte via `tickers`,
    som annars skulle sakna just den symbolen.

    `clock` (bugfix 2026-08-31, bekräftad mot en riktig live-körning):
    staleness för VARJE hämtad post (ticker/kline/funding/open interest)
    bedöms mot ett FÄRSKT `clock()`-anrop taget direkt efter just den
    postens nätverksanrop - aldrig mot det delade `now` som gäller för hela
    snapshoten. En sekventiell hämtning över hela instrumentuniversumet tar
    på riktig BingX-data ~19 minuter (1132 instrument, ~1 request/s verklig
    svarslatens) - långt mer än `max_data_age_seconds["ticker"]` (30s).
    Med staleness bedömd mot ETT `now` fångat FÖRE hela loopen blev VARJE
    ticker som hämtades mer än några sekunder in i loopen felaktigt
    klassad `invalid`: inte för att datan var gammal, utan för att dess
    `closeTime` (som korrekt speglar tiden den faktiskt hämtades) hamnade
    bortom `_FUTURE_TIMESTAMP_GRACE_SECONDS` (5s) relativt det redan
    inaktuella batch-start-`now`:t. Resultatet var strukturellt `eligible=0`
    oavsett marknadsläge eller screener-trösklar - inte ett tröskel- eller
    marknadsvillkor. `now`/`simulated_now` fortsätter oförändrat styra allt
    annat (look-ahead-bias-filtrering, `InstrumentMetadata.from_raw`,
    returnerad `simulated_now`) - bara staleness-jämförelsen bytte
    referenspunkt. `clock=None` (default) bevarar EXAKT tidigare beteende
    (`now` används som förr) - bakåtkompatibelt för alla anropare som inte
    uttryckligen skickar in en egen klocka; produktionsanropet i
    `discovery_loop.py` skickar `clock=lambda: datetime.now(UTC)`.

    `sleep_fn`/`run_id` (bugfix 2026-08-31, del 2): kline-/funding-
    hämtningen för top-N-symbolerna görs om (begränsat, se
    `_fetch_klines_with_retry()`/`_fetch_funding_with_retry()` ovan) om
    ett färskt anrop fortfarande faller på staleness - bekräftat mot
    riktig BingX-data att detta intermittent förekommer även när ticker/
    open interest är helt färska. `sleep_fn` styr backoff-fördröjningen
    (default riktig `time.sleep`, injicerbar i tester). `run_id` används
    bara för `log_event()` när retries förbrukas utan att datan blev
    färsk - `discovery_loop.py` skickar sitt riktiga run_id."""
    fetch_clock: Callable[[], datetime] = clock if clock is not None else (lambda: now)

    contracts_raw = connector.get_contracts()
    instruments = {c["symbol"]: InstrumentMetadata.from_raw(c, now) for c in contracts_raw}

    # Bulk-hämtning (bugfix 2026-09-01, empiriskt verifierad mot riktig
    # BingX: samma /ticker-endpoint utan `symbol` returnerar samtliga
    # instrument i ETT anrop, ~1-2s, i stället för en sekventiell
    # per-instrument-loop som på riktig data dominerade hela discovery-
    # cykelns körtid (15-23 min för ~1119 instrument) - se
    # BingXMarketDataConnector.get_all_tickers(). Ett totalt fel på detta
    # anropet (hela endpointen nere) fångas medvetet INTE här - propagerar
    # precis som ett get_contracts()-fel ovan, samma "hela endpointen är
    # nere" felkategori (extern blockerare, hela discovery_tick markeras
    # 'error' och försöks igen nästa cykel, se discovery_loop.py). `now`/
    # fetch_clock() tas EN gång direkt efter anropet - korrekt igen sedan
    # hela batchen anländer i ett enda svar, till skillnad från den gamla
    # loopen där sena instrument kunde vara flera minuter "efter" en tidig
    # gemensam tidsstämpel (se docstring ovan, 2026-08-31-buggen).
    raw_tickers_by_symbol = {t["symbol"]: t for t in connector.get_all_tickers()}
    ticker_fetch_time = fetch_clock()

    tickers: dict[str, Ticker] = {}
    ticker_dq: dict[str, DataQualityResult] = {}
    for symbol in instruments:
        raw_ticker = raw_tickers_by_symbol.get(symbol)
        if raw_ticker is None:
            # Symbolen fanns i get_contracts() men inte i bulk-ticker-svaret
            # (t.ex. en pausad symbol på BingX, AC3 2026-08-28) - klassas
            # som DATA_INVALID för det instrumentet, aldrig en krasch av
            # hela ticken, samma princip som redan gäller monitoring_loop.py.
            ticker_dq[symbol] = "invalid"
            continue
        completeness = check_completeness(raw_ticker, settings.pipeline.required_fields["ticker"])
        if completeness == "invalid":
            ticker_dq[symbol] = "invalid"
            continue
        ticker = Ticker.from_raw(raw_ticker)
        staleness = check_staleness(
            ticker.observed_at, ticker_fetch_time, settings.pipeline.max_data_age_seconds["ticker"]
        )
        ticker_dq[symbol] = classify(completeness, staleness)
        tickers[symbol] = ticker

    eligible = []
    for symbol, ticker in tickers.items():
        ok, _reason = check_eligibility(
            instruments[symbol],
            ticker,
            ticker_dq[symbol],
            settings.pipeline.eligibility_min_quote_volume_24h_usdt,
            settings.pipeline.eligibility_max_spread_pct,
        )
        if ok:
            eligible.append(ticker)
    top_n_symbols = set(select_top_n(eligible, settings.pipeline.top_n))

    interval = settings.pipeline.screener_timeframes[0]  # primary - styr evidence-gating (§8.1)
    secondary_interval = (
        settings.pipeline.screener_timeframes[1]
        if len(settings.pipeline.screener_timeframes) > 1
        else None
    )
    klines: dict[str, list[Kline]] = {}
    funding_rates: dict[str, list[FundingRate]] = {}
    secondary_klines: dict[str, list[Kline]] = {}
    secondary_funding_rates: dict[str, list[FundingRate]] = {}
    data_quality_status: dict[str, DataQualityResult] = {}

    for symbol in top_n_symbols:
        try:
            kline_completeness, parsed_klines, kline_staleness = _fetch_klines_with_retry(
                connector,
                symbol,
                interval,
                settings.pipeline.screener_lookback_periods + 5,
                settings.pipeline.required_fields["kline"],
                settings.pipeline.max_data_age_seconds["kline"],
                fetch_clock,
                settings.pipeline.bingx_max_retries,
                sleep_fn,
                run_id,
            )
            klines[symbol] = parsed_klines
            kline_consistency = (
                check_kline_consistency(
                    parsed_klines, settings.pipeline.kline_consistency_tolerance_pct
                )
                if parsed_klines
                else "invalid"
            )

            funding_completeness, parsed_funding, funding_staleness = _fetch_funding_with_retry(
                connector,
                symbol,
                settings.pipeline.screener_funding_history_limit,
                settings.pipeline.required_fields["funding_rate"],
                settings.pipeline.max_data_age_seconds["funding_rate"],
                fetch_clock,
                settings.pipeline.bingx_max_retries,
                sleep_fn,
                run_id,
            )
            funding_rates[symbol] = parsed_funding

            raw_oi = connector.get_open_interest(symbol)
            oi_fetch_time = fetch_clock()
            oi_completeness = check_completeness(
                raw_oi, settings.pipeline.required_fields["open_interest"]
            )
            if oi_completeness == "ok":
                oi = OpenInterest.from_raw(raw_oi)
                oi_staleness = check_staleness(
                    oi.observed_at,
                    oi_fetch_time,
                    settings.pipeline.max_data_age_seconds["open_interest"],
                )
            else:
                oi_staleness = "invalid"

            data_quality_status[symbol] = classify(
                ticker_dq[symbol],
                kline_completeness,
                kline_staleness,
                kline_consistency,
                funding_completeness,
                funding_staleness,
                oi_completeness,
                oi_staleness,
            )
        except ConnectorUnavailableError:
            # Samma kontrakt som ticker-loopen ovan: ett enskilt instruments
            # fel (klines/funding/open interest) klassas DATA_INVALID för
            # just den symbolen, aldrig en krasch av hela ticken.
            klines[symbol] = []
            funding_rates[symbol] = []
            data_quality_status[symbol] = "invalid"

        # Sekundär timeframe (beslut 2026-08-29, "primary triggers, secondary
        # confirms"): en EGEN try/except, helt frikopplad från primary-blocket
        # ovan - ett fel här får ALDRIG påverka data_quality_status, klines
        # eller tickers för symbolen (secondary är rent bekräftande, aldrig
        # gatande, se schemas/evidence.py::SecondaryTimeframeEvidence).
        secondary_klines[symbol] = []
        secondary_funding_rates[symbol] = []
        if secondary_interval is not None:
            try:
                raw_secondary_klines = connector.get_klines(
                    symbol,
                    secondary_interval,
                    limit=settings.pipeline.screener_lookback_periods + 5,
                )
                secondary_kline_completeness = (
                    classify(
                        *(
                            check_completeness(raw, settings.pipeline.required_fields["kline"])
                            for raw in raw_secondary_klines
                        )
                    )
                    if raw_secondary_klines
                    else "invalid"
                )
                if secondary_kline_completeness == "ok":
                    secondary_klines[symbol] = [
                        Kline.from_raw(k, symbol, secondary_interval) for k in raw_secondary_klines
                    ]

                raw_secondary_funding = connector.get_funding_rate(
                    symbol, limit=settings.pipeline.screener_funding_history_limit
                )
                secondary_funding_completeness = (
                    classify(
                        *(
                            check_completeness(
                                raw, settings.pipeline.required_fields["funding_rate"]
                            )
                            for raw in raw_secondary_funding
                        )
                    )
                    if raw_secondary_funding
                    else "invalid"
                )
                if secondary_funding_completeness == "ok":
                    secondary_funding_rates[symbol] = [
                        FundingRate.from_raw(f) for f in raw_secondary_funding
                    ]
            except ConnectorUnavailableError:
                pass  # secondary_klines/secondary_funding_rates redan [] ovan

    return MarketSnapshot(
        simulated_now=now,
        instruments=instruments,
        tickers=tickers,
        klines=klines,
        funding_rates=funding_rates,
        secondary_klines=secondary_klines,
        secondary_funding_rates=secondary_funding_rates,
        data_quality_status=data_quality_status
        | {
            s: "invalid"
            for s in instruments
            if s not in top_n_symbols and s not in data_quality_status
        },
    )
