from datetime import UTC, datetime

from intelligence.pipeline.anomaly import detect_events
from intelligence.schemas.event import NormalizedRecord
from intelligence.schemas.source import Source


def _source():
    return Source(
        source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com"
    )


def test_large_deviation_creates_event():
    record = NormalizedRecord(
        source_id="hn", observed_at=datetime.now(UTC), metric="score", value=300.0, raw_ref="h1"
    )
    events = detect_events([record], _source(), baseline=50.0, threshold_pct=50.0)
    assert len(events) == 1
    assert events[0].deviation == 500.0


def test_small_deviation_creates_no_event():
    record = NormalizedRecord(
        source_id="hn", observed_at=datetime.now(UTC), metric="score", value=55.0, raw_ref="h1"
    )
    events = detect_events([record], _source(), baseline=50.0, threshold_pct=50.0)
    assert events == []
