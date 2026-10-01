"""Deterministic QA step (P1, 2026-10-01) - replaces the QA/Gate LLM agent.

What the QA agent was asked to do (.claude/agents/crypto-qa-gate.md):
  1. check that every preceding assessment exists and is filled in;
  2. look for semantic contradictions BETWEEN them (Bull vs Bear, Forecast
     horizon vs Risk, Risk downside vs Bull setup);
  never judge the content itself.

Part 1 is mechanical and is done here, with the same outcome contract as the
agent (`QAAssessment.passed` / `violations`, REJECTED by the Gate when
`passed` is False). Part 2 has no input any more: with
config/ai_pipeline.yaml the Bull, Bear, Forecast, News and Technical roles no
longer run, so there is nothing for an LLM to cross-check. When legacy roles
do run (code default), their mechanical invariants are checked here too.

Deliberately NOT repeated here: what the Risk/Signal Gate already enforces
(SL < reference < TP for a LONG, R:R minimum, signal age). A duplicate would
turn the Gate's NO_TRADE into REJECTED and change the meaning of the audit
trail, not its safety.

Pure: no I/O, no AI, no clock other than the one passed in.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal, InvalidOperation

from crypto_trading.schemas.assessments import QAAssessment
from crypto_trading.schemas.candidate import Candidate

AGENT_NAME = "deterministic_qa"

# Sanity bounds for the Risk Agent's levels relative to the reference price.
# Not a strategy filter: every CONFIRMED candidate 2026-09-01..10-01 had its
# stop 0.6-10.2 % and its target 1-25 % away. These only catch a malformed
# answer (a stop at 0.0001, a target 10x the price, a unit mix-up).
MIN_STOP_DISTANCE = Decimal("0.001")
MAX_STOP_DISTANCE = Decimal("0.25")
MAX_TARGET_DISTANCE = Decimal("1.0")
_TEXT_FIELDS_RISK = ("downside", "liquidity_risk", "model_risk", "timing_risk")


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def check_candidate(candidate: Candidate, required_roles: tuple[str, ...]) -> list[str]:
    """All violations for `candidate`; empty = passed. `required_roles` are the
    AI roles that ran on the entry path (QA excluded)."""
    violations: list[str] = []
    for role in required_roles:
        assessment = getattr(candidate, role, None)
        if assessment is None:
            violations.append(f"MISSING_ASSESSMENT:{role}")
        elif assessment.status != "ok":
            violations.append(f"ASSESSMENT_NOT_OK:{role}:{assessment.status}")

    evidence = candidate.evidence_record
    if evidence.instrument != candidate.instrument:
        violations.append("EVIDENCE_INSTRUMENT_MISMATCH")
    if evidence.data_quality_status != "ok":
        violations.append(f"EVIDENCE_DATA_QUALITY:{evidence.data_quality_status}")

    risk = candidate.risk
    if risk is not None and risk.status == "ok":
        for name in _TEXT_FIELDS_RISK:
            if not str(getattr(risk, name, "") or "").strip():
                violations.append(f"RISK_FIELD_EMPTY:{name}")
        stop = _decimal(risk.suggested_stop_loss)
        target = _decimal(risk.suggested_target)
        reference = candidate.reference_price
        if stop is not None and stop <= 0:
            violations.append("RISK_STOP_NOT_POSITIVE")
        if target is not None and target <= 0:
            violations.append("RISK_TARGET_NOT_POSITIVE")
        if (reference is not None and reference > 0 and stop is not None and target is not None
                and stop > 0 and target > 0 and stop < reference < target):
            stop_distance = (reference - stop) / reference
            target_distance = (target - reference) / reference
            if not MIN_STOP_DISTANCE <= stop_distance <= MAX_STOP_DISTANCE:
                violations.append("RISK_STOP_DISTANCE_IMPLAUSIBLE")
            if target_distance > MAX_TARGET_DISTANCE:
                violations.append("RISK_TARGET_DISTANCE_IMPLAUSIBLE")

    forecast = candidate.forecast
    if forecast is not None and forecast.status == "ok":
        values = list(forecast.scenario_probabilities.values())
        if not values or any(not math.isfinite(v) or v < 0 or v > 1 for v in values):
            violations.append("FORECAST_PROBABILITIES_INVALID")
        if not str(forecast.horizon or "").strip():
            violations.append("FORECAST_HORIZON_MISSING")
    return violations


def run_deterministic_qa(
    candidate: Candidate, required_roles: tuple[str, ...], run_id: str, now: datetime
) -> QAAssessment:
    violations = check_candidate(candidate, required_roles)
    return QAAssessment(
        agent_name=AGENT_NAME,
        run_id=run_id,
        created_at=now,
        status="ok",
        passed=not violations,
        violations=violations,
    )
