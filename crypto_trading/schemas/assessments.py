from __future__ import annotations

from datetime import datetime
from typing import Literal

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
    ever copies a candidate into the live heuristics table.

    `target_decision_type` (Task 4B, 2026-09-16 addendum; widened to a third
    value, `TAKE_PROFIT`, alongside Guardian Authority's TAKE_PROFIT
    decision type) is the decision type this proposal is FOR, declared
    explicitly by the proposing model rather than inferred from the
    condition's shape - an auditable statement of intent, and the ONLY thing
    that routes the candidate to its own evidence pool at validation time.
    The three types are validated against three structurally different,
    never-merged pools: `TIGHTEN_SL` against resolved Guardian Authority
    TIGHTEN_SL decisions (shadow + real), `PRE_ENTRY_VETO` against a real
    closed-position counterfactual pool (real pre-entry evidence, real
    realized PnL), and `TAKE_PROFIT` against a real per-observation
    counterfactual pool (real progress_ratio/unrealized_pnl at each tick of
    an open position, real eventual realized PnL) - the latter two are both
    testable at cold start, when no Guardian Authority decision has ever
    been made. Each type also has its OWN factor vocabulary
    (`guardian_state`-shaped factors vs. `_pre_entry_factors`'
    `instrument`/`candidate_score`/`trigger_reasons` vs. `progress_ratio`/
    `unrealized_pnl_positive`), and the three vocabularies share zero field
    names with each other; a condition written in another type's vocabulary
    simply never matches anything in its own pool and is rejected there on
    sample size, never silently "fixed" here."""

    description: str
    condition: dict
    adjustment: float
    rationale: str
    target_decision_type: Literal["TIGHTEN_SL", "PRE_ENTRY_VETO", "TAKE_PROFIT"]


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
