from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    PipelineConfig,
    RiskLimitsConfig,
    Settings,
)
from crypto_trading.market_snapshot import build_live_snapshot

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _settings(
    top_n: int = 5,
    screener_timeframes: list[str] | None = None,
    max_ai_calls_per_day: int = 500,
) -> Settings:
    return Settings(
        db_path="unused",
        pipeline=PipelineConfig(
            discovery_interval_minutes=15,
            monitoring_interval_seconds=30,
            top_n=top_n,
            cooldown_minutes=60,
            max_data_age_seconds={
                "ticker": 30,
                "kline": 120,
                "funding_rate": 3600,
                "open_interest": 300,
                "contracts": 86400,
            },
            min_sample_size_for_calibration=30,
            calibration_preliminary_sample_size=10,
            sqlite_busy_timeout_ms=5000,
            required_fields={
                "ticker": ["lastPrice", "askPrice", "bidPrice", "quoteVolume", "closeTime"],
                "kline": ["open", "high", "low", "close", "volume", "time"],
                "funding_rate": ["fundingRate", "fundingTime", "markPrice"],
                "open_interest": ["openInterest", "time"],
                "contracts": ["symbol", "status"],
            },
            screener_timeframes=screener_timeframes or ["1h"],
            bingx_base_url="https://open-api.bingx.com",
            bingx_requests_per_second=10,
            bingx_cache_ttl_seconds=5,
            bingx_max_retries=3,
            kline_consistency_tolerance_pct=Decimal("0.5"),
            eligibility_min_quote_volume_24h_usdt=Decimal("1000000"),
            eligibility_max_spread_pct=Decimal("0.01"),
            screener_lookback_periods=3,
            screener_price_volatility_threshold_pct=Decimal("2.0"),
            screener_rsi_period=3,
            screener_rsi_overbought_threshold=Decimal("70"),
            screener_volume_zscore_threshold=Decimal("2.5"),
            screener_funding_rate_threshold_pct=Decimal("0.05"),
            screener_funding_history_limit=10,
            evidence_change_threshold_for_reanalysis=Decimal("0.15"),
        ),
        risk_limits=RiskLimitsConfig(
            starting_capital_usdt=Decimal("10000"),
            risk_per_trade_pct=Decimal("0.01"),
            max_concurrent_positions=5,
            max_total_exposure_pct=Decimal("1.0"),
            spread_pct=Decimal("0.0005"),
            slippage_pct=Decimal("0.0005"),
            fee_pct=Decimal("0.0004"),
            max_position_hold_hours=24,
        ),
        budget_limits=BudgetLimitsConfig(
            max_candidates_per_discovery_run=10,
            max_ai_calls_per_discovery_run=70,
            max_ai_calls_per_day=max_ai_calls_per_day,
            warning_threshold_pct=Decimal("0.8"),
        ),
    )


class _StubConnector:
    def __init__(self, contracts, tickers, klines=None, funding_rates=None, open_interest=None):
        self._contracts = contracts
        self._tickers = tickers
        self._klines = klines or {}
        self._funding_rates = funding_rates or {}
        self._open_interest = open_interest or {}
        self.klines_calls: list[str] = []
        self.klines_interval_used: str | None = None

    def get_contracts(self):
        return self._contracts

    def get_ticker(self, symbol):
        return self._tickers[symbol]

    def get_klines(self, symbol, interval, limit=100):
        self.klines_calls.append(symbol)
        self.klines_interval_used = interval
        return self._klines[symbol][-limit:]

    def get_funding_rate(self, symbol, limit=1):
        return self._funding_rates[symbol][-limit:]

    def get_open_interest(self, symbol):
        return self._open_interest[symbol]


def _raw_contract(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "status": 1,
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "tradeMinUSDT": "2",
    }


def _raw_ticker(symbol: str, last_price: str, quote_volume: str, close_time_ms: int) -> dict:
    return {
        "symbol": symbol,
        "lastPrice": last_price,
        "priceChange": "0",
        "priceChangePercent": "0",
        "highPrice": last_price,
        "lowPrice": last_price,
        "volume": "100",
        "quoteVolume": quote_volume,
        "openPrice": last_price,
        "askPrice": last_price,
        "askQty": "1",
        "bidPrice": last_price,
        "bidQty": "1",
        "closeTime": close_time_ms,
    }


