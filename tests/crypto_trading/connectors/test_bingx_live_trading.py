import json
from urllib.parse import parse_qs

import pytest
import respx
from httpx import Response

from crypto_trading.connectors.bingx_live_trading import (
    BingXLiveTradingConnector,
    LiveExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_LIVE_BASE = "https://open-api.bingx.com"


def _connector(**overrides) -> BingXLiveTradingConnector:
    defaults = dict(api_key="k", api_secret="s", timeout_seconds=5, max_retries=2)
    defaults.update(overrides)
    return BingXLiveTradingConnector(**defaults)


@respx.mock
def test_place_entry_order_with_sl_tp_hits_live_host_with_10x_leverage_fields():
    route = respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(
            200,
            json={"code": 0, "msg": "", "data": {"order": {"orderId": "ex-1", "avgPrice": "50030"}}},
        )
    )

    result = _connector().place_entry_order_with_sl_tp(
        symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
        stop_loss_price="49000", target_price="52000",
    )

    assert result == {"orderId": "ex-1", "avgPrice": "50030"}
    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    assert params["symbol"] == ["BTC-USDT"]
    assert params["clientOrderID"] == ["lv-cid-1"]
    assert "signature" in params
    stop_loss = json.loads(params["stopLoss"][0])
    assert stop_loss == {
        "type": "STOP_MARKET", "quantity": 0.002, "stopPrice": 49000.0,
        "price": 49000.0, "workingType": "MARK_PRICE",
    }


@respx.mock
def test_set_leverage_defaults_to_10x():
    route = respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/leverage").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"leverage": 10}})
    )

    _connector().set_leverage("BTC-USDT")

    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    assert params["leverage"] == ["10"]


@respx.mock
def test_place_entry_order_raises_on_api_error_code():
    respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80001, "msg": "insufficient balance", "data": {}})
    )

    with pytest.raises(ConnectorUnavailableError, match="insufficient balance"):
        _connector().place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_to_place_order_against_a_non_live_host():
    connector = _connector()
    connector._base_url = "https://open-api-vst.bingx.com"  # simulate a mutated instance

    with pytest.raises(LiveExecutionGuardError):
        connector.place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_a_lookalike_host():
    """A subdomain/near-miss host must never pass the guard (exact match
    only, no substring check - same discipline as the Demo connector)."""
    connector = _connector()
    connector._base_url = "https://open-api.bingx.com.evil.example"

    with pytest.raises(LiveExecutionGuardError):
        connector.cancel_all_open_orders("BTC-USDT")


@respx.mock
def test_get_all_positions_filters_out_flat_positions():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": [
                    {"symbol": "ETH-USDT", "positionSide": "LONG", "positionAmt": "0"},
                    {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"},
                ],
            },
        )
    )

    result = _connector().get_all_positions()

    assert result == [{"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"}]


@respx.mock
def test_get_position_filters_by_symbol_from_get_all_positions():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": [
                    {"symbol": "ETH-USDT", "positionSide": "LONG", "positionAmt": "1.0"},
                    {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"},
                ],
            },
        )
    )

    result = _connector().get_position("BTC-USDT")

    assert result == {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"}


@respx.mock
def test_get_position_returns_none_for_a_symbol_not_in_the_account():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": [{"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"}],
            },
        )
    )

    result = _connector().get_position("SOL-USDT")

    assert result is None


@respx.mock
def test_get_balance_returns_the_unwrapped_balance_object():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/balance").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": {"balance": {"asset": "USDT", "availableMargin": "123.45"}},
            },
        )
    )

    result = _connector().get_balance()

    assert result == {"asset": "USDT", "availableMargin": "123.45"}


@respx.mock
def test_close_position_market_omits_reduce_only():
    route = respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"order": {"avgPrice": "49500"}}})
    )

    result = _connector().close_position_market("BTC-USDT", "0.002", "lv-close-1")

    assert result == {"avgPrice": "49500"}
    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    assert "reduceOnly" not in params
    assert params["side"] == ["SELL"]
    assert params["positionSide"] == ["LONG"]
