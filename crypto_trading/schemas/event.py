from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Event(BaseModel):
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    run_id: str | None
    schema_version: int
    payload: dict
