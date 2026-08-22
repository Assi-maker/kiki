from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RawRecord(BaseModel):
    source_id: str
    fetched_at: datetime
    payload: dict
    content_hash: str


class NormalizedRecord(BaseModel):
    source_id: str
    observed_at: datetime
    metric: str
    value: float
    raw_ref: str


class Event(BaseModel):
    event_id: str
    source_id: str
    observed_at: datetime
    category: str
    metric: str
    baseline: float
    deviation: float
    description: str
    raw_ref: str
