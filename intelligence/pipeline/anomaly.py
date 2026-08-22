from __future__ import annotations

import uuid

from intelligence.schemas.event import Event, NormalizedRecord
from intelligence.schemas.source import Source


def detect_events(
    records: list[NormalizedRecord], source: Source, baseline: float, threshold_pct: float = 50.0
) -> list[Event]:
    events = []
    for record in records:
        if baseline == 0:
            continue
        deviation_pct = abs(record.value - baseline) / baseline * 100
        if deviation_pct >= threshold_pct:
            events.append(
                Event(
                    event_id=str(uuid.uuid4()),
                    source_id=source.source_id,
                    observed_at=record.observed_at,
                    category=source.type,
                    metric=record.metric,
                    baseline=baseline,
                    deviation=deviation_pct,
                    description=(
                        f"{record.metric}={record.value} avviker {deviation_pct:.1f}% "
                        f"från baseline {baseline}"
                    ),
                    raw_ref=record.raw_ref,
                )
            )
    return events
