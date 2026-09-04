import json
from urllib.parse import parse_qs

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
                # BingX nests the actual order fields under "order" -
                # confirmed live 2026-09-04, not documented anywhere
                # readable at design time.
                "data": {"order": {"orderId": "ex-1", "avgPrice": "50030"}},
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
    # POST sends params in the body, not the URL query string - a raw,
    # unencoded JSON-valued stopLoss/takeProfit param in the URL triggers a
    # CloudFront-level rejection (confirmed live, 2026-09-04), so the query
    # string is empty and everything lives in the request body instead.
    assert request.url.query == b""
    body = request.content.decode("utf-8")
    params = parse_qs(body)
    assert params["symbol"] == ["BTC-USDT"]
    assert params["clientOrderID"] == ["cid-1"]
    assert "signature" in params
    stop_loss = json.loads(params["stopLoss"][0])
    take_profit = json.loads(params["takeProfit"][0])
    # BingX silently ignores an attached stopLoss/takeProfit that's missing
    # quantity/price (confirmed live 2026-09-04: order filled, but a
    # follow-up query showed the SL/TP had NOT actually been registered) -
    # both fields are required alongside stopPrice/type/workingType.
    assert stop_loss == {
        "type": "STOP_MARKET", "quantity": 0.02, "stopPrice": 49000.0,
        "price": 49000.0, "workingType": "MARK_PRICE",
    }
    assert take_profit == {
        "type": "TAKE_PROFIT_MARKET", "quantity": 0.02, "stopPrice": 52000.0,
        "price": 52000.0, "workingType": "MARK_PRICE",
    }


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


@respx.mock
def test_get_order_status_unwraps_the_order_envelope():
    respx.get(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": {"order": {"orderId": "ex-1", "status": "FILLED", "avgPrice": "49000"}},
            },
        )
    )

    result = _connector().get_order_status("BTC-USDT", "ex-1")

    assert result == {"orderId": "ex-1", "status": "FILLED", "avgPrice": "49000"}


@respx.mock
def test_close_position_market_unwraps_the_order_envelope_and_omits_reduce_only():
    route = respx.post(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(
            200, json={"code": 0, "msg": "", "data": {"order": {"avgPrice": "49500"}}}
        )
    )

    result = _connector().close_position_market("BTC-USDT", "0.001", "close-1")

    assert result == {"avgPrice": "49500"}
    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    # confirmed live 2026-09-04: BingX rejects reduceOnly outright on a
    # hedge-mode account ("In the Hedge mode, the 'ReduceOnly' field can
    # not be filled") - positionSide=LONG already prevents any flip/increase.
    assert "reduceOnly" not in params
    assert params["side"] == ["SELL"]
    assert params["positionSide"] == ["LONG"]


@respx.mock
def test_get_position_returns_matching_open_position():
    respx.get(f"{_VST_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": [
                    {"symbol": "ETH-USDT", "positionSide": "LONG", "positionAmt": "1.0"},
                    {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.001"},
                ],
            },
        )
    )

    result = _connector().get_position("BTC-USDT")

    assert result == {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.001"}


@respx.mock
def test_get_position_returns_none_when_flat_or_absent():
    respx.get(f"{_VST_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": [{"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0"}],
            },
        )
    )

    result = _connector().get_position("BTC-USDT")

    assert result is None
