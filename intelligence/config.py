from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    anthropic_api_key: str | None
    alphavantage_api_key: str | None
    db_path: Path
    scoring_weights_path: Path
    max_events_per_run: int
    max_opportunities_per_run: int
    max_agent_calls_per_run: int
    agent_timeout_seconds: float
    connector_timeout_seconds: float
    connector_max_retries: int


def get_settings() -> Settings:
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    db_path_override = os.environ.get("DB_PATH_OVERRIDE")
    db_path = (
        Path(db_path_override)
        if db_path_override
        else _PROJECT_ROOT / "data" / "intelligence.db"
    )
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY") or None,
        db_path=db_path,
        scoring_weights_path=_PROJECT_ROOT / "config" / "scoring_weights.yaml",
        max_events_per_run=int(os.environ.get("MAX_EVENTS_PER_RUN", "20")),
        max_opportunities_per_run=int(os.environ.get("MAX_OPPORTUNITIES_PER_RUN", "5")),
        max_agent_calls_per_run=int(os.environ.get("MAX_AGENT_CALLS_PER_RUN", "50")),
        agent_timeout_seconds=float(os.environ.get("AGENT_TIMEOUT_SECONDS", "30")),
        connector_timeout_seconds=float(os.environ.get("CONNECTOR_TIMEOUT_SECONDS", "10")),
        connector_max_retries=int(os.environ.get("CONNECTOR_MAX_RETRIES", "3")),
    )
