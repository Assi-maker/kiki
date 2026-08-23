from __future__ import annotations

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorError
from intelligence.logging import log_event
from intelligence.pipeline.anomaly import detect_events
from intelligence.pipeline.dedupe import is_duplicate
from intelligence.pipeline.normalize import normalize_record
from intelligence.schemas.event import Event
from intelligence.storage.repository import Repository


def run_event_pipeline(
    connectors: list[BaseConnector],
    source_types: dict[str, str],
    baselines: dict[str, float],
    repo: Repository,
    max_events: int,
    run_id: str,
) -> list[Event]:
    all_events: list[Event] = []

    for connector in connectors:
        source_id = connector.source.source_id
        try:
            raw_records = connector.validate(connector.fetch())
        except ConnectorError as exc:
            log_event(run_id, event="connector_unavailable", source_id=source_id, error=str(exc))
            continue

        fresh_records = [r for r in raw_records if not is_duplicate(repo, r)]
        normalized = [normalize_record(r, source_types[source_id]) for r in fresh_records]
        events = detect_events(normalized, connector.source, baseline=baselines[source_id])

        for event in events:
            if len(all_events) >= max_events:
                log_event(run_id, event="max_events_reached", limit=max_events)
                return all_events
            repo.save_event(event)
            all_events.append(event)

    return all_events
