from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.connectors.exceptions import ConnectorUnavailableError


class BaseMarketDataConnector:
    """Delad infrastruktur för market-data-connectors: timeout, retry,
    rate-limit, TTL-cache. En BingX-connector har flera distinkta endpoint-
    metoder istället för en enda fetch() - medveten avvikelse från Fas 1:s
    BaseConnector-form (som passar en connector med EN datatyp), se
    SPEC_CRYPTO.md §15 och Phase 1-designbeslutet i konversationshistoriken."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        requests_per_second: float,
        cache_ttl_seconds: float,
    ):
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._min_interval_seconds = 1.0 / requests_per_second
        self._cache_ttl_seconds = cache_ttl_seconds
        self._last_call_at: float | None = None
        self._cache: dict[str, tuple[float, object]] = {}

    def _rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_call_at is not None:
            elapsed = now - self._last_call_at
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _cache_get(self, key: str) -> object | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self._cache_ttl_seconds:
            del self._cache[key]
            return None
        return value

    def _cache_set(self, key: str, value: object) -> None:
        self._cache[key] = (time.monotonic(), value)

    def _get(self, path: str, params: dict) -> object:
        # "timestamp" är BingX-signeringsbrus (ändras varje anrop) och hör
        # INTE till förfrågans semantiska identitet - exkluderas ur cache-
        # nyckeln, annars missar cachen alltid (upptäckt vid exekvering).
        cache_params = {k: v for k, v in params.items() if k != "timestamp"}
        cache_key = f"{path}?{sorted(cache_params.items())}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            data = self._get_with_retry(path, params)
        except httpx.HTTPError as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status_code}" if status_code is not None else type(exc).__name__
            raise ConnectorUnavailableError(
                f"{self._source_name} otillgänglig: {path} ({detail})"
            ) from exc
        self._cache_set(cache_key, data)
        return data

    def _get_with_retry(self, path: str, params: dict) -> object:
        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )
        def _do() -> object:
            self._rate_limit()
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(f"{self._base_url}{path}", params=params)
                response.raise_for_status()
            return self._parse_response(response, path)

        return _do()

    # Klassattribut, override:as av subklasser för tydligare felmeddelanden
    # (t.ex. "BingX otillgänglig: ..." vs "CoinDesk RSS otillgänglig: ...").
    _source_name: str = "market data source"

    def _parse_response(self, response: httpx.Response, path: str) -> object:
        """Hook: tolkar det råa HTTP-svaret till anropbar data. BASEN gör
        INGET antagande om svarsformat (JSON/XML, envelope eller inte) -
        varje connector-familj (BingX-envelope, RSS, Fear&Greed-JSON, ...)
        implementerar sin egen tolkning. Delad infrastruktur ovanför denna
        punkt (timeout/retry/rate-limit/cache) är generell för alla
        market-data-/nyhets-/external-data-connectors (SPEC §15)."""
        raise NotImplementedError(
            f"{type(self).__name__} måste implementera _parse_response()"
        )

    def _now(self) -> datetime:
        return datetime.now(UTC)
