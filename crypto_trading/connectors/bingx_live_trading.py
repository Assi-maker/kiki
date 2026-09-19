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

_LIVE_HOST = "open-api.bingx.com"
_ORDER_PATH = "/openApi/swap/v2/trade/order"
_ALL_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/allOpenOrders"
_LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"
_POSITIONS_PATH = "/openApi/swap/v2/user/positions"
_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/openOrders"
_ALL_ORDERS_PATH = "/openApi/swap/v2/trade/allOrders"
_BALANCE_PATH = "/openApi/swap/v2/user/balance"


def _unwrap_order(data: dict | None) -> dict:
    """Same nesting quirk BingXDemoTradingConnector already found live
    (2026-09-04): the order endpoint nests actual fields one level down
    under "order". Centralized here so every caller gets a flat dict."""
    if not data:
        return {}
    return data.get("order", data)


class LiveExecutionGuardError(Exception):
    """Raised whenever this connector would otherwise send a mutating
    request to anything other than the exact real BingX host. Refuses to
    proceed rather than risk placing an order somewhere unintended - see
    docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md."""


class OrderRejectedError(Exception):
    """Raised only by place_entry_order_with_sl_tp(), only when the
    exchange's own synchronous response to THIS specific submission is a
    definitive, structured rejection (a parsed JSON body with a non-zero
    `code` - the exchange received, parsed, and refused the request, e.g.
    "TP Price must be greater than Last Price"). This is never a guess:
    the order-placement endpoint is synchronous, so this response IS the
    authoritative, zero-fill outcome for this exact submission - there is
    no order to look up afterwards, because none was ever created.
    Deliberately NOT a ConnectorUnavailableError subclass: a genuine
    transport/format ambiguity (timeout, non-JSON body, a bare HTTP status
    error) must keep raising plain ConnectorUnavailableError and go through
    the existing lookup-based resolution - only a confirmed rejection ends
    up here (2026-09-06 fix, following the same-day Risk D audit)."""


class _ApiCodeError(ConnectorUnavailableError):
    """Internal-only: raised by _request() for a parsed response body whose
    `code` is non-zero. A ConnectorUnavailableError subclass so every other
    call site (get_balance, get_all_positions, set_leverage, the two order
    lookups, get_open_orders) keeps behaving exactly as before - only
    place_entry_order_with_sl_tp gives this a different, more specific
    meaning by catching it and re-raising OrderRejectedError."""


