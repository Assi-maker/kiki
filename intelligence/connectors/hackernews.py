from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorUnavailableError
from intelligence.schemas.event import RawRecord

_BASE_URL = "https://hacker-news.firebaseio.com/v0"
_TOP_STORIES_LIMIT = 10


class HackerNewsConnector(BaseConnector):
    def fetch(self) -> list[RawRecord]:
        self._rate_limit()
        try:
            return self._fetch_with_retry()
        except httpx.HTTPError as exc:
            raise ConnectorUnavailableError(f"Hacker News otillgänglig: {exc}") from exc

    def _fetch_with_retry(self) -> list[RawRecord]:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )
        def _do() -> list[RawRecord]:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.get(f"{_BASE_URL}/topstories.json")
                resp.raise_for_status()
                story_ids = resp.json()[:_TOP_STORIES_LIMIT]
                records = []
                for story_id in story_ids:
                    item_resp = client.get(f"{_BASE_URL}/item/{story_id}.json")
                    item_resp.raise_for_status()
                    payload = item_resp.json()
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
