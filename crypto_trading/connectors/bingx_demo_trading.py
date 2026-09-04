from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_VST_HOST = "open-api-vst.bingx.com"
_ORDER_PATH = "/openApi/swap/v2/trade/order"
_ALL_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/allOpenOrders"
_LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"


class DemoExecutionGuardError(Exception):
    """Raised whenever this connector would otherwise send a mutating
    request to anything other than the exact BingX Demo (VST) host. Refuses
    to proceed rather than risk reaching the user's real BingX account -
    see docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md."""


class BingXDemoTradingConnector:
    """Order placement/cancel/query against the user's BingX Demo (VST)
    account ONLY. `_base_url` is a hardcoded class constant, never a
    constructor parameter or settings/env value - there is no code path
    that can point this connector at the live open-api.bingx.com host
    (SPEC_CRYPTO.md §1/§19 amendment, 2026-09-04)."""

    _base_url = f"https://{_VST_HOST}"

    def __init__(
        self, api_key: str, api_secret: str, timeout_seconds: float = 10.0, max_retries: int = 3
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def _guard_host(self) -> None:
        parsed = urlparse(self._base_url)
        if parsed.scheme != "https" or parsed.hostname != _VST_HOST:
            raise DemoExecutionGuardError(
                f"refuses to trade against host={parsed.hostname!r}, "
                f"only {_VST_HOST!r} is permitted"
            )

    def _sign(self, params: dict) -> dict:
        query = urlencode(sorted(params.items()))
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {**params, "signature": signature}

    def _request(self, method: str, path: str, params: dict) -> dict | None:
        self._guard_host()
        full_params = {**params, "timestamp": int(time.time() * 1000)}
        signed = self._sign(full_params)

        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.TransportError),
            reraise=True,
        )
        def _do() -> dict | None:
            self._guard_host()  # re-checked immediately before the network call itself
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=signed,
                    headers={"X-BX-APIKEY": self._api_key},
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ConnectorUnavailableError(f"BingX Demo HTTP-fel: {path} ({exc})") from exc
            body = response.json()
            if body.get("code") != 0:
                raise ConnectorUnavailableError(
                    f"BingX Demo API-fel {body.get('code')}: {body.get('msg')} ({path})"
                )
            return body.get("data")

        return _do()

    def set_leverage(self, symbol: str, leverage: int = 1, side: str = "LONG") -> dict:
        return self._request(
            "POST", _LEVERAGE_PATH, {"symbol": symbol, "side": side, "leverage": leverage}
        ) or {}

    def place_entry_order_with_sl_tp(
        self,
        symbol: str,
        quantity: str,
        client_order_id: str,
        stop_loss_price: str,
        target_price: str,
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": "BUY",
            "positionSide": "LONG",
            "type": "MARKET",
            "quantity": quantity,
            "clientOrderID": client_order_id,
            "stopLoss": json.dumps(
                {"type": "STOP_MARKET", "stopPrice": stop_loss_price, "workingType": "MARK_PRICE"}
            ),
            "takeProfit": json.dumps(
                {"type": "TAKE_PROFIT_MARKET", "stopPrice": target_price, "workingType": "MARK_PRICE"}
            ),
        }
        return self._request("POST", _ORDER_PATH, params) or {}

    def get_order_by_client_order_id(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            return self._request(
                "GET", _ORDER_PATH, {"symbol": symbol, "clientOrderID": client_order_id}
            )
        except ConnectorUnavailableError:
            return None

    def get_order_status(self, symbol: str, order_id: str) -> dict | None:
        try:
            return self._request("GET", _ORDER_PATH, {"symbol": symbol, "orderId": order_id})
        except ConnectorUnavailableError:
            return None

    def cancel_all_open_orders(self, symbol: str) -> dict:
        return self._request("DELETE", _ALL_OPEN_ORDERS_PATH, {"symbol": symbol}) or {}

    def close_position_market(self, symbol: str, quantity: str, client_order_id: str) -> dict:
        return (
            self._request(
                "POST",
                _ORDER_PATH,
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "positionSide": "LONG",
                    "type": "MARKET",
                    "quantity": quantity,
                    "reduceOnly": "true",
                    "clientOrderID": client_order_id,
                },
            )
            or {}
        )
