from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorConfigError, ConnectorUnavailableError
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source

_BASE_URL = "https://www.alphavantage.co/query"


class AlphaVantageConnector(BaseConnector):
    def __init__(
        self,
        source: Source,
        timeout_seconds: float,
        max_retries: int,
        api_key: str | None,
        symbols: list[str],
        min_interval_seconds: float = 12.0,
    ):
        super().__init__(source, timeout_seconds, max_retries, min_interval_seconds)
        self._api_key = api_key
        self._symbols = symbols

    def fetch(self) -> list[RawRecord]:
        if not self._api_key:
            raise ConnectorConfigError(
                "ALPHAVANTAGE_API_KEY saknas — connectorn är avstängd tills en nyckel konfigureras"
            )
        try:
            return self._fetch_with_retry()
        except httpx.HTTPError as exc:
            # OBS: interpolera aldrig `exc` (eller dess request-URL) rakt av här —
            # httpx:s felmeddelanden innehåller hela request-URL:en inklusive
            # ?apikey=... i klartext. Använd bara typnamn + ev. statuskod.
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status_code}" if status_code is not None else type(exc).__name__
            raise ConnectorUnavailableError(f"Alpha Vantage otillgänglig: {detail}") from exc

    def _fetch_with_retry(self) -> list[RawRecord]:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )
        def _do() -> list[RawRecord]:
            records = []
            with httpx.Client(timeout=self.timeout_seconds) as client:
                for symbol in self._symbols:
                    self._rate_limit()
                    resp = client.get(
                        _BASE_URL,
                        params={
                            "function": "GLOBAL_QUOTE",
                            "symbol": symbol,
                            "apikey": self._api_key,
                        },
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    records.append(
                        RawRecord(
                            source_id=self.source.source_id,
                            fetched_at=self._now(),
                            payload=payload,
                            content_hash=self._content_hash(payload),
                        )
                    )
            return records

        return _do()