class BingXLiveTradingConnector:
    """Order placement/cancel/query against the user's REAL BingX account.
    `_base_url` is a hardcoded class constant, never a constructor parameter
    or settings/env value - there is no code path that can point this
    connector at the Demo/VST host or anywhere else. A separate class from
    BingXDemoTradingConnector on purpose: no shared mutable state, no risk
    that a base-class change silently affects both hosts."""

    _base_url = f"https://{_LIVE_HOST}"

    def __init__(
        self, api_key: str, api_secret: str, timeout_seconds: float = 10.0, max_retries: int = 3
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def _guard_host(self) -> None:
        parsed = urlparse(self._base_url)
        if parsed.scheme != "https" or parsed.hostname != _LIVE_HOST:
            raise LiveExecutionGuardError(
                f"refuses to trade against host={parsed.hostname!r}, "
                f"only {_LIVE_HOST!r} is permitted"
            )

    def _sign_query(self, params: dict) -> str:
        """Same signing/transport discipline already live-verified for
        BingX Demo (2026-09-04): plain, never percent-encoded key=value
        join, HMAC-SHA256 over that exact string, POST body (not URL query
        string) to avoid a CloudFront-level WAF rejection of JSON-valued
        params in the URL."""
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
                        f"BingX Live HTTP error: {path} ({exc})"
                    ) from exc
                raise ConnectorUnavailableError(
                    f"BingX Live: non-JSON response from {path} (status {response.status_code})"
                )
            if body.get("code") != 0:
                raise _ApiCodeError(
                    f"BingX Live API error {body.get('code')}: {body.get('msg')} ({path})"
                )
            return body.get("data")

        return _do()

    def set_leverage(self, symbol: str, leverage: int = 10, side: str = "LONG") -> dict:
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
                    "quantity": float(quantity),
                    "stopPrice": float(stop_loss_price),
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
        try:
            return _unwrap_order(self._request("POST", _ORDER_PATH, params))
        except _ApiCodeError as exc:
            raise OrderRejectedError(str(exc)) from exc

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

    def get_all_positions(self) -> list[dict]:
        """Read-only. Account-wide open-position list in one call - used by
        the reconciled capacity count (live_execution.py) so the two-layer
        gate never trusts local DB state alone."""
        positions = self._request("GET", _POSITIONS_PATH, {}) or []
        return [p for p in positions if Decimal(str(p.get("positionAmt", "0"))) != 0]

    def get_position(self, symbol: str) -> dict | None:
        for position in self.get_all_positions():
            if position.get("symbol") == symbol:
                return position
        return None

    def get_open_orders(self, symbol: str) -> list[dict]:
        data = self._request("GET", _OPEN_ORDERS_PATH, {"symbol": symbol}) or {}
        if isinstance(data, list):
            return data
        return data.get("orders", [])

    def get_order_history(self, symbol: str, start_time_ms: int, limit: int = 50) -> list[dict]:
        """Read-only. The exchange's own order history for `symbol` since
        `start_time_ms` (GET allOrders) - the ground truth for HOW a position
        actually closed (which order type filled, and at what price). Unlike
        get_order_by_client_order_id/get_order_status, an API error is NOT
        collapsed into an empty result: it raises ConnectorUnavailableError so a
        caller can tell 'no such orders' from 'the exchange did not answer'."""
        data = self._request(
            "GET", _ALL_ORDERS_PATH,
            {"symbol": symbol, "startTime": start_time_ms, "limit": limit},
        ) or {}
        if isinstance(data, list):
            return data
        return data.get("orders", [])

    def cancel_all_open_orders(self, symbol: str) -> dict:
        return self._request("DELETE", _ALL_OPEN_ORDERS_PATH, {"symbol": symbol}) or {}

    def close_position_market(self, symbol: str, quantity: str, client_order_id: str) -> dict:
        """LONG-only close: side=SELL against positionSide=LONG. No
        reduceOnly - confirmed live on the Demo account (2026-09-04) that a
        hedge-mode account rejects it outright; positionSide=LONG already
        provides the same safety property (a SELL order pinned to the LONG
        bucket can only reduce/close it, never flip/increase). Not yet
        independently re-verified against the LIVE account (Task 11, read-
        only checks only - this call itself is never made by this plan)."""
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

    def place_stop_loss_order(
        self, symbol: str, quantity: str, stop_price: str, client_order_id: str
    ) -> dict:
        """Standalone SL replacement for LIVE Profit Protection (2026-09-13) -
        NEVER touches TP, NEVER used at entry. Same STOP_MARKET/MARK_PRICE
        shape as the SL sub-order inside place_entry_order_with_sl_tp's
        combined placement, just issued alone against the same order
        endpoint. Raises OrderRejectedError on a structured rejection -
        identical, already-safety-audited semantics as
        place_entry_order_with_sl_tp (a synchronous rejection here means zero
        fill guaranteed, nothing to look up)."""
        params = {
            "symbol": symbol,
            "side": "SELL",
            "positionSide": "LONG",
            "type": "STOP_MARKET",
            "quantity": quantity,
            "stopPrice": stop_price,
            "clientOrderID": client_order_id,
            "workingType": "MARK_PRICE",
        }
        try:
            return _unwrap_order(self._request("POST", _ORDER_PATH, params))
        except _ApiCodeError as exc:
            raise OrderRejectedError(str(exc)) from exc

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        """Cancels exactly one order by orderId - never the whole-symbol
        cancel_all_open_orders(), which would also remove TP. Same
        _ORDER_PATH as get_order_status()/place_entry_order_with_sl_tp(),
        verified against CCXT's documented BingX linear-swap cancelOrder
        implementation (DELETE, {symbol, orderId})."""
        return self._request("DELETE", _ORDER_PATH, {"symbol": symbol, "orderId": order_id}) or {}

    def get_balance(self) -> dict:
        """Read-only. Real USDT-margin account balance. Response shape and
        field names (`availableMargin` et al.) live-verified 2026-09-06
        (Task 11, read-only): `{"code":0,"data":{"balance":{...}}}`, exactly
        as assumed - `data.get("balance", data)` unwraps correctly. Every
        caller still reads fields defensively via `.get(..., "0")`."""
        data = self._request("GET", _BALANCE_PATH, {}) or {}
        return data.get("balance", data)
