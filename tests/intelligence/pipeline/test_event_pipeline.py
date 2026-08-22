from datetime import UTC, datetime

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorUnavailableError
from intelligence.pipeline.event_pipeline import run_event_pipeline
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source
from intelligence.storage.repository import SQLiteRepository


class _WorkingConnector(BaseConnector):
    def fetch(self):
        payload = {"id": 1, "score": 500}
        return [
            RawRecord(
                source_id=self.source.source_id,
                fetched_at=datetime.now(UTC),
                payload=payload,
                content_hash=self._content_hash(payload),
            )
        ]


class _BrokenConnector(BaseConnector):
    def fetch(self):
        raise ConnectorUnavailableError("simulerat fel")


def test_pipeline_continues_when_one_source_fails(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    working_source = Source(source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com")
    broken_source = Source(source_id="broken", name="Broken", type="forum", reliability_score=0.5, url="https://y.com")
    repo.save_source(working_source)
    repo.save_source(broken_source)

    connectors = [
        _WorkingConnector(working_source, timeout_seconds=1, max_retries=1, min_interval_seconds=0),
        _BrokenConnector(broken_source, timeout_seconds=1, max_retries=1, min_interval_seconds=0),
    ]
    events = run_event_pipeline(
        connectors=connectors,
        source_types={"hn": "forum", "broken": "forum"},
        baselines={"hn": 50.0, "broken": 50.0},
        repo=repo,
        max_events=10,
        run_id="r1",
    )
    assert len(events) == 1
    assert events[0].source_id == "hn"


def test_pipeline_respects_max_events(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    source = Source(source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com")
    repo.save_source(source)
    connector = _WorkingConnector(source, timeout_seconds=1, max_retries=1, min_interval_seconds=0)
    events = run_event_pipeline(
        connectors=[connector], source_types={"hn": "forum"}, baselines={"hn": 50.0},
        repo=repo, max_events=0, run_id="r1",
    )
    assert events == []
