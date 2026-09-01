from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    DashboardConfig,
    NotifyConfig,
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
        notify=NotifyConfig(notification_level="important", notify_interval_seconds=60),
        dashboard=DashboardConfig(host="127.0.0.1", port=8000),
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

    def get_all_tickers(self):
        return list(self._tickers.values())

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


class _StaleThenFreshKlinesConnector(_StubConnector):
    """Reproducerar 2026-08-31-fyndet (del 2): get_klines() kan returnera
    genuint gammal data för en enskild symbol vid ETT anrop, sedan färsk
    data vid nästa - bekräftat mot riktig BingX-data (BTC-USDT, samma
    parametrar, 2 minuters mellanrum: en gång 24h gammal, en gång 20
    minuter gammal). `stale_call_count` styr hur MÅNGA av de FAKTISKA
    anropen som ger den gamla datan innan den friska datan returneras -
    varje anrop är genuint separat (klines_call_count räknar), aldrig en
    omtolkning av samma svar."""

    def __init__(self, *args, stale_klines_raw, fresh_klines_raw, stale_call_count, **kwargs):
        super().__init__(*args, **kwargs)
        self._stale_klines_raw = stale_klines_raw
        self._fresh_klines_raw = fresh_klines_raw
        self._stale_call_count = stale_call_count
        self.klines_call_count = 0

    def get_klines(self, symbol, interval, limit=100):
        self.klines_call_count += 1
        self.klines_calls.append(symbol)
        self.klines_interval_used = interval
        self.klines_calls_by_interval.setdefault(interval, []).append(symbol)
        source = (
            self._stale_klines_raw
            if self.klines_call_count <= self._stale_call_count
            else self._fresh_klines_raw
        )
        return source[-limit:]


class _StaleThenFreshFundingConnector(_StubConnector):
    """Samma reproduktion som _StaleThenFreshKlinesConnector ovan, för
    get_funding_rate()."""

    def __init__(self, *args, stale_funding_raw, fresh_funding_raw, stale_call_count, **kwargs):
        super().__init__(*args, **kwargs)
        self._stale_funding_raw = stale_funding_raw
        self._fresh_funding_raw = fresh_funding_raw
        self._stale_call_count = stale_call_count
        self.funding_call_count = 0

    def get_funding_rate(self, symbol, limit=1):
        self.funding_call_count += 1
        source = (
            self._stale_funding_raw
            if self.funding_call_count <= self._stale_call_count
            else self._fresh_funding_raw
        )
        return source[-limit:]


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


def test_build_live_snapshot_ticker_staleness_uses_a_fresh_per_item_clock_not_the_batch_start_now():
    """Bug reproducerad 2026-08-31 (riktig live-körning mot BingX): en
    sekventiell hämtningsloop över hela instrumentuniversumet (1132
    instrument) tog ~1152s att slutföra, långt mer än
    max_data_age_seconds["ticker"] (30s). Med staleness bedömd mot ETT
    `now` fångat FÖRE hela loopen blev en ticker vars closeTime korrekt
    speglar tiden DEN FAKTISKT HÄMTADES (klart senare än batch-start-`now`)
    felaktigt klassad 'invalid' - inte för att datan är gammal, utan för
    att den är "för färsk" relativt en klocka som redan hunnit bli
    inaktuell (overskrider _FUTURE_TIMESTAMP_GRACE_SECONDS=5s redan efter
    några sekunders loop-drift). Fixen: staleness bedöms mot ett eget,
    färskt timestamp per ticker (injicerat via `clock`), inte mot
    batch-start-`now`."""
    contracts = [_raw_contract("BTCUSDT")]
    # Simulerar: loopen har redan pågått 600s när just den här tickern
    # faktiskt hämtas - closeTime speglar den verkliga hämtningstidpunkten.
    late_fetch_instant = _NOW + timedelta(seconds=600)
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(late_fetch_instant))}
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(late_fetch_instant - timedelta(hours=1))),
            _raw_kline("50100", _ms(late_fetch_instant)),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(late_fetch_instant))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(late_fetch_instant))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    snapshot = build_live_snapshot(
        connector, _settings(top_n=1), _NOW, clock=lambda: late_fetch_instant
    )

    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert "BTCUSDT" in snapshot.tickers


def test_build_live_snapshot_kline_funding_oi_staleness_uses_a_fresh_per_item_clock():
    """Samma buggklass som ticker-testet ovan, i top_n-loopen (kline/
    funding/open interest) - en sen top_n-symbols hämtning ska inte
    straffas mot batch-start-`now`, precis som ticker-fallet."""
    contracts = [_raw_contract("BTCUSDT")]
    late_fetch_instant = _NOW + timedelta(seconds=600)
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(late_fetch_instant))}
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(late_fetch_instant - timedelta(hours=1))),
            _raw_kline("50100", _ms(late_fetch_instant)),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(late_fetch_instant))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(late_fetch_instant))}
    connector = _StubConnector(contracts, tickers, klines, funding_rates, open_interest)

    snapshot = build_live_snapshot(
        connector, _settings(top_n=1), _NOW, clock=lambda: late_fetch_instant
    )

    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert len(snapshot.klines["BTCUSDT"]) > 0


def test_build_live_snapshot_default_clock_preserves_old_behavior_when_not_overridden():
    """Bakåtkompatibilitet: anropare som inte skickar `clock` (alla
    existerande anropsplatser/tester innan denna fix) ska få EXAKT samma
    beteende som innan - staleness bedöms mot det redan passerade `now`,
    aldrig mot den verkliga systemklockan (som skulle göra varje test med
    ett fast historiskt `_NOW` trasigt)."""
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


