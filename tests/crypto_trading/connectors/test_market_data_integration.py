from datetime import UTC, datetime, timedelta
from decimal import Decimal

import respx
from httpx import Response

from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.connectors.data_quality import check_completeness, check_staleness, classify
from crypto_trading.schemas.market import Ticker

_BASE_URL = "https://open-api.bingx.com"
_REQUIRED_TICKER_FIELDS = ["lastPrice", "askPrice", "bidPrice", "quoteVolume", "closeTime"]


@respx.mock
def test_fresh_ticker_flows_through_fetch_map_classify_as_ok():
    fresh_close_time_ms = int(datetime.now(UTC).timestamp() * 1000)
    raw_ticker = {
        "symbol": "BTC-USDT", "lastPrice": "77955.4", "askPrice": "77993.5",
        "bidPrice": "77993.4", "quoteVolume": "1306179101.75",
        "priceChange": "0", "priceChangePercent": "0", "highPrice": "0", "lowPrice": "0",
        "volume": "0", "openPrice": "0", "openTime": fresh_close_time_ms,
        "closeTime": fresh_close_time_ms, "askQty": "1", "bidQty": "1",
    }
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": raw_ticker})
    )
    connector = BingXMarketDataConnector(
        base_url=_BASE_URL, timeout_seconds=5, max_retries=1,
        requests_per_second=1000, cache_ttl_seconds=0,
    )

    raw = connector.get_ticker("BTC-USDT")
    completeness = check_completeness(raw, required_fields=_REQUIRED_TICKER_FIELDS)
    ticker = Ticker.from_raw(raw)
    staleness = check_staleness(ticker.observed_at, datetime.now(UTC), max_age_seconds=30)
    overall = classify(completeness, staleness)

    assert isinstance(ticker.last_price, Decimal)
    assert overall == "ok"


@respx.mock
def test_stale_ticker_flows_through_as_invalid_never_silently_ok():
    stale_close_time_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    raw_ticker = {
        "symbol": "BTC-USDT", "lastPrice": "77955.4", "askPrice": "77993.5",
        "bidPrice": "77993.4", "quoteVolume": "1306179101.75",
        "priceChange": "0", "priceChangePercent": "0", "highPrice": "0", "lowPrice": "0",
        "volume": "0", "openPrice": "0", "openTime": stale_close_time_ms,
        "closeTime": stale_close_time_ms, "askQty": "1", "bidQty": "1",
    }
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": raw_ticker})
    )
    connector = BingXMarketDataConnector(
        base_url=_BASE_URL, timeout_seconds=5, max_retries=1,
        requests_per_second=1000, cache_ttl_seconds=0,
    )

    raw = connector.get_ticker("BTC-USDT")
    completeness = check_completeness(raw, required_fields=_REQUIRED_TICKER_FIELDS)
    ticker = Ticker.from_raw(raw)
    staleness = check_staleness(ticker.observed_at, datetime.now(UTC), max_age_seconds=30)
    overall = classify(completeness, staleness)

    assert overall == "invalid"  # 1 timme gammal ticker, max_age_seconds=30 - aldrig "ok"


@respx.mock
def test_incomplete_ticker_is_invalid_before_even_reaching_pydantic():
    raw_ticker = {"symbol": "BTC-USDT", "lastPrice": "77955.4"}  # saknar askPrice/bidPrice/etc
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": raw_ticker})
    )
    connector = BingXMarketDataConnector(
        base_url=_BASE_URL, timeout_seconds=5, max_retries=1,
        requests_per_second=1000, cache_ttl_seconds=0,
    )

    raw = connector.get_ticker("BTC-USDT")
    completeness = check_completeness(raw, required_fields=_REQUIRED_TICKER_FIELDS)

    assert completeness == "invalid"
