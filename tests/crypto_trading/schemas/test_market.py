from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.market import (
    FundingRate,
    InstrumentMetadata,
    Kline,
    OpenInterest,
    Ticker,
)

# Fixtures = riktiga svar, verifierade live mot https://open-api.bingx.com
# under Phase 1-brainstormingen (2026-08-25).

_RAW_CONTRACT = {
    "contractId": "100",
    "symbol": "BTC-USDT",
    "size": "0.0001",
    "quantityPrecision": 4,
    "pricePrecision": 1,
    "feeRate": 0.0005,
    "tradeMinUSDT": 2,
    "currency": "USDT",
    "asset": "BTC",
    "status": 1,
    "launchTime": 1586275200000,
    "displayName": "BTC-USDT",
}

_RAW_KLINE = {
    "open": "78162.6",
    "close": "77930.1",
    "high": "78260.0",
    "low": "77831.0",
    "volume": "361.4139",
    "time": 1787691600000,
}

_RAW_TICKER = {
    "symbol": "BTC-USDT",
    "priceChange": "-1019.8",
    "priceChangePercent": "-1.29",
    "lastPrice": "77955.4",
    "highPrice": "81263.0",
    "lowPrice": "77831.0",
    "volume": "16449.4100",
    "quoteVolume": "1306179101.75",
    "openPrice": "78975.2",
    "openTime": 1787605813000,
    "closeTime": 1787692213000,
    "askPrice": "77993.5",
    "askQty": "1.2853",
    "bidPrice": "77993.4",
    "bidQty": "24.1266",
}

_RAW_FUNDING_RATE = {
    "symbol": "BTC-USDT",
    "fundingRate": "0.00010000",
    "fundingTime": 1787673600000,
    "markPrice": "79463.4",
}

_RAW_OPEN_INTEREST = {
    "openInterest": "1100360743.1",
    "symbol": "BTC-USDT",
    "time": 1787692230396,
}


def test_instrument_metadata_from_raw_uses_decimal_not_float():
    fetched_at = datetime.now(UTC)
    instrument = InstrumentMetadata.from_raw(_RAW_CONTRACT, fetched_at=fetched_at)
    assert instrument.symbol == "BTC-USDT"
    assert instrument.status == 1
    assert instrument.trade_min_usdt == Decimal("2")
    assert isinstance(instrument.trade_min_usdt, Decimal)
    assert instrument.fetched_at == fetched_at


def test_kline_from_raw_maps_time_to_observed_at():
    kline = Kline.from_raw(_RAW_KLINE, instrument="BTC-USDT", interval="1h")
    assert kline.close == Decimal("77930.1")
    assert kline.high >= kline.low
    assert kline.observed_at == datetime.fromtimestamp(1787691600000 / 1000, tz=UTC)


def test_ticker_from_raw_maps_close_time_to_observed_at():
    ticker = Ticker.from_raw(_RAW_TICKER)
    assert ticker.instrument == "BTC-USDT"
    assert ticker.last_price == Decimal("77955.4")
    assert ticker.quote_volume == Decimal("1306179101.75")
    assert ticker.observed_at == datetime.fromtimestamp(1787692213000 / 1000, tz=UTC)


def test_funding_rate_from_raw():
    funding = FundingRate.from_raw(_RAW_FUNDING_RATE)
    assert funding.funding_rate == Decimal("0.00010000")
    assert funding.mark_price == Decimal("79463.4")
    assert funding.observed_at == datetime.fromtimestamp(1787673600000 / 1000, tz=UTC)


def test_open_interest_from_raw():
    oi = OpenInterest.from_raw(_RAW_OPEN_INTEREST)
    assert oi.open_interest == Decimal("1100360743.1")
    assert oi.observed_at == datetime.fromtimestamp(1787692230396 / 1000, tz=UTC)
