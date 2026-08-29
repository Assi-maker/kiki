from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    PipelineConfig,
    RiskLimitsConfig,
    Settings,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
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
        self.klines_calls_by_interval: dict[str, list[str]] = {}

    def get_contracts(self):
        return self._contracts

    def get_ticker(self, symbol):
        return self._tickers[symbol]

    def get_klines(self, symbol, interval, limit=100):
        self.klines_calls.append(symbol)
        self.klines_interval_used = interval
        self.klines_calls_by_interval.setdefault(interval, []).append(symbol)
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


class _TickerFailingConnector(_StubConnector):
    """AC3-live-körningen 2026-08-28 kraschade hela discovery-ticken när
    BingX svarade med ett API-fel (kod 109415, en pausad symbol) för EN
    symbol bland hela instrumentuniversumet - build_live_snapshot() hade
    ingen per-symbol-isolering runt get_ticker(), till skillnad från det
    redan etablerade mönstret i monitoring_loop.py."""

    def __init__(self, *args, fail_symbol: str, exc: Exception, **kwargs):
        super().__init__(*args, **kwargs)
        self._fail_symbol = fail_symbol
        self._exc = exc

    def get_ticker(self, symbol):
        if symbol == self._fail_symbol:
            raise self._exc
        return super().get_ticker(symbol)


class _KlinesFailingConnector(_StubConnector):
    """Samma AC3-regression, andra loopen (klines/funding/open interest för
    top_n-symbolerna) - lika oskyddad mot ett enskilt instruments fel."""

    def __init__(self, *args, fail_symbol: str, exc: Exception, **kwargs):
        super().__init__(*args, **kwargs)
        self._fail_symbol = fail_symbol
        self._exc = exc
        self.funding_calls: list[str] = []

    def get_klines(self, symbol, interval, limit=100):
        if symbol == self._fail_symbol:
            raise self._exc
        return super().get_klines(symbol, interval, limit)

    def get_funding_rate(self, symbol, limit=1):
        self.funding_calls.append(symbol)
        return super().get_funding_rate(symbol, limit)


class _SecondaryIntervalFailingConnector(_StubConnector):
    """En sekundär-timeframe-hämtning (t.ex. 4h) som fallerar ska aldrig
    påverka primary-datan eller symbolens data_quality_status - secondary är
    rent bekräftande (beslut 2026-08-29 "primary triggers, secondary
    confirms"), aldrig gatande."""

    def __init__(self, *args, fail_interval: str, exc: Exception, **kwargs):
        super().__init__(*args, **kwargs)
        self._fail_interval = fail_interval
        self._exc = exc

    def get_klines(self, symbol, interval, limit=100):
        if interval == self._fail_interval:
            raise self._exc
        return super().get_klines(symbol, interval, limit)


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


def test_build_live_snapshot_skips_a_symbol_whose_ticker_raises_connector_unavailable():
    """AC3-regression: en ConnectorUnavailableError för ETT instruments
    ticker-hämtning (t.ex. en pausad symbol på BingX) ska markera bara det
    instrumentet 'invalid' och aldrig abortera resten av snapshoten."""
    contracts = [_raw_contract("BADUSDT"), _raw_contract("BTCUSDT")]
    tickers = {
        "BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW)),
    }
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(_NOW - timedelta(hours=2))),
            _raw_kline("50100", _ms(_NOW - timedelta(hours=1))),
            _raw_kline("50200", _ms(_NOW)),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _TickerFailingConnector(
        contracts,
        tickers,
        klines,
        funding_rates,
        open_interest,
        fail_symbol="BADUSDT",
        exc=ConnectorUnavailableError("BingX API-fel 109415: BADUSDT is pause currently"),
    )

    snapshot = build_live_snapshot(connector, _settings(top_n=2), _NOW)

    assert snapshot.data_quality_status["BADUSDT"] == "invalid"
    assert "BADUSDT" not in snapshot.tickers
    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert "BTCUSDT" in snapshot.tickers
    assert connector.klines_calls == ["BTCUSDT"]  # BADUSDT nådde aldrig Top N


def test_build_live_snapshot_skips_a_symbol_whose_klines_raise_connector_unavailable():
    """Samma regression, andra loopen: en ConnectorUnavailableError vid
    get_klines() för en top_n-symbol ska hoppa över HELA den symbolens
    återstående bearbetning (funding/open interest anropas aldrig för den),
    utan att påverka övriga top_n-symboler."""
    contracts = [_raw_contract("BADUSDT"), _raw_contract("BTCUSDT")]
    tickers = {
        "BADUSDT": _raw_ticker("BADUSDT", "10", "5000000", _ms(_NOW)),
        "BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW)),
    }
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(_NOW - timedelta(hours=2))),
            _raw_kline("50100", _ms(_NOW - timedelta(hours=1))),
            _raw_kline("50200", _ms(_NOW)),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _KlinesFailingConnector(
        contracts,
        tickers,
        klines,
        funding_rates,
        open_interest,
        fail_symbol="BADUSDT",
        exc=ConnectorUnavailableError("BingX API-fel: BADUSDT klines otillgängliga"),
    )

    snapshot = build_live_snapshot(connector, _settings(top_n=2), _NOW)

    assert snapshot.data_quality_status["BADUSDT"] == "invalid"
    assert snapshot.klines["BADUSDT"] == []
    assert "BADUSDT" not in connector.funding_calls  # hela symbolen hoppades över
    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert len(snapshot.klines["BTCUSDT"]) > 0