def test_build_live_snapshot_retries_stale_klines_and_succeeds_on_a_fresher_attempt():
    """Bug reproducerad 2026-08-31 (del 2, riktig live-körning mot BingX):
    get_klines() kan intermittent returnera genuint gammal data - bekräftat
    med BTC-USDT, samma parametrar, 2 minuters mellanrum: en gång 24h
    gammal, en gång 20 minuter gammal. Ticker/open interest visar aldrig
    detta. Fixen: ett nytt, RIKTIGT get_klines()-anrop (aldrig omtolkning
    av samma svar) görs om staleness misslyckas, upp till
    `bingx_max_retries` gånger."""
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    stale_klines_raw = [_raw_kline("50000", _ms(_NOW - timedelta(hours=30)))]
    fresh_klines_raw = [
        _raw_kline("50000", _ms(_NOW - timedelta(hours=1))),
        _raw_kline("50100", _ms(_NOW)),
    ]
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StaleThenFreshKlinesConnector(
        contracts,
        tickers,
        funding_rates=funding_rates,
        open_interest=open_interest,
        stale_klines_raw=stale_klines_raw,
        fresh_klines_raw=fresh_klines_raw,
        stale_call_count=1,
    )

    snapshot = build_live_snapshot(
        connector, _settings(top_n=1), _NOW, clock=lambda: _NOW, sleep_fn=lambda seconds: None
    )

    # Bevisar att retry gjorde ett NYTT faktiskt anrop, inte en omtolkning
    # av samma stale svar - exakt 2 anrop: första (stale) + andra (fresh).
    assert connector.klines_call_count == 2
    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert len(snapshot.klines["BTCUSDT"]) == 2  # den friska datan, inte den gamla


def test_build_live_snapshot_marks_invalid_and_logs_when_klines_stay_stale_after_all_retries(
    monkeypatch,
):
    """Retries är begränsade (aldrig oändliga) och ger tydligt besked när
    de förbrukas: symbolen förblir korrekt `invalid` (fail-closed, SPEC
    §8.3) och en explicit händelse loggas."""
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    always_stale_klines_raw = [_raw_kline("50000", _ms(_NOW - timedelta(hours=30)))]
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StaleThenFreshKlinesConnector(
        contracts,
        tickers,
        funding_rates=funding_rates,
        open_interest=open_interest,
        stale_klines_raw=always_stale_klines_raw,
        fresh_klines_raw=always_stale_klines_raw,  # blir aldrig fräsch
        stale_call_count=999,
    )
    logged_events: list[dict] = []
    monkeypatch.setattr(
        "crypto_trading.market_snapshot.log_event",
        lambda run_id, **fields: logged_events.append({"run_id": run_id, **fields}),
    )

    snapshot = build_live_snapshot(
        connector,
        _settings(top_n=1),
        _NOW,
        clock=lambda: _NOW,
        sleep_fn=lambda seconds: None,
        run_id="test-run-1",
    )

    # Begränsat, aldrig oändligt - exakt settings.pipeline.bingx_max_retries (3) anrop.
    assert connector.klines_call_count == 3
    assert snapshot.data_quality_status["BTCUSDT"] == "invalid"
    exhausted_events = [
        e for e in logged_events if e["event"] == "kline_staleness_retries_exhausted"
    ]
    assert len(exhausted_events) == 1
    assert exhausted_events[0]["run_id"] == "test-run-1"
    assert exhausted_events[0]["symbol"] == "BTCUSDT"


def test_build_live_snapshot_retries_stale_funding_and_succeeds_on_a_fresher_attempt():
    """Samma reproduktion som klines-testet ovan, för get_funding_rate()."""
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(_NOW))}
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(_NOW - timedelta(hours=1))),
            _raw_kline("50100", _ms(_NOW)),
        ]
    }
    stale_funding_raw = [_raw_funding("BTCUSDT", "0.0001", _ms(_NOW - timedelta(days=3)))]
    fresh_funding_raw = [_raw_funding("BTCUSDT", "0.0002", _ms(_NOW))]
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(_NOW))}
    connector = _StaleThenFreshFundingConnector(
        contracts,
        tickers,
        klines=klines,
        open_interest=open_interest,
        stale_funding_raw=stale_funding_raw,
        fresh_funding_raw=fresh_funding_raw,
        stale_call_count=1,
    )

    snapshot = build_live_snapshot(
        connector, _settings(top_n=1), _NOW, clock=lambda: _NOW, sleep_fn=lambda seconds: None
    )

    assert connector.funding_call_count == 2
    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert len(snapshot.funding_rates["BTCUSDT"]) == 1
    assert snapshot.funding_rates["BTCUSDT"][0].funding_rate == Decimal("0.0002")


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
    Constraints, SPEC §8.3) kan fånga och logga det på tick-nivå.

    Bulk-ticker-fixen (2026-09-01): get_all_tickers() är EN gemensam
    hämtning för hela universumet, inte längre en per-symbol-loop - ett fel
    här är strukturellt samma "hela endpointen"-kategori som ett
    get_contracts()-fel (medvetet ofångat, se build_live_snapshot()), inte
    ett enskilt instruments fel. Testet flyttas därför till get_all_tickers()
    i stället för det gamla, nu obefintliga per-symbol get_ticker()-anropet."""
    contracts = [_raw_contract("BADUSDT"), _raw_contract("BTCUSDT")]

    class _AllTickersFailingConnector(_StubConnector):
        def get_all_tickers(self):
            raise RuntimeError("genuint oväntad programmeringsbugg")

    connector = _AllTickersFailingConnector(contracts, tickers={})

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
