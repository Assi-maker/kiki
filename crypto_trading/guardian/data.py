from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.schemas.evidence import CandidateEvidenceRecord
from crypto_trading.schemas.market import FundingRate, Kline, Ticker
from crypto_trading.screening.quant_screener import build_momentum_breakout_evidence, evaluate_candidate

_BTC_INSTRUMENT = "BTC-USDT"


class GuardianDataSource(Protocol):
    def get_ticker(self, symbol: str) -> dict: ...
    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]: ...
    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]: ...


def _min_klines_required(settings: Settings) -> int:
    return max(
        settings.pipeline.screener_lookback_periods + 2,
        settings.pipeline.screener_rsi_period + 1,
    )


def _fetch_klines_and_funding(
    connector: GuardianDataSource, instrument: str, interval: str, settings: Settings
) -> tuple[list[Kline], list[FundingRate]] | None:
    try:
        raw_klines = connector.get_klines(instrument, interval, limit=100)
        raw_funding = connector.get_funding_rate(
            instrument, limit=settings.pipeline.screener_funding_history_limit
        )
    except ConnectorUnavailableError:
        return None
    if len(raw_klines) < _min_klines_required(settings) or not raw_funding:
        return None
    klines = sorted(
        (Kline.from_raw(k, instrument, interval) for k in raw_klines), key=lambda k: k.observed_at
    )
    funding = sorted(
        (FundingRate.from_raw(f) for f in raw_funding), key=lambda f: f.observed_at
    )
    return klines, funding


def fetch_fresh_evidence(
    connector: GuardianDataSource,
    instrument: str,
    secondary_timeframe: str | None,
    settings: Settings,
    now: datetime,
) -> CandidateEvidenceRecord | None:
    """Fail-safe: any missing/insufficient data returns None (skip this
    position this tick, never a guess) - same principle as
    monitoring_loop.py's per-instrument skip. Reuses evaluate_candidate()
    end-to-end with the same thresholds used at entry, so fresh and
    entry-time evidence stay directly comparable."""
    primary_interval = settings.pipeline.screener_timeframes[0]
    primary = _fetch_klines_and_funding(connector, instrument, primary_interval, settings)
    if primary is None:
        return None
    primary_klines, primary_funding = primary

    secondary_klines: list[Kline] | None = None
    secondary_funding: list[FundingRate] | None = None
    if secondary_timeframe is not None:
        secondary = _fetch_klines_and_funding(connector, instrument, secondary_timeframe, settings)
        if secondary is not None:
            secondary_klines, secondary_funding = secondary

    return evaluate_candidate(
        instrument=instrument,
        timeframes=[primary_interval],
        klines=primary_klines,
        funding_rates=primary_funding,
        data_quality_status="ok",
        evaluated_at=now,
        price_volatility_threshold_pct=settings.pipeline.screener_price_volatility_threshold_pct,
        lookback=settings.pipeline.screener_lookback_periods,
        rsi_period=settings.pipeline.screener_rsi_period,
        rsi_overbought_threshold=settings.pipeline.screener_rsi_overbought_threshold,
        volume_zscore_threshold=settings.pipeline.screener_volume_zscore_threshold,
        funding_rate_threshold_pct=settings.pipeline.screener_funding_rate_threshold_pct,
        secondary_timeframe=secondary_timeframe,
        secondary_klines=secondary_klines,
        secondary_funding_rates=secondary_funding,
    )


def fetch_btc_regime_rsi(connector: GuardianDataSource, settings: Settings, now: datetime) -> Decimal | None:
    primary_interval = settings.pipeline.screener_timeframes[0]
    fetched = _fetch_klines_and_funding(connector, _BTC_INSTRUMENT, primary_interval, settings)
    if fetched is None:
        return None
    klines, _funding = fetched
    evidence = build_momentum_breakout_evidence(
        klines, settings.pipeline.screener_rsi_period, settings.pipeline.screener_rsi_overbought_threshold, now
    )
    return Decimal(str(evidence.value))


def fetch_current_price(connector: GuardianDataSource, instrument: str) -> Decimal | None:
    try:
        ticker = Ticker.from_raw(connector.get_ticker(instrument))
    except (ConnectorUnavailableError, KeyError, ValueError):
        return None
    return ticker.last_price
