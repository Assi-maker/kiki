import respx
from httpx import Response

from crypto_trading.connectors.news_rss import NewsRSSConnector

_ONE_ITEM_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Bitcoin surges past resistance</title>
    <link>https://example.com/1</link>
    <pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate>
    <description>Some description</description>
  </item>
</channel></rss>"""

_TWO_ITEM_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Bitcoin surges past resistance</title>
    <link>https://example.com/1</link>
    <pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate>
    <description>Some description</description>
  </item>
  <item>
    <title>Ethereum funding rate spikes</title>
    <link>https://example.com/2</link>
    <pubDate>Wed, 26 Aug 2026 09:00:00 GMT</pubDate>
    <description>Another description</description>
  </item>
</channel></rss>"""


def _connector() -> NewsRSSConnector:
    return NewsRSSConnector(
        base_url="https://www.coindesk.com/arc/outboundfeeds/rss/",
        timeout_seconds=10,
        max_retries=3,
        requests_per_second=1,
        cache_ttl_seconds=5,
    )


@respx.mock
def test_get_latest_items_parses_rss_entries():
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(
        return_value=Response(200, text=_ONE_ITEM_RSS)
    )
    items = _connector().get_latest_items(limit=10)
    assert items[0]["title"] == "Bitcoin surges past resistance"
    assert items[0]["link"] == "https://example.com/1"
    assert items[0]["pub_date"] == "Wed, 26 Aug 2026 10:00:00 GMT"
    assert items[0]["description"] == "Some description"


@respx.mock
def test_get_latest_items_respects_limit():
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(
        return_value=Response(200, text=_TWO_ITEM_RSS)
    )
    items = _connector().get_latest_items(limit=1)
    assert len(items) == 1
    assert items[0]["title"] == "Bitcoin surges past resistance"


@respx.mock
def test_get_latest_items_returns_all_when_fewer_than_limit():
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(
        return_value=Response(200, text=_TWO_ITEM_RSS)
    )
    items = _connector().get_latest_items(limit=10)
    assert len(items) == 2
    assert items[1]["title"] == "Ethereum funding rate spikes"
