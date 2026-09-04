from datetime import UTC, datetime

from crypto_trading.guardian.data import fetch_btc_regime_rsi, fetch_current_price, fetch_fresh_evidence
from tests.crypto_trading.test_market_snapshot import _raw_funding, _raw_kline, _raw_ticker, _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _StubConnector:
    def __init__(self, klines=None, funding_rates=None, tickers=None):
        self._klines = klines or {}
        self._funding_rates = funding_rates or {}
        self._tickers = tickers or {}

    def get_klines(self, symbol, interval, limit=100):
        return self._klines.get(symbol, [])[-limit:]

    def get_funding_rate(self, symbol, limit=1):
        return self._funding_rates.get(symbol, [])[-limit:]

    def get_ticker(self, symbol):
        return self._tickers[symbol]


def _klines_series(n, base_price=100.0, symbol="BTCUSDT"):
    # Timestamps count BACKWARD from _NOW, ending exactly at _NOW - the
    # evidence builders reject any kline dated after evaluated_at (SPEC
    # §8.4 no-future-data guard), so a realistic series must never be
    # dated in the future relative to `now`.
    return [
        _raw_kline(str(base_price + i), int(_NOW.timestamp() * 1000) - (n - 1 - i) * 60000)
        for i in range(n)
    ]


def test_fetch_fresh_evidence_returns_none_on_insufficient_klines():
    connector = _StubConnector(
        klines={"BTCUSDT": _klines_series(2)},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", int(_NOW.timestamp() * 1000))]},
    )

    result = fetch_fresh_evidence(connector, "BTCUSDT", None, _settings(), _NOW)

    assert result is None


def test_fetch_fresh_evidence_returns_a_record_with_enough_data():
    connector = _StubConnector(
        klines={"BTCUSDT": _klines_series(30)},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", int(_NOW.timestamp() * 1000))]},
    )

    result = fetch_fresh_evidence(connector, "BTCUSDT", None, _settings(), _NOW)

    assert result is not None
    assert result.instrument == "BTCUSDT"
    assert result.secondary_timeframe_evidence is None


def test_fetch_btc_regime_rsi_returns_none_on_insufficient_data():
    connector = _StubConnector(klines={"BTC-USDT": _klines_series(2, symbol="BTC-USDT")})

    assert fetch_btc_regime_rsi(connector, _settings(), _NOW) is None


def test_fetch_btc_regime_rsi_returns_a_value_with_enough_data():
    connector = _StubConnector(
        klines={"BTC-USDT": _klines_series(30, symbol="BTC-USDT")},
        funding_rates={"BTC-USDT": [_raw_funding("BTC-USDT", "0.0001", int(_NOW.timestamp() * 1000))]},
    )

    result = fetch_btc_regime_rsi(connector, _settings(), _NOW)

    assert result is not None


def test_fetch_current_price_reads_last_price():
    connector = _StubConnector(tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "55000", "1000000", int(_NOW.timestamp() * 1000))})

    price = fetch_current_price(connector, "BTCUSDT")

    assert price == 55000
