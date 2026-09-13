import json
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from crypto_trading.connectors.bingx_live_trading import (
    BingXLiveTradingConnector,
    LiveExecutionGuardError,
    OrderRejectedError,
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
def test_place_entry_order_raises_order_rejected_on_definitive_api_error_code():
    """A parsed JSON body with a non-zero `code` is the exchange's own
    synchronous, authoritative refusal of THIS submission - a genuine
    rejection, not ambiguity, so it must NOT surface as the same
    ConnectorUnavailableError used for real transport/format ambiguity
    (2026-09-06 fix: the previous behavior left rejected LIVE entries stuck
    in CLAIMED forever, permanently blocking a capacity slot)."""
    respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80001, "msg": "insufficient balance", "data": {}})
    )

    with pytest.raises(OrderRejectedError, match="insufficient balance"):
        _connector().place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
            stop_loss_price="49000", target_price="52000",
        )


@respx.mock
def test_place_entry_order_raises_order_rejected_not_connector_unavailable():
    """OrderRejectedError must not also be a ConnectorUnavailableError -
    callers that distinguish the two (paper_trading/live_execution.py) rely
    on them being unrelated exception types, not a subclass relationship."""
    respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(
            200, json={"code": 101400, "msg": "TP Price must exceed Last Price", "data": {}}
        )
    )

    try:
        _connector().place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
            stop_loss_price="49000", target_price="52000",
        )
        raise AssertionError("expected OrderRejectedError")
    except OrderRejectedError as exc:
        assert not isinstance(exc, ConnectorUnavailableError)


@respx.mock
def test_get_order_status_still_raises_connector_unavailable_on_api_error_code():
    """Non-placement calls must be provably unaffected by the placement-only
    OrderRejectedError distinction: a lookup's own `code != 0` response
    (e.g. "order does not exist") stays exactly the pre-existing
    ConnectorUnavailableError - callers rely on this to fall through to
    None (order-not-found is inherently ambiguous, never a confirmed
    rejection, see get_order_status/get_order_by_client_order_id)."""
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80016, "msg": "order does not exist", "data": {}})
    )

    assert _connector().get_order_status("BTC-USDT", "missing-order") is None


@respx.mock
def test_set_leverage_still_raises_connector_unavailable_on_api_error_code():
    """Same non-regression proof as above, for a non-order-placement
    mutating call."""
    respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/leverage").mock(
        return_value=Response(200, json={"code": 80014, "msg": "invalid leverage", "data": {}})
    )

    with pytest.raises(ConnectorUnavailableError, match="invalid leverage"):
        _connector().set_leverage("BTC-USDT")


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


@respx.mock
def test_place_stop_loss_order_sends_stop_market_sell_long():
    route = respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"order": {"orderId": "999"}}})
    )
    connector = _connector()

    result = connector.place_stop_loss_order(
        "BTC-USDT", quantity="0.01", stop_price="50000", client_order_id="lvabc123pp"
    )

    assert result["orderId"] == "999"
    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    assert params["side"] == ["SELL"]
    assert params["positionSide"] == ["LONG"]
    assert params["type"] == ["STOP_MARKET"]
    assert params["clientOrderID"] == ["lvabc123pp"]


@respx.mock
def test_place_stop_loss_order_raises_order_rejected_on_structured_error():
    respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80001, "msg": "duplicate stop order"})
    )
    connector = _connector()

    with pytest.raises(OrderRejectedError):
        connector.place_stop_loss_order("BTC-USDT", "0.01", "50000", "lvabc123pp")


@respx.mock
def test_cancel_order_sends_delete_with_symbol_and_order_id():
    route = respx.delete(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"orderId": "999", "status": "CANCELED"}})
    )
    connector = _connector()

    result = connector.cancel_order("BTC-USDT", "999")

    assert result["status"] == "CANCELED"
    query_bytes = route.calls.last.request.url.query
    if isinstance(query_bytes, bytes):
        query_str = query_bytes.decode("utf-8")
    else:
        query_str = query_bytes
    params = parse_qs(query_str)
    assert params["symbol"] == ["BTC-USDT"]
    assert params["orderId"] == ["999"]
