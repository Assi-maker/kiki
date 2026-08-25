import time

import pytest
import respx
from httpx import Response, TimeoutException

from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_BASE_URL = "https://open-api.bingx.com"


def _connector(**overrides) -> BingXMarketDataConnector:
    defaults = dict(
        base_url=_BASE_URL,
        timeout_seconds=5,
        max_retries=3,
        requests_per_second=1000,  # ingen konstgjord väntan i dessa tester
        cache_ttl_seconds=0,
    )
    defaults.update(overrides)
    return BingXMarketDataConnector(**defaults)


@respx.mock
def test_get_ticker_returns_raw_dict_unmapped():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(
            200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT", "lastPrice": "77955.4"}}
        )
    )
    result = _connector().get_ticker("BTC-USDT")
    assert result == {"symbol": "BTC-USDT", "lastPrice": "77955.4"}


@respx.mock
def test_get_klines_returns_raw_list():
    respx.get(f"{_BASE_URL}/openApi/swap/v3/quote/klines").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": [
                    {
                        "open": "1",
                        "close": "2",
                        "high": "3",
                        "low": "0.5",
                        "volume": "10",
                        "time": 1,
                    }
                ],
            },
        )
    )
    result = _connector().get_klines("BTC-USDT", interval="1h", limit=1)
    assert isinstance(result, list)
    assert result[0]["close"] == "2"


@respx.mock
def test_get_contracts_returns_raw_list():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/contracts").mock(
        return_value=Response(
            200, json={"code": 0, "msg": "", "data": [{"symbol": "BTC-USDT", "status": 1}]}
        )
    )
    result = _connector().get_contracts()
    assert result == [{"symbol": "BTC-USDT", "status": 1}]


@respx.mock
def test_get_funding_rate_returns_raw_list():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/fundingRate").mock(
        return_value=Response(
            200,
            json={"code": 0, "msg": "", "data": [{"symbol": "BTC-USDT", "fundingRate": "0.0001"}]},
        )
    )
    result = _connector().get_funding_rate("BTC-USDT")
    assert result[0]["fundingRate"] == "0.0001"


@respx.mock
def test_get_open_interest_returns_raw_dict():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/openInterest").mock(
        return_value=Response(
            200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT", "openInterest": "123"}}
        )
    )
    result = _connector().get_open_interest("BTC-USDT")
    assert result == {"symbol": "BTC-USDT", "openInterest": "123"}


def test_connector_only_calls_whitelisted_market_data_paths():
    """Positivt bevis (utöver Phase 0:s generella grep-test): connectorns
    egna endpoint-konstanter är EXAKT de fem verifierade market-data-
    endpointsen, inget mer - ingen account-/order-path kan smygas in utan
    att detta testet upptäcker det."""
    import crypto_trading.connectors.bingx_market_data as module

    paths = {
        module._CONTRACTS_PATH,
        module._TICKER_PATH,
        module._KLINES_PATH,
        module._FUNDING_RATE_PATH,
        module._OPEN_INTEREST_PATH,
    }
    assert paths == {
        "/openApi/swap/v2/quote/contracts",
        "/openApi/swap/v2/quote/ticker",
        "/openApi/swap/v3/quote/klines",
        "/openApi/swap/v2/quote/fundingRate",
        "/openApi/swap/v2/quote/openInterest",
    }
    for path in paths:
        assert "/account" not in path
        assert "/order" not in path
        assert "/trade" not in path


@respx.mock
def test_transient_5xx_is_retried_then_succeeds():
    route = respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        side_effect=[
            Response(500),
            Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT"}}),
        ]
    )
    result = _connector(max_retries=3).get_ticker("BTC-USDT")
    assert result == {"symbol": "BTC-USDT"}
    assert route.call_count == 2


@respx.mock
def test_persistent_failure_raises_connector_unavailable_not_a_crash():
    """BingX-nere-scenariot: pipelinen ska aldrig gissa eller krascha,
    bara signalera tydligt att data saknas (SPEC §8.2 - instrumentet blir
    DATA_INVALID nedströms i Phase 2, inte här)."""
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(return_value=Response(500))
    with pytest.raises(ConnectorUnavailableError):
        _connector(max_retries=2).get_ticker("BTC-USDT")


@respx.mock
def test_timeout_raises_connector_unavailable():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        side_effect=TimeoutException("timed out")
    )
    with pytest.raises(ConnectorUnavailableError):
        _connector(max_retries=1).get_ticker("BTC-USDT")


@respx.mock
def test_api_level_error_code_raises_without_retry():
    route = respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 100001, "msg": "invalid symbol", "data": None})
    )
    with pytest.raises(ConnectorUnavailableError):
        _connector().get_ticker("NOTREAL-USDT")
    assert route.call_count == 1  # ett applikationsfel (fel symbol) retryas inte i onödan


@respx.mock
def test_rate_limiter_enforces_minimum_interval_between_calls():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT"}})
    )
    connector = _connector(requests_per_second=5, cache_ttl_seconds=0)  # min_interval = 0.2s
    started = time.monotonic()
    connector.get_ticker("BTC-USDT")
    connector.get_ticker("ETH-USDT")
    elapsed = time.monotonic() - started
    assert elapsed >= 0.15, f"andra anropet verkar inte ha väntat (elapsed={elapsed:.3f}s)"


@respx.mock
def test_cache_avoids_duplicate_http_call_within_ttl():
    route = respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT"}})
    )
    connector = _connector(cache_ttl_seconds=10)
    connector.get_ticker("BTC-USDT")
    connector.get_ticker("BTC-USDT")
    assert route.call_count == 1  # andra anropet kom från cachen, inget nytt HTTP-anrop
