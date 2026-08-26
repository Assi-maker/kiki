import pytest
import respx
from httpx import Response

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.connectors.external_data import ExternalDataConnector


def _connector() -> ExternalDataConnector:
    return ExternalDataConnector(
        base_url="https://api.alternative.me/fng/",
        timeout_seconds=10,
        max_retries=3,
        requests_per_second=1,
        cache_ttl_seconds=60,
    )


@respx.mock
def test_get_fear_greed_index_returns_parsed_value():
    respx.get("https://api.alternative.me/fng/").mock(
        return_value=Response(
            200,
            json={
                "name": "Fear and Greed Index",
                "data": [
                    {
                        "value": "42",
                        "value_classification": "Fear",
                        "timestamp": "1756209600",
                    }
                ],
                "metadata": {"error": None},
            },
        )
    )
    result = _connector().get_fear_greed_index()
    assert result["value"] == "42"
    assert result["value_classification"] == "Fear"
    assert result["timestamp"] == "1756209600"


@respx.mock
def test_get_fear_greed_index_raises_when_data_list_is_empty():
    respx.get("https://api.alternative.me/fng/").mock(
        return_value=Response(200, json={"name": "Fear and Greed Index", "data": []})
    )
    with pytest.raises(ConnectorUnavailableError):
        _connector().get_fear_greed_index()
