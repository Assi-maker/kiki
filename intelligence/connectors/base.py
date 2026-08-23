from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source


class BaseConnector(ABC):
    """Hämtar och strukturellt validerar rådata. Normaliserar INTE och gör INGEN
    anomali-/eventdetektion — det är pipeline-lagrets ansvar (SPEC §6)."""

    def __init__(
        self,
        source: Source,
        timeout_seconds: float,
        max_retries: int,
        min_interval_seconds: float = 1.0,
    ):
        self.source = source
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._min_interval_seconds = min_interval_seconds
        self._last_call_at: float | None = None
        self._cache: dict[str, object] = {}

    @abstractmethod
    def fetch(self) -> list[RawRecord]: ...

    def validate(self, records: list[RawRecord]) -> list[RawRecord]:
        valid = []
        for record in records:
            if record.source_id and record.content_hash and isinstance(record.payload, dict):
                valid.append(record)
        return valid

    def _rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_call_at is not None:
            elapsed = now - self._last_call_at
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _content_hash(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _cached_fetch(self, key: str, loader):
        if key in self._cache:
            return self._cache[key]
        value = loader()
        self._cache[key] = value
        return value

    def _now(self) -> datetime:
        return datetime.now(UTC)
