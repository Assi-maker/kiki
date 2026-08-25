from __future__ import annotations

from typing import Literal

CandidateStatus = Literal[
    "CANDIDATE",
    "DATA_INVALID",
    "BUDGET_LIMITED",
    "UNDER_AI_ANALYSIS",
    "ANALYSIS_INTERRUPTED",
    "REJECTED",
    "NO_TRADE",
    "CONFIRMED",
]

PositionStatus = Literal["OPEN_POSITION", "CLOSED"]

AssessmentStatus = Literal["ok", "failed", "timeout"]

DataQualityStatus = Literal["ok", "degraded", "invalid"]
