from __future__ import annotations

import xml.etree.ElementTree as ET

from crypto_trading.connectors.base import BaseMarketDataConnector


class NewsRSSConnector(BaseMarketDataConnector):
    """Kryptonyheter via RSS - icke-kritisk källa (SPEC §8.2/§14). Leverantör:
    CoinDesk RSS, kostnadsfri, nyckellös, källangiven. Exakt feedformat
    (RSS 2.0 <item>-fält) verifieras live mot den riktiga URL:en i en
    dedikerad @pytest.mark.live-täckt task, inte antaget här."""

    _source_name = "CoinDesk RSS"

    def _parse_response(self, response, path: str) -> list[dict]:
        root = ET.fromstring(response.text)
        items = []
        for item in root.findall("./channel/item"):
            items.append(
                {
                    "title": (item.findtext("title") or "").strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "pub_date": (item.findtext("pubDate") or "").strip(),
                    "description": (item.findtext("description") or "").strip(),
                }
            )
        return items

    def get_latest_items(self, limit: int) -> list[dict]:
        items = self._get("", {})
        return items[:limit]
