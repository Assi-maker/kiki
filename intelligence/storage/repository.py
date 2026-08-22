from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from intelligence.schemas.assessments import (
    AssessmentBase,
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.event import Event
from intelligence.schemas.opportunity import Opportunity, OpportunityStatus
from intelligence.schemas.source import Source
from intelligence.storage.db import get_connection

_ASSESSMENT_TYPES: dict[str, type[AssessmentBase]] = {
    "research": ResearchAssessment,
    "opportunity": OpportunityAssessment,
    "market": MarketAssessment,
    "forecast": ForecastAssessment,
    "risk": RiskAssessment,
    "bear": BearAssessment,
    "qa": QAAssessment,
}


class Repository(Protocol):
    def save_source(self, source: Source) -> None: ...
    def save_event(self, event: Event) -> None: ...
    def has_seen_content_hash(self, source_id: str, content_hash: str) -> bool: ...
    def save_opportunity(self, opportunity: Opportunity) -> None: ...
    def get_opportunity(self, opportunity_id: str) -> Opportunity | None: ...
    def update_opportunity_status(
        self, opportunity_id: str, status: OpportunityStatus
    ) -> None: ...
    def save_assessment(
        self, opportunity_id: str, field_name: str, assessment: AssessmentBase
    ) -> None: ...
    def log_run_event(self, run_id: str, **fields) -> None: ...


class SQLiteRepository:
    def __init__(self, path: Path):
        self._conn = get_connection(path)

    def save_source(self, source: Source) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)",
            (source.source_id, source.name, source.type, source.reliability_score, source.url),
        )
        self._conn.commit()

    def save_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event.event_id, event.source_id, event.observed_at.isoformat(), event.category,
                event.metric, event.baseline, event.deviation, event.description, event.raw_ref,
            ),
        )
        self._conn.commit()

    def has_seen_content_hash(self, source_id: str, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE source_id = ? AND raw_ref = ?", (source_id, content_hash)
        ).fetchone()
        return row is not None

    def save_opportunity(self, opportunity: Opportunity) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO opportunities "
            "(opportunity_id, event_id, created_at, category, title, summary, "
            "time_horizon, liquidity, status, score, score_breakdown) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                opportunity.opportunity_id,
                opportunity.event_id,
                opportunity.created_at.isoformat(),
                opportunity.category,
                opportunity.title,
                opportunity.summary,
                opportunity.time_horizon,
                opportunity.liquidity,
                opportunity.status,
                opportunity.score,
                json.dumps(opportunity.score_breakdown) if opportunity.score_breakdown else None,
            ),
        )
        self._conn.commit()
        for field_name in _ASSESSMENT_TYPES:
            assessment = getattr(opportunity, field_name)
            if assessment is not None:
                self.save_assessment(opportunity.opportunity_id, field_name, assessment)

    def save_assessment(
        self, opportunity_id: str, field_name: str, assessment: AssessmentBase
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO assessments VALUES (?,?,?)",
            (opportunity_id, field_name, assessment.model_dump_json()),
        )
        self._conn.commit()

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        row = self._conn.execute(
            "SELECT * FROM opportunities WHERE opportunity_id = ?", (opportunity_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["score_breakdown"] = (
            json.loads(data["score_breakdown"]) if data["score_breakdown"] else None
        )
        for field_name, cls in _ASSESSMENT_TYPES.items():
            arow = self._conn.execute(
                "SELECT payload FROM assessments WHERE opportunity_id = ? AND field_name = ?",
                (opportunity_id, field_name),
            ).fetchone()
            data[field_name] = cls.model_validate_json(arow["payload"]) if arow else None
        return Opportunity(**data)

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        self._conn.execute(
            "UPDATE opportunities SET status = ? WHERE opportunity_id = ?", (status, opportunity_id)
        )
        self._conn.commit()

    def log_run_event(self, run_id: str, **fields) -> None:
        self._conn.execute(
            "INSERT INTO runs "
            "(run_id, event_id, opportunity_id, agent_name, status, started_at, "
            "completed_at, errors, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                fields.get("event_id"),
                fields.get("opportunity_id"),
                fields.get("agent_name", ""),
                fields.get("status", ""),
                fields.get("started_at"),
                fields.get("completed_at"),
                fields.get("errors"),
                fields.get("latency_ms"),
            ),
        )
        self._conn.commit()
