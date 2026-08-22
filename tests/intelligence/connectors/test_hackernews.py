import respx
from httpx import Response

from intelligence.connectors.hackernews import HackerNewsConnector
from intelligence.schemas.source import Source


def _connector():
    source = Source(source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://news.ycombinator.com")
    return HackerNewsConnector(source, timeout_seconds=5, max_retries=2, min_interval_seconds=0)


@respx.mock
def test_fetch_returns_raw_records_for_top_stories():
    respx.get("https://hacker-news.firebaseio.com/v0/topstories.json").mock(
        return_value=Response(200, json=[111, 222])
    )
    respx.get("https://hacker-news.firebaseio.com/v0/item/111.json").mock(
        return_value=Response(
            200,
            json={
                "id": 111,
                "title": "Cool thing",
                "score": 250,
                "descendants": 80,
                "time": 1700000000,
            },
        )
    )
    respx.get("https://hacker-news.firebaseio.com/v0/item/222.json").mock(
        return_value=Response(
            200,
            json={
                "id": 222,
                "title": "Other thing",
                "score": 10,
                "descendants": 2,
                "time": 1700000100,
            },
        )
    )
    connector = _connector()
    records = connector.fetch()
    assert len(records) == 2
    assert records[0].source_id == "hn"
    assert records[0].payload["score"] == 250
    assert records[0].content_hash


@respx.mock
def test_fetch_raises_connector_unavailable_after_retries():
    from intelligence.connectors.exceptions import ConnectorUnavailableError

    respx.get("https://hacker-news.firebaseio.com/v0/topstories.json").mock(return_value=Response(500))
    connector = _connector()
    try:
        connector.fetch()
        raise AssertionError("förväntade ConnectorUnavailableError")
    except ConnectorUnavailableError:
        pass
