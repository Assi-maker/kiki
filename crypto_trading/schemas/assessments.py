from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator

from crypto_trading.schemas.common import AssessmentStatus


class AssessmentBase(BaseModel):
    agent_name: str
    run_id: str
    created_at: datetime
    status: AssessmentStatus


class NewsSentimentAssessment(AssessmentBase):
    verified_facts: list[str]
    source_claims: list[str]
    interpretation: str


class TechnicalAssessment(AssessmentBase):
    market_data: dict
    interpretation: str


class BullThesisAssessment(AssessmentBase):
    hypothesis: str
    catalyst: str
    setup: str


class ForecastAssessment(AssessmentBase):
    scenario_probabilities: dict[str, float]
    horizon: str
    forecast_version: str

    @field_validator("scenario_probabilities")
    @classmethod
    def probabilities_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if not (0.999 <= total <= 1.001):
            raise ValueError(f"scenario_probabilities must sum to 1.0, got {total}")
        return v


class RiskAssessment(AssessmentBase):
    suggested_stop_loss: str
    suggested_target: str
    downside: str
    liquidity_risk: str
    model_risk: str
    timing_risk: str


class BearAdversarialAssessment(AssessmentBase):
    counterarguments: list[str]
    alternative_explanations: list[str]
    falsification_conditions: str


class QAAssessment(AssessmentBase):
    passed: bool
    violations: list[str]


class ProposedHeuristic(BaseModel):
    """ONE candidate heuristic proposed by the GODFATHER Strategist role
    (2026-09-15, Guardian Authority Live Autonomy, Task 3 - the PROPOSE step
    of propose -> validate -> promote -> track/demote).

    `condition` is a plain dict in exactly the shape
    `guardian/authority.py::heuristic_condition_matches` already consumes
    (see that module's "Condition-matching semantics" docstring section:
    `"<name>_max"`/`"<name>_min"` numeric bounds, list-membership, equality,
    AND across keys, fail-closed on a missing factor). It is persisted
    verbatim as `guardian_authority_heuristic_candidates.condition_json` and
    is never rewritten/normalized on the way in - the later, independent
    validation step evaluates it through that same unmodified matcher, so a
    condition the matcher cannot satisfy simply collects zero samples and is
    rejected there rather than silently "fixed" here.

    `adjustment` mirrors `guardian_authority_heuristics.adjustment`: signed,
    positive reinforces the decision the heuristic conditions on, negative
    discourages it. Carries ZERO effect on any real decision while the row
    sits in the candidates table - only promotion (a separate, later step)
    ever copies a candidate into the live heuristics table."""

    description: str
    condition: dict
    adjustment: float
    rationale: str


class GodfatherStrategistAssessment(AssessmentBase):
    """Output of `.claude/agents/crypto-godfather-strategist.md`. An EMPTY
    `proposed_heuristics` list with `status="ok"` is a fully valid, expected
    and often-correct answer ("the supplied history does not support a
    confident pattern") - never treated as a failure by
    guardian/self_improvement.py::propose_candidate_heuristics."""

    proposed_heuristics: list[ProposedHeuristic]


class OpportunityScreenAssessment(AssessmentBase):
    """Billig förscreening (kostnadsoptimering 2026-09-02) - körs INNAN den
    fulla 7-rollskedjan, på en separat, billigare modell (t.ex. Haiku 4.5).
    Avgör aldrig CONFIRMED/NO_TRADE själv - bara vilka kandidater som är
    värda den dyra fulla analysen. Aldrig en handelsrekommendation."""

    opportunity_score: float
    reasoning: str