def test_build_live_snapshot_does_not_mask_unexpected_non_connector_errors():
    """Kravet är strikt avgränsat till ConnectorUnavailableError - ett
    genuint oväntat fel (programmeringsbugg, inte ett känt connector-fel)
    ska fortfarande propagera okontrollerat, precis som tidigare, så att
    discovery_loop.run_discovery_tick()s befintliga fail-safe (Global
    Constraints, SPEC §8.3) kan fånga och logga det på tick-nivå."""
    contracts = [_raw_contract("BADUSDT"), _raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    connector = _TickerFailingConnector(
        contracts,
        tickers,
        fail_symbol="BADUSDT",
        exc=RuntimeError("genuint oväntad programmeringsbugg"),
    )

    with pytest.raises(RuntimeError):
        build_live_snapshot(connector, _settings(top_n=2), _NOW)


def test_build_live_snapshot_uses_the_first_configured_screener_timeframe_as_primary():
    """Beslut 5 (2026-08-27), uppdaterat 2026-08-29 ("primary triggers,
    secondary confirms"): screener_timeframes[0] är alltjämt primary (den
    ENDA som styr klines[symbol]/data_quality_status/evidence-gating).
    Tidigare var detta test dokumentation av en olöst begränsning (bara 1h
    hämtades någonsin) - nu är det en kontraktsgaranti: primary-slotten är
    fortfarande [0], oavsett hur många timeframes som konfigureras."""
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    klines = {"BTCUSDT": [_raw_kline("50000", _ms(_NOW))]}
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    snapshot = build_live_snapshot(
        connector, _settings(top_n=1, screener_timeframes=["1h", "4h"]), _NOW
    )

    assert len(snapshot.klines["BTCUSDT"]) > 0
    assert snapshot.klines["BTCUSDT"][0].interval == "1h"


def test_build_live_snapshot_also_fetches_the_second_configured_timeframe_as_secondary():
    """Beslut 2026-08-29: när screener_timeframes har en andra timeframe
    hämtas den också (klines OCH funding), lagrad separat i secondary_klines/
    secondary_funding_rates - stänger Fas 5-luckan där 4h aldrig hämtades."""
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

    snapshot = build_live_snapshot(
        connector, _settings(top_n=1, screener_timeframes=["1h", "4h"]), _NOW
    )

    assert connector.klines_calls_by_interval["1h"] == ["BTCUSDT"]
    assert connector.klines_calls_by_interval["4h"] == ["BTCUSDT"]
    assert len(snapshot.secondary_klines["BTCUSDT"]) > 0
    assert snapshot.secondary_klines["BTCUSDT"][0].interval == "4h"
    assert len(snapshot.secondary_funding_rates["BTCUSDT"]) > 0


def test_build_live_snapshot_secondary_is_empty_when_only_one_timeframe_configured():
    """Bakåtkompatibilitet: en config med bara EN timeframe (t.ex. befintliga
    tester/miljöer) ska aldrig försöka hämta en sekundär serie."""
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    klines = {"BTCUSDT": [_raw_kline("50000", _ms(_NOW))]}
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    snapshot = build_live_snapshot(connector, _settings(top_n=1), _NOW)  # default: ["1h"] only

    assert snapshot.secondary_klines["BTCUSDT"] == []
    assert snapshot.secondary_funding_rates["BTCUSDT"] == []
    assert "4h" not in connector.klines_calls_by_interval


def test_build_live_snapshot_secondary_fetch_failure_does_not_affect_primary_or_data_quality():
    """Kärnkontraktet: secondary är rent bekräftande. Ett fel vid 4h-hämtning
    får aldrig göra symbolen invalid eller påverka klines[symbol]/tickers -
    bara secondary_klines[symbol] blir tom."""
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
    connector = _SecondaryIntervalFailingConnector(
        contracts,
        tickers,
        klines,
        funding_rates,
        open_interest,
        fail_interval="4h",
        exc=ConnectorUnavailableError("BingX API-fel: 4h klines otillgängliga"),
    )

    snapshot = build_live_snapshot(
        connector, _settings(top_n=1, screener_timeframes=["1h", "4h"]), _NOW
    )

    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert "BTCUSDT" in snapshot.tickers
    assert len(snapshot.klines["BTCUSDT"]) > 0
    assert snapshot.secondary_klines["BTCUSDT"] == []
