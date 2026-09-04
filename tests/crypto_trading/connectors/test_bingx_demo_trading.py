from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from crypto_trading.connectors.bingx_demo_trading import (
    BingXDemoTradingConnector,
    DemoExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_VST_BASE = "https://open-api-vst.bingx.com"


def _connector(**overrides) -> BingXDemoTradingConnector:
    defaults = dict(api_key="k", api_secret="s", timeout_seconds=5, max_retries=2)
    defaults.update(overrides)
    return BingXDemoTradingConnector(**defaults)


@respx.mock
def test_place_entry_order_with_sl_tp_hits_vst_host_with_signed_request():
    route = respx.post(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {"orderId": "ex-1", "avgPrice": "50030"},
            },
        )
    )

    result = _connector().place_entry_order_with_sl_tp(
        symbol="BTC-USDT",
        quantity="0.02",
        client_order_id="cid-1",
        stop_loss_price="49000",
        target_price="52000",
    )

    assert result == {"orderId": "ex-1", "avgPrice": "50030"}
    request = route.calls[0].request
    assert request.headers["X-BX-APIKEY"] == "k"
    params = parse_qs(urlparse(str(request.url)).query)
    assert params["symbol"] == ["BTC-USDT"]
    assert params["clientOrderID"] == ["cid-1"]
    assert "signature" in params


@respx.mock
def test_place_entry_order_raises_on_api_error_code():
    respx.post(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80001, "msg": "insufficient balance", "data": {}})
    )

    with pytest.raises(ConnectorUnavailableError, match="insufficient balance"):
        _connector().place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.02", client_order_id="cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_to_place_order_against_a_non_vst_host():
    connector = _connector()
    connector._base_url = "https://open-api.bingx.com"  # simulate a mutated instance

    with pytest.raises(DemoExecutionGuardError):
        connector.place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.02", client_order_id="cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_a_lookalike_host():
    """A subdomain/near-miss host must never pass the guard (exact match
    only, no substring check - required per user feedback on the design)."""
    connector = _connector()
    connector._base_url = "https://open-api-vst.bingx.com.evil.example"

    with pytest.raises(DemoExecutionGuardError):
        connector.cancel_all_open_orders("BTC-USDT")


@respx.mock
def test_get_order_by_client_order_id_returns_none_when_not_found():
    respx.get(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80016, "msg": "order not found", "data": {}})
    )

    result = _connector().get_order_by_client_order_id("BTC-USDT", "cid-missing")

    assert result is None
