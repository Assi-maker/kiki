from datetime import UTC, datetime

from intelligence.schemas.event import Event, NormalizedRecord, RawRecord


def test_raw_record_roundtrip():
    r = RawRecord(
        source_id="hn",
        fetched_at=datetime.now(UTC),
        payload={"id": 1},
        content_hash="abc123",
    )
    assert r.payload["id"] == 1


def test_normalized_record_roundtrip():
    n = NormalizedRecord(
        source_id="hn",
        observed_at=datetime.now(UTC),
        metric="score",
        value=42.0,
        raw_ref="abc123",
    )
    assert n.value == 42.0


def test_event_roundtrip():
    e = Event(
        event_id="evt-1",
        source_id="hn",
        observed_at=datetime.now(UTC),
        category="trend",
        metric="score",
        baseline=10.0,
        deviation=32.0,
        description="Score 42 vs baseline 10",
        raw_ref="abc123",
    )
    assert e.deviation == 32.0