def _raw_kline(close: str, time_ms: int, high=None, low=None, volume="100") -> dict:
    return {
        "open": close,
        "high": high or close,
        "low": low or close,
        "close": close,
        "volume": volume,
        "time": time_ms,
    }


def _raw_funding(symbol: str, rate: str, time_ms: int) -> dict:
    return {"symbol": symbol, "fundingRate": rate, "fundingTime": time_ms, "markPrice": "50000"}


def _raw_open_interest(symbol: str, oi: str, time_ms: int) -> dict:
    return {"symbol": symbol, "openInterest": oi, "time": time_ms}


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_build_live_snapshot_produces_ok_quality_for_complete_fresh_data():
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(_NOW - timedelta(hours=2))),
            _raw_kline("50100", _ms(_NOW - timedelta(hours=1))),
            _raw_kline("50200", _ms(_NOW)),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    snapshot = build_live_snapshot(connector, _settings(top_n=1), _NOW)

    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert "BTCUSDT" in snapshot.tickers
    assert len(snapshot.klines["BTCUSDT"]) > 0


def test_build_live_snapshot_marks_invalid_when_a_ticker_field_is_missing():
    """Conflict-fix (2026-08-27): en genuint SAKNAD nyckel i rå-tickern (inte
    bara ett null-värde) - Ticker.from_raw() indexerar direkt utan .get(),
    så en verkligt saknad nyckel FÅR ALDRIG nå den parsern. check_completeness
    körs på rådatan innan någon .from_raw() ens övervägs."""
    contracts = [_raw_contract("BTCUSDT")]
    raw_ticker = _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))
    del raw_ticker["askPrice"]  # genuint saknad nyckel, inte None
    tickers = {"BTCUSDT": raw_ticker}
    connector = _StubConnector(contracts, tickers)

    snapshot = build_live_snapshot(connector, _settings(top_n=1), _NOW)

    assert snapshot.data_quality_status["BTCUSDT"] == "invalid"
    assert "BTCUSDT" not in snapshot.tickers  # kunde aldrig parsas - inte gissat/fabricerat
    assert connector.klines_calls == []  # nådde aldrig eligibility/Top N


def test_build_live_snapshot_marks_invalid_for_stale_kline():
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    stale_time = _NOW - timedelta(minutes=10)  # max_data_age_seconds["kline"] = 120s
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(stale_time - timedelta(hours=1))),
            _raw_kline("50100", _ms(stale_time)),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    snapshot = build_live_snapshot(connector, _settings(top_n=1), _NOW)

    assert snapshot.data_quality_status["BTCUSDT"] == "invalid"


def test_build_live_snapshot_only_fetches_klines_funding_oi_for_top_n_symbols():
    """Prestandagaranti: instrument som inte klarar eligibility (låg
    quote_volume) ska aldrig trigga ett get_klines/get_funding_rate/
    get_open_interest-anrop."""
    contracts = [_raw_contract("BTCUSDT"), _raw_contract("LOWVOLUSDT")]
    tickers = {
        "BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW)),
        "LOWVOLUSDT": _raw_ticker("LOWVOLUSDT", "1", "100", _ms(_NOW)),  # under tröskeln
    }
    klines = {"BTCUSDT": [_raw_kline("50000", _ms(_NOW))]}
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    build_live_snapshot(connector, _settings(top_n=1), _NOW)

    assert connector.klines_calls == ["BTCUSDT"]


def test_build_live_snapshot_uses_only_the_first_configured_screener_timeframe():
    """Beslut 5, dokumenterat som ett levande test."""
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    klines = {"BTCUSDT": [_raw_kline("50000", _ms(_NOW))]}
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    build_live_snapshot(connector, _settings(top_n=1, screener_timeframes=["1h", "4h"]), _NOW)

    assert connector.klines_interval_used == "1h"
