from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

from crypto_trading.config.loader import GatePolicyConfig
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
    # P1 (2026-09-28) audit trail: which ENFORCED content conditions failed,
    # what the SHADOW-only forecast conditions would have done, and every
    # number the decision was based on.
    enforced_failed: list[str] = Field(default_factory=list)
    shadow: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _forecast_metrics(candidate: Candidate) -> dict:
    """Scenario names are free-form ("bullish_continuation", "bearish_reversal",
    ...): every key containing "bull" / "bear" is summed, the rest is neutral."""
    probabilities = candidate.forecast.scenario_probabilities if candidate.forecast else {}
    if not probabilities:
        return {}
    bull = round(sum(v for k, v in probabilities.items() if "bull" in k.lower()), 6)
    bear = round(sum(v for k, v in probabilities.items() if "bear" in k.lower()), 6)
    neutral = round(max(0.0, sum(probabilities.values()) - bull - bear), 6)
    buckets = [p for p in (bull, bear, neutral) if p > 0]
    entropy = -sum(p * math.log(p) for p in buckets) / math.log(3) if buckets else None
    return {
        "bull_probability": bull, "bear_probability": bear, "neutral_probability": neutral,
        "forecast_confidence": max(bull, bear, neutral),
        "forecast_uncertainty": round(entropy, 6) if entropy is not None else None,
        "forecast_horizon": candidate.forecast.horizon if candidate.forecast else None,
    }


def _shadow_forecast_checks(metrics: dict, policy: GatePolicyConfig) -> dict:
    """SHADOW ONLY - never changes the outcome (user decision 2026-09-28)."""
    if "bull_probability" not in metrics:
        failed = ["SHADOW_FORECAST_UNPARSEABLE"]
    else:
        bull, bear = metrics["bull_probability"], metrics["bear_probability"]
        neutral = metrics["neutral_probability"]
        failed = []
        if not (bull > bear and bull >= neutral):
            failed.append("SHADOW_BULLISH_NOT_DOMINANT")
        if bear > bull and bear >= neutral:
            failed.append("SHADOW_BEARISH_DOMINANT")
        if bull <= policy.shadow_min_bullish_probability:
            failed.append("SHADOW_BULLISH_NOT_MAJORITY")
    return {"failed": failed, "would_block": bool(failed)}


def _content_checks(
    candidate: Candidate, policy: GatePolicyConfig, now: datetime | None
) -> tuple[list[str], dict]:
    """ENFORCED content requirements. Every failing condition is reported."""
    failed: list[str] = []
    metrics: dict = {
        "candidate_score": candidate.evidence_record.candidate_score,
        "trigger_reasons": list(candidate.evidence_record.trigger_reasons),
        "reference_price": str(candidate.reference_price) if candidate.reference_price else None,
        "evaluated_at": now.isoformat() if now else None,
        "signal_created_at": candidate.created_at.isoformat(),
    }
    reference = candidate.reference_price
    stop = _decimal(candidate.risk.suggested_stop_loss)
    target = _decimal(candidate.risk.suggested_target)
    metrics.update(stop_loss=str(stop) if stop is not None else candidate.risk.suggested_stop_loss,
                   target=str(target) if target is not None else candidate.risk.suggested_target)
    if reference is None:
        failed.append("REFERENCE_PRICE_MISSING")
    elif stop is None or target is None:
        failed.append("RISK_LEVELS_UNPARSEABLE")
    elif not (stop < reference < target):
        # The Risk Agent's own plan contradicts a LONG: a structural conflict.
        failed.append("CONFLICT_RISK_LEVELS_CONTRADICT_LONG")
    else:
        risk_reward = (target - reference) / (reference - stop)
        metrics["risk_reward"] = str(risk_reward.normalize())
        if risk_reward < policy.min_risk_reward:
            failed.append("RR_BELOW_MINIMUM")
    if candidate.qa.violations:
        failed.append("CONFLICT_QA_PASSED_WITH_VIOLATIONS")
    if now is not None:
        age_minutes = (now - candidate.created_at).total_seconds() / 60
        metrics["signal_age_minutes"] = round(age_minutes, 3)
        if age_minutes > policy.max_signal_age_minutes:
            failed.append("SIGNAL_STALE")
    return failed, metrics


def evaluate_risk_signal_gate(
    candidate: Candidate,
    open_positions: int,
    max_concurrent_positions: int,
    policy: GatePolicyConfig | None = None,
    now: datetime | None = None,
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

    # P1 (2026-09-28): AI form alone never creates CONFIRMED. `now` is always
    # passed by the orchestrator; without it the freshness check cannot run.
    policy = policy or GatePolicyConfig()
    enforced_failed, metrics = _content_checks(candidate, policy, now)
    metrics.update(_forecast_metrics(candidate))
    shadow = _shadow_forecast_checks(metrics, policy)
    if enforced_failed:
        return GateDecision(
            outcome="NO_TRADE", reasons=[f"content_gate:{code}" for code in enforced_failed],
            enforced_failed=enforced_failed, shadow=shadow, metrics=metrics,
        )
    return GateDecision(
        outcome="CONFIRMED", reasons=["all_checks_passed"], enforced_failed=[],
        shadow=shadow, metrics=metrics,
    )
