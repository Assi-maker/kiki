from __future__ import annotations

from pydantic import BaseModel, Field


class Source(BaseModel):
    source_id: str
    name: str
    type: str
    reliability_score: float = Field(ge=0.0, le=1.0)
    url: str
