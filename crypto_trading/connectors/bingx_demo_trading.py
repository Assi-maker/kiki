from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_VST_HOST = "open-api-vst.bingx.com"
_ORDER_PATH = "/openApi/swap/v2/trade/order"
_ALL_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/allOpenOrders"
_LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"
_POSITIONS_PATH = "/openApi/swap/v2/user/positions"
_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/openOrders"


def _unwrap_order(data: dict | None) -> dict:
    """BingX's /trade/order endpoint (place, GET-by-id, GET-by-clientOrderID)
    nests the actual order fields one level down under a `data.order` key -
    confirmed live 2026-09-04, not documented anywhere readable at design
    time. Centralized here so every caller gets a flat dict, matching what
    the rest of this codebase already expects."""
    if not data:
        return {}
    return data.get("order", data)


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

    def _sign_query(self, params: dict) -> str:
        """Builds the exact, raw (never percent-encoded) query string that
        gets signed AND transmitted - byte-for-byte identical everywhere.
        Confirmed against BingX's own reference client implementations
        (2026-09-04, after this connector's first live attempts failed):
        the signature is HMAC-SHA256 over a plain `key=value` join with NO
        URL-encoding at all. This only matters for GET/DELETE, which still
        carry params in the URL query string - POST sends this same string
        as the request body instead (see _request()), which is what
        actually avoids a CloudFront-level rejection of literal `{`/`"`/`:`
        characters from the JSON-valued stopLoss/takeProfit params inside a
        URL (confirmed live: `X-Cache: Error from cloudfront` on an
        empty-body 400 when those characters were sent in the URL)."""
        query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict) -> dict | None:
        self._guard_host()
        full_params = {**params, "timestamp": int(time.time() * 1000)}
        query_string = self._sign_query(full_params)

        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.TransportError),
            reraise=True,
        )
        def _do() -> dict | None:
            self._guard_host()  # re-checked immediately before the network call itself
            headers = {"X-BX-APIKEY": self._api_key}
            with httpx.Client(timeout=self._timeout_seconds) as client:
                if method == "POST":
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    response = client.request(
                        method, f"{self._base_url}{path}", content=query_string, headers=headers
                    )
                else:
                    response = client.request(
                        method, f"{self._base_url}{path}?{query_string}", headers=headers
                    )
            try:
                body = response.json()
            except ValueError:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ConnectorUnavailableError(
                        f"BingX Demo HTTP-fel: {path} ({exc})"
                    ) from exc
                raise ConnectorUnavailableError(
                    f"BingX Demo: icke-JSON-svar från {path} (status {response.status_code})"
                )
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
                {
                    "type": "STOP_MARKET",
                    # quantity/price are required alongside stopPrice - confirmed live
                    # 2026-09-04: an entry order filled fine without them, but a
                    # follow-up query showed the SL/TP had NOT actually been
                    # registered (silently ignored, not rejected).
                    "quantity": float(quantity),
                    "stopPrice": float(stop_loss_price),  # numeric, not a string - BingX rejects a quoted stopPrice
                    "price": float(stop_loss_price),
                    "workingType": "MARK_PRICE",
                },
                separators=(",", ":"),
            ),
            "takeProfit": json.dumps(
                {
                    "type": "TAKE_PROFIT_MARKET",
                    "quantity": float(quantity),
                    "stopPrice": float(target_price),
                    "price": float(target_price),
                    "workingType": "MARK_PRICE",
                },
                separators=(",", ":"),
            ),
        }
        return _unwrap_order(self._request("POST", _ORDER_PATH, params))

    def get_order_by_client_order_id(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            data = self._request(
                "GET", _ORDER_PATH, {"symbol": symbol, "clientOrderID": client_order_id}
            )
        except ConnectorUnavailableError:
            return None
        return _unwrap_order(data) or None

    def get_order_status(self, symbol: str, order_id: str) -> dict | None:
        try:
            data = self._request("GET", _ORDER_PATH, {"symbol": symbol, "orderId": order_id})
        except ConnectorUnavailableError:
            return None
        return _unwrap_order(data) or None

    def get_position(self, symbol: str) -> dict | None:
        """Read-only. BingX does not expose a separate, independently
        queryable order id for the stopLoss/takeProfit legs attached to an
        entry order (confirmed live 2026-09-04: the entry order's own
        `get_order_status()` response embeds them as descriptive sub-fields,
        never as a distinct order id to poll) - so reconciliation
        (paper_trading/demo_execution.py::reconcile_active_executions)
        detects an exchange-side close by noticing the position itself has
        gone flat, not by polling a leg's order id."""
        positions = self._request("GET", _POSITIONS_PATH, {}) or []
        for position in positions:
            if position.get("symbol") == symbol and Decimal(str(position.get("positionAmt", "0"))) != 0:
                return position
        return None

    def get_open_orders(self, symbol: str) -> list[dict]:
        """Read-only, diagnostic/verification use: independent proof that
        attached stopLoss/takeProfit legs actually registered as live
        conditional orders on the exchange (as opposed to the entry order's
        own echoed-back fields, which can look correct without the
        attachment having taken effect - see place_entry_order_with_sl_tp's
        docstring). Not used by the production reconcile loop, which
        watches position state instead (§12 of the design doc)."""
        data = self._request("GET", _OPEN_ORDERS_PATH, {"symbol": symbol}) or {}
        if isinstance(data, list):
            return data
        return data.get("orders", [])

    def cancel_all_open_orders(self, symbol: str) -> dict:
        return self._request("DELETE", _ALL_OPEN_ORDERS_PATH, {"symbol": symbol}) or {}

    def close_position_market(self, symbol: str, quantity: str, client_order_id: str) -> dict:
        """LONG-only close: side=SELL against positionSide=LONG. No
        `reduceOnly` - confirmed live (2026-09-04) that BingX rejects it
        outright on a hedge-mode account ("In the Hedge mode, the
        'ReduceOnly' field can not be filled"). `positionSide=LONG` already
        provides the same safety property in hedge mode: a SELL order
        pinned to the LONG position bucket can only reduce/close that
        bucket, never flip into or increase a SHORT position (hedge mode
        tracks LONG/SHORT as entirely separate books, selected by
        positionSide, not by net side) - there is no code path here that
        could ever increase or flip the position."""
        return _unwrap_order(
            self._request(
                "POST",
                _ORDER_PATH,
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "positionSide": "LONG",
                    "type": "MARKET",
                    "quantity": quantity,
                    "clientOrderID": client_order_id,
                },
            )
        )
