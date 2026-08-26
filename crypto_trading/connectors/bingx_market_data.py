from __future__ import annotations

import time

from crypto_trading.connectors.base import BaseMarketDataConnector
from crypto_trading.connectors.exceptions import ConnectorUnavailableError

# Verifierade live 2026-08-25 mot https://open-api.bingx.com - se
# SPEC_CRYPTO.md §14 och konversationshistoriken för de faktiska svaren.
_CONTRACTS_PATH = "/openApi/swap/v2/quote/contracts"
_TICKER_PATH = "/openApi/swap/v2/quote/ticker"
_KLINES_PATH = "/openApi/swap/v3/quote/klines"
_FUNDING_RATE_PATH = "/openApi/swap/v2/quote/fundingRate"
_OPEN_INTEREST_PATH = "/openApi/swap/v2/quote/openInterest"


class BingXMarketDataConnector(BaseMarketDataConnector):
    """Uteslutande publika BingX swap (USDT-marginerade futures) market-
    data-endpoints. Ingen kod här refererar ett konto, en order eller en
    broker-credential (SPEC §1/§19)."""

    _source_name = "BingX"

    def _parse_response(self, response, path: str) -> object:
        """BingX-specifik svarsenvelope: {"code": 0, "msg": "", "data": ...}.
        Flyttad hit från BaseMarketDataConnector (Fas 3) - basen gör inget
        antagande om envelope-format, se base.py:s _parse_response-hook."""
        body = response.json()
        if body.get("code") != 0:
            raise ConnectorUnavailableError(
                f"BingX API-fel {body.get('code')}: {body.get('msg')} ({path})"
            )
        return body["data"]

    def get_contracts(self) -> list[dict]:
        return self._get(_CONTRACTS_PATH, {"timestamp": self._timestamp_ms()})

    def get_ticker(self, symbol: str) -> dict:
        return self._get(_TICKER_PATH, {"symbol": symbol, "timestamp": self._timestamp_ms()})

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        return self._get(
            _KLINES_PATH,
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "timestamp": self._timestamp_ms(),
            },
        )

    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]:
        return self._get(
            _FUNDING_RATE_PATH,
            {"symbol": symbol, "limit": limit, "timestamp": self._timestamp_ms()},
        )

    def get_open_interest(self, symbol: str) -> dict:
        return self._get(_OPEN_INTEREST_PATH, {"symbol": symbol, "timestamp": self._timestamp_ms()})

    @staticmethod
    def _timestamp_ms() -> int:
        return int(time.time() * 1000)
