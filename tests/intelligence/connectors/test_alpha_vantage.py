import pytest
import respx
from httpx import Response

from intelligence.connectors.alpha_vantage import AlphaVantageConnector
from intelligence.connectors.exceptions import ConnectorConfigError
from intelligence.schemas.source import Source


def _source():
    return Source(
        source_id="alpha_vantage",
        name="Alpha Vantage",
        type="market_data",
        reliability_score=0.8,
        url="https://www.alphavantage.co",
    )


def test_fetch_without_api_key_raises_config_error_not_crash():
    connector = AlphaVantageConnector(
        _source(),
        timeout_seconds=5,
        max_retries=2,
        api_key=None,
        symbols=["IBM"],
        min_interval_seconds=0,
    )
    with pytest.raises(ConnectorConfigError):
        connector.fetch()


@respx.mock
def test_fetch_with_mocked_key_and_http_returns_raw_records():
    respx.get("https://www.alphavantage.co/query").mock(
        return_value=Response(
            200,
            json={
                "Global Quote": {
                    "01. symbol": "IBM",
                    "05. price": "231.50",
                    "09. change": "5.10",
                    "10. change percent": "2.25%",
                }
            },
        )
    )
    connector = AlphaVantageConnector(
        _source(),
        timeout_seconds=5,
        max_retries=2,
        api_key="fake-key",
        symbols=["IBM"],
        min_interval_seconds=0,
    )
    records = connector.fetch()
    assert len(records) == 1
    assert records[0].payload["Global Quote"]["01. symbol"] == "IBM"
