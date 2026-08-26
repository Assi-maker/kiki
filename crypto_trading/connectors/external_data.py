from __future__ import annotations

from crypto_trading.connectors.base import BaseMarketDataConnector
from crypto_trading.connectors.exceptions import ConnectorUnavailableError


class ExternalDataConnector(BaseMarketDataConnector):
    """Fear & Greed Index - icke-kritisk källa (SPEC §8.2/§14). Leverantör:
    alternative.me:s publika API, kostnadsfri, nyckellös, källangiven. Exakt
    svarsformat verifieras live mot den riktiga URL:en i en dedikerad
    @pytest.mark.live-täckt task, inte antaget här."""

    _source_name = "Fear & Greed Index (alternative.me)"

    def _parse_response(self, response, path: str) -> dict:
        body = response.json()
        data = body.get("data") or []
        if not data:
            raise ConnectorUnavailableError(
                f"{self._source_name} otillgänglig: tomt data-fält i svaret ({path})"
            )
        return data[0]

    def get_fear_greed_index(self) -> dict:
        return self._get("", {})
