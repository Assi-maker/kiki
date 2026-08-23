from datetime import UTC, datetime

from intelligence.pipeline.normalize import normalize_record
from intelligence.schemas.event import RawRecord


def test_normalize_hackernews_extracts_score():
    record = RawRecord(
        source_id="hn",
        fetched_at=datetime.now(UTC),
        payload={"id": 111, "score": 250, "time": 1700000000},
        content_hash="h1",
    )
    normalized = normalize_record(record, source_type="forum")
    assert normalized.metric == "score"
    assert normalized.value == 250.0
    assert normalized.raw_ref == "h1"


def test_normalize_alpha_vantage_extracts_price():
    record = RawRecord(
        source_id="alpha_vantage",
        fetched_at=datetime.now(UTC),
        payload={"Global Quote": {"01. symbol": "IBM", "05. price": "231.50"}},
        content_hash="h2",
    )
    normalized = normalize_record(record, source_type="market_data")
    assert normalized.metric == "price"
    assert normalized.value == 231.50
