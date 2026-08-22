from datetime import UTC, datetime

from intelligence.pipeline.dedupe import is_duplicate
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source
from intelligence.storage.repository import SQLiteRepository


def test_is_duplicate_false_then_true_after_seen(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_source(
        Source(source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com")
    )
    record = RawRecord(
        source_id="hn", fetched_at=datetime.now(UTC), payload={"id": 1}, content_hash="dup-hash"
    )
    assert is_duplicate(repo, record) is False

    from intelligence.schemas.event import Event
    repo.save_event(Event(
        event_id="evt-1", source_id="hn", observed_at=datetime.now(UTC), category="trend",
        metric="score", baseline=1.0, deviation=1.0, description="d", raw_ref="dup-hash",
    ))
    assert is_duplicate(repo, record) is True
