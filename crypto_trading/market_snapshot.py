from __future__ import annotations

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
    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]: ...
    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]: ...
    def get_open_interest(self, symbol: str) -> dict: ...


def build_live_snapshot(
    connector: LiveMarketDataSource, settings: Settings, now: datetime
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
    """
    contracts_raw = connector.get_contracts()
    instruments = {c["symbol"]: InstrumentMetadata.from_raw(c, now) for c in contracts_raw}

    tickers: dict[str, Ticker] = {}
    ticker_dq: dict[str, DataQualityResult] = {}
    for symbol in instruments:
        try:
            raw_ticker = connector.get_ticker(symbol)
        except ConnectorUnavailableError:
            # SPEC §8.2/ConnectorUnavailableError-kontraktet: ett enskilt
            # instruments fel (t.ex. en pausad symbol på BingX, AC3
            # 2026-08-28) klassas som DATA_INVALID för det instrumentet,
            # aldrig en krasch av hela ticken - övriga instrument fortsätter
            # obehindrat, samma princip som redan gäller monitoring_loop.py.
            ticker_dq[symbol] = "invalid"
            continue
        completeness = check_completeness(raw_ticker, settings.pipeline.required_fields["ticker"])
        if completeness == "invalid":
            ticker_dq[symbol] = "invalid"
            continue
        ticker = Ticker.from_raw(raw_ticker)
        staleness = check_staleness(
            ticker.observed_at, now, settings.pipeline.max_data_age_seconds["ticker"]
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

    interval = settings.pipeline.screener_timeframes[0]  # Beslut 5 - inte löst här
    klines: dict[str, list[Kline]] = {}
    funding_rates: dict[str, list[FundingRate]] = {}
    data_quality_status: dict[str, DataQualityResult] = {}

    for symbol in top_n_symbols:
        try:
            raw_klines = connector.get_klines(
                symbol, interval, limit=settings.pipeline.screener_lookback_periods + 5
            )
            kline_completeness = (
                classify(
                    *(
                        check_completeness(raw, settings.pipeline.required_fields["kline"])
                        for raw in raw_klines
                    )
                )
                if raw_klines
                else "invalid"
            )
            parsed_klines = (
                [Kline.from_raw(k, symbol, interval) for k in raw_klines]
                if kline_completeness == "ok"
                else []
            )
            klines[symbol] = parsed_klines
            kline_staleness = (
                check_staleness(
                    parsed_klines[-1].observed_at,
                    now,
                    settings.pipeline.max_data_age_seconds["kline"],
                )
                if parsed_klines
                else "invalid"
            )
            kline_consistency = (
                check_kline_consistency(
                    parsed_klines, settings.pipeline.kline_consistency_tolerance_pct
                )
                if parsed_klines
                else "invalid"
            )

            raw_funding = connector.get_funding_rate(
                symbol, limit=settings.pipeline.screener_funding_history_limit
            )
            funding_completeness = (
                classify(
                    *(
                        check_completeness(raw, settings.pipeline.required_fields["funding_rate"])
                        for raw in raw_funding
                    )
                )
                if raw_funding
                else "invalid"
            )
            parsed_funding = (
                [FundingRate.from_raw(f) for f in raw_funding]
                if funding_completeness == "ok"
                else []
            )
            funding_rates[symbol] = parsed_funding
            funding_staleness = (
                check_staleness(
                    parsed_funding[-1].observed_at,
                    now,
                    settings.pipeline.max_data_age_seconds["funding_rate"],
                )
                if parsed_funding
                else "invalid"
            )

            raw_oi = connector.get_open_interest(symbol)
            oi_completeness = check_completeness(
                raw_oi, settings.pipeline.required_fields["open_interest"]
            )
            if oi_completeness == "ok":
                oi = OpenInterest.from_raw(raw_oi)
                oi_staleness = check_staleness(
                    oi.observed_at, now, settings.pipeline.max_data_age_seconds["open_interest"]
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

    return MarketSnapshot(
        simulated_now=now,
        instruments=instruments,
        tickers=tickers,
        klines=klines,
        funding_rates=funding_rates,
        data_quality_status=data_quality_status
        | {
            s: "invalid"
            for s in instruments
            if s not in top_n_symbols and s not in data_quality_status
        },
    )
