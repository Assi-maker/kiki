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


def test_normalize_hackernews_preserves_title_url_author_and_text():
    # Fas 2: opportunity-hunter/trading-research need more than a bare score
    # deviation to do their job — the HN payload already carries this content,
    # it just wasn't surviving normalization.
    record = RawRecord(
        source_id="hn",
        fetched_at=datetime.now(UTC),
        payload={
            "id": 111,
            "score": 250,
            "time": 1700000000,
            "title": "Show HN: I built a thing",
            "url": "https://example.com/thing",
            "by": "someuser",
            "text": "A self-text body",
        },
        content_hash="h1",
    )
    normalized = normalize_record(record, source_type="forum")
    assert normalized.title == "Show HN: I built a thing"
    assert normalized.url == "https://example.com/thing"
    assert normalized.author == "someuser"
    assert normalized.content_excerpt == "A self-text body"


def test_normalize_hackernews_missing_optional_fields_stay_none():
    # A typical story has no self-text — must not crash or fabricate a value.
    record = RawRecord(
        source_id="hn",
        fetched_at=datetime.now(UTC),
        payload={"id": 111, "score": 250, "time": 1700000000, "title": "T", "by": "u"},
        content_hash="h1",
    )
    normalized = normalize_record(record, source_type="forum")
    assert normalized.title == "T"
    assert normalized.url is None
    assert normalized.content_excerpt is None


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
