from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.candidate import Candidate

_REQUIRED_ROLES = (
    "news_sentiment",
    "technical",
    "bull_thesis",
    "forecast",
    "risk",
    "bear_adversarial",
    "qa",
)


class GateDecision(BaseModel):
    outcome: Literal["CONFIRMED", "NO_TRADE", "REJECTED"]
    reasons: list[str]


def evaluate_risk_signal_gate(
    candidate: Candidate, open_positions: int, max_concurrent_positions: int
) -> GateDecision:
    """SPEC §1 kärnprincip 1 / §8.3: helt oberoende av AI-utfallet - kan
    blockera CONFIRMED även när alla sju roller är positiva (AC4).

    REJECTED/NO_TRADE-avgränsning (se PLAN_CRYPTO_PHASE3.md Global
    Constraints): REJECTED = alla sju assessments närvarande med
    status="ok" OCH QAAssessment.passed is False (fullt analyserad,
    sakligt underkänd). Allt annat som blockerar CONFIRMED - saknad/
    failed/timeout-assessment, eller gatens egna oberoende regler - ger
    NO_TRADE, aldrig REJECTED.
    """
    missing_or_failed = [
        role
        for role in _REQUIRED_ROLES
        if getattr(candidate, role) is None or getattr(candidate, role).status != "ok"
    ]
    if missing_or_failed:
        return GateDecision(
            outcome="NO_TRADE",
            reasons=[f"missing_or_failed_assessment:{role}" for role in missing_or_failed],
        )

    if candidate.qa.passed is False:
        return GateDecision(outcome="REJECTED", reasons=["qa_gate_rejected"])

    if open_positions >= max_concurrent_positions:
        return GateDecision(
            outcome="NO_TRADE",
            reasons=[
                f"max_concurrent_positions reached: {open_positions}/{max_concurrent_positions}"
            ],
        )

    return GateDecision(outcome="CONFIRMED", reasons=["all_checks_passed"])
