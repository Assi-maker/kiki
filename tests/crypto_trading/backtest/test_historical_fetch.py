import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from crypto_trading.backtest.historical_fetch import (
    fetch_historical_funding,
    fetch_historical_klines,
)
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector


def _connector() -> BingXMarketDataConnector:
    return BingXMarketDataConnector(
        base_url="https://open-api.bingx.com", timeout_seconds=5.0,
        max_retries=1, requests_per_second=1000, cache_ttl_seconds=0,
    )


def _raw_kline(close: str, time_ms: int) -> dict:
    return {"open": close, "high": close, "low": close, "close": close, "volume": "1", "time": time_ms}


@respx.mock
def test_fetch_historical_klines_single_call_for_a_24h_window(tmp_path):
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    route = respx.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "msg": "",
            "data": [_raw_kline("50000", int(start.timestamp() * 1000))],
        })
    )
    connector = _connector()

    klines = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)

    assert route.call_count == 1
    assert route.calls.last.request.url.params["limit"] == "1440"
    assert len(klines) == 1
    assert klines[0].instrument == "BTC-USDT"


@respx.mock
def test_fetch_historical_klines_paginates_beyond_24h(tmp_path):
    """A >24h window (an OPEN position replayed up to 'now') must issue
    more than one call, each still capped at limit=1440 (the real server
    limit verified live: limit>1440 -> code 109400)."""
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)  # 48h -> 2 pages of 1440

    def _responder(request):
        start_ms = int(request.url.params["startTime"])
        return httpx.Response(200, json={"code": 0, "msg": "", "data": [_raw_kline("1", start_ms)]})

    route = respx.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(side_effect=_responder)
    connector = _connector()

    klines = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)

    assert len(klines) == 2  # one kline returned per page in this stub
    assert route.call_count == 2  # exactly two network calls for 48h with 1440-candle limit
    assert route.calls[0].request.url.params["limit"] == "1440"
    assert route.calls[1].request.url.params["limit"] == "1440"
    # Verify boundary matching: call 2's startTime equals call 1's endTime (no gap/overlap)
    call1_end_ms = int(route.calls[0].request.url.params["endTime"])
    call2_start_ms = int(route.calls[1].request.url.params["startTime"])
    assert call2_start_ms == call1_end_ms


@respx.mock
def test_fetch_historical_klines_is_cached_on_second_call(tmp_path):
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    route = respx.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "msg": "", "data": [_raw_kline("50000", int(start.timestamp() * 1000))],
        })
    )
    connector = _connector()

    first = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)
    second = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)

    assert route.call_count == 1  # second call served entirely from cache
    assert first == second


@respx.mock
def test_fetch_historical_funding_uses_start_end_time(tmp_path):
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    respx.get("https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "msg": "",
            "data": [{"symbol": "BTC-USDT", "fundingRate": "0.0001",
                      "fundingTime": int(start.timestamp() * 1000), "markPrice": "50000"}],
        })
    )
    connector = _connector()

    rates = fetch_historical_funding(connector, "BTC-USDT", start, end, tmp_path)

    assert len(rates) == 1
    assert rates[0].funding_rate == Decimal("0.0001")
