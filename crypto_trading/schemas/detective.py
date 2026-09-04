from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.assessments import AssessmentBase


class DetectiveBatchAnalysis(AssessmentBase):
    """AI-utdata för EN batch stängda PAPER-trades (Post-Trade Analyst,
    2026-09-04). Producerar uteslutande observationer/hypoteser - aldrig en
    åtgärd, aldrig en config-/strategiändring (se .claude/agents/
    crypto-detective.md). Strukturellt identisk maskin som de sju
    realtidsrollerna (AssessmentBase + AgentRunner.run()), men körs ALDRIG
    av Orchestrator/Gate och ingår aldrig i agents/roles.py::ROLE_MAP/
    gate/risk_signal_gate.py::_REQUIRED_ROLES - se crypto_trading/
    detective/batch.py."""

    observations: list[str]
    winning_patterns: list[str]
    losing_patterns: list[str]


class DetectiveAnalysisRecord(BaseModel):
    """Persisterad rad (storage/db.py::detective_analyses) - refererar bara
    till redan lagrade position_ids istället för att duplicera trade-/
    evidensdata (explicit användarkrav: "undvik att duplicera stora
    datamängder i onödan")."""

    analysis_id: str
    created_at: datetime
    position_ids: list[str]
    win_count: int
    loss_count: int
    breakeven_count: int
    status: Literal["ok", "failed"]
    observations: list[str]
    winning_patterns: list[str]
    losing_patterns: list[str]
    stats_snapshot: dict
    ai_cost_usd: Decimal
