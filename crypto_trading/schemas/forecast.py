from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class ForecastRecord(BaseModel):
    forecast_id: str
    candidate_id: str
    instrument: str
    forecast_timestamp: datetime
    horizon: str
    scenario_probabilities: dict[str, float]
    forecast_version: str
    market_state_metadata: dict
    actual_outcome: str | None = None
    outcome_timestamp: datetime | None = None

    @field_validator("scenario_probabilities")
    @classmethod
    def probabilities_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if not (0.999 <= total <= 1.001):
            raise ValueError(f"scenario_probabilities must sum to 1.0, got {total}")
        return v
