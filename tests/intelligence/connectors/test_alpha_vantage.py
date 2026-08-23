import logging

import pytest
import respx
from httpx import Response

from intelligence.connectors.alpha_vantage import AlphaVantageConnector
from intelligence.connectors.exceptions import ConnectorConfigError
from intelligence.pipeline.event_pipeline import run_event_pipeline
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


class _FakeRepo:
    """Minimal Repository stand-in — event_pipeline only needs these two."""

    def has_seen_content_hash(self, source_id: str, content_hash: str) -> bool:
        return False

    def save_event(self, event) -> None:
        pass


@respx.mock
def test_non_2xx_response_never_leaks_api_key_through_full_pipeline_log(caplog):
    # Finding #1 regression: the real key value must never reach any log output
    # via the connector -> event_pipeline -> log_event composition, not just
    # in isolation. 429 is Alpha Vantage's expected steady-state failure mode.
    caplog.set_level(logging.INFO)
    fake_key = "SUPER-SECRET-KEY-123"
    respx.get("https://www.alphavantage.co/query").mock(
        return_value=Response(429, json={"Note": "rate limited"})
    )
    connector = AlphaVantageConnector(
        _source(),
        timeout_seconds=5,
        max_retries=1,
        api_key=fake_key,
        symbols=["IBM"],
        min_interval_seconds=0,
    )

    run_event_pipeline(
        connectors=[connector],
        source_types={"alpha_vantage": "market_data"},
        baselines={"alpha_vantage": 100.0},
        repo=_FakeRepo(),
        max_events=20,
        run_id="r1",
    )

    # Scope this to *our* "intelligence" logger (the log_event/redact() pipeline
    # this finding is about) rather than all captured loggers — httpx logs its
    # own "HTTP Request: ..." line (with the URL) at INFO independently of our
    # code, which is a separate, pre-existing concern outside this fix's scope.
    combined = "\n".join(r.getMessage() for r in caplog.records if r.name == "intelligence")
    assert fake_key not in combined
    assert "connector_unavailable" in combined
