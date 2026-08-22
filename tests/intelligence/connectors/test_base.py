import hashlib
import time

from intelligence.connectors.base import BaseConnector
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source


class _FakeConnector(BaseConnector):
    def __init__(self, source, timeout_seconds, max_retries, min_interval_seconds):
        super().__init__(source, timeout_seconds, max_retries, min_interval_seconds)
        self.fetch_calls = 0

    def fetch(self):
        self._rate_limit()
        self.fetch_calls += 1
        payload = {"id": 1}
        return [
            RawRecord(
                source_id=self.source.source_id,
                fetched_at=self._now(),
                payload=payload,
                content_hash=self._content_hash(payload),
            )
        ]


def _source():
    return Source(source_id="fake", name="Fake", type="test", reliability_score=0.5, url="https://x.com")


def test_content_hash_is_deterministic():
    c = _FakeConnector(_source(), timeout_seconds=1, max_retries=1, min_interval_seconds=0)
    h1 = c._content_hash({"a": 1, "b": 2})
    h2 = c._content_hash({"b": 2, "a": 1})
    assert h1 == h2
    assert h1 == hashlib.sha256(b'{"a": 1, "b": 2}').hexdigest()


def test_rate_limit_enforces_min_interval():
    c = _FakeConnector(_source(), timeout_seconds=1, max_retries=1, min_interval_seconds=0.2)
    start = time.monotonic()
    c.fetch()
    c.fetch()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2
    assert c.fetch_calls == 2


def test_validate_passes_through_well_formed_records():
    c = _FakeConnector(_source(), timeout_seconds=1, max_retries=1, min_interval_seconds=0)
    records = c.fetch()
    validated = c.validate(records)
    assert len(validated) == 1
    assert validated[0].payload == {"id": 1}
