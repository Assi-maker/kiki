"""Typed records for the GODFATHER Intelligence Layer (2026-09-25).

Every one of these is STRUCTURED DATA, not LLM prose - that is an
explicit user requirement ("Detta ska vara strukturerad data, inte bara
LLM-text", requirement 3; "Experience Memory ska vara strukturerad data",
requirement 11). An LLM may later be asked to narrate one of these
records; it is never the thing that produces the fields.

The taxonomies below are closed `Literal` sets on purpose. A classifier
that can emit a free-form label cannot be counted, grouped, or tested
against - and a category that keeps drifting cannot accumulate the sample
size that `godfather/experience.py` needs before it is allowed to call
anything an edge.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------
# Trade Investigator
# --------------------------------------------------------------------

TradeClassification = Literal[
    "GOOD_ENTRY_GOOD_MANAGEMENT",
    "GOOD_ENTRY_BAD_MANAGEMENT",
    "BAD_ENTRY",
    "BAD_TIMING",
    "REGIME_FAILURE",
    "FALSE_BREAKOUT",
    "MOMENTUM_DECAY",
    "EXIT_TOO_LATE",
    "TARGET_TOO_FAR",
    "SL_TOO_WIDE",
    "NOISE",
    "UNKNOWN",
]

QualityVerdict = Literal["GOOD", "ACCEPTABLE", "BAD", "UNKNOWN"]


class AvoidableLossFinding(BaseModel):
    """The answer to the user's single most important post-mortem
    question: "what concrete decision available at time T could have
    reduced this loss without unacceptable damage to winning trades?"

    `policy` is None when the honest answer is "none of the alternatives
    we can evaluate would have helped" - which is a real and frequent
    answer, and far more useful than a fabricated one.
    """

    policy: str | None = None
    decision_available_at_minutes: float | None = None
    estimated_pnl_improvement_usdt: Decimal | None = None
    winner_damage_checked: bool = False
    explanation: str = ""


class TradeInvestigation(BaseModel):
    """One complete BEFORE / DURING / AFTER post-mortem of one closed
    position. `before`/`during`/`after` are open dicts holding the
    already-persisted source data verbatim - this record never becomes a
    second, divergent copy of the truth, it is an index into it plus the
    derived judgements."""

    position_id: str
    candidate_id: str
    instrument: str
    created_at: datetime
    classification: TradeClassification
    entry_verdict: QualityVerdict
    management_verdict: QualityVerdict
    before: dict
    during: dict
    after: dict
    reason_codes: list[str] = Field(default_factory=list)
    avoidable_loss: AvoidableLossFinding = Field(default_factory=AvoidableLossFinding)
    run_id: str


# --------------------------------------------------------------------
# Decision Auditor
# --------------------------------------------------------------------

ComponentStance = Literal["BULLISH", "BEARISH", "NEUTRAL", "PASS", "FAIL", "UNKNOWN"]
ComponentVerdictValue = Literal["RIGHT", "WRONG", "UNSCORABLE"]
FaultDomain = Literal[
    "SIGNAL_SELECTION", "POSITION_MANAGEMENT", "BOTH", "NEITHER", "UNKNOWN"
]


class ComponentVerdict(BaseModel):
    """What one pipeline component said before entry, and whether it
    turned out to be right. `verdict` is UNSCORABLE - never a coin-flip
    guess - whenever the component made no directional commitment that
    the real outcome could contradict."""

    component: str
    stance: ComponentStance
    expectation: str
    verdict: ComponentVerdictValue
    weight: float = 0.0
    evidence: dict = Field(default_factory=dict)


class DecisionAudit(BaseModel):
    position_id: str
    candidate_id: str
    created_at: datetime
    components: list[ComponentVerdict]
    conflicts: list[str] = Field(default_factory=list)
    misleading_components: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    fault_domain: FaultDomain
    run_id: str


# --------------------------------------------------------------------
# Counterfactual Engine
# --------------------------------------------------------------------

CounterfactualPolicy = Literal[
    "BASELINE",
    "REJECT_ENTRY",
    "DELAY_ENTRY_30M",
    "DELAY_ENTRY_60M",
    "EXIT_ON_THESIS_INVALID",
    "EXIT_ON_THESIS_WEAKENING",
    "TIGHTEN_SL_AFTER_FAVORABLE",
    "REDUCE_ON_WEAKENING",
    "SAFE_TP_AT_HALF_TARGET",
]


class CounterfactualResult(BaseModel):
    """A SIMULATED outcome. `no_lookahead_verified` is asserted by the
    engine itself (it evaluates each policy against a strictly truncated
    prefix of the path); a False value disqualifies the row from every
    aggregation rather than merely annotating it."""

    counterfactual_id: str
    position_id: str
    policy: CounterfactualPolicy
    created_at: datetime
    triggered: bool
    trigger_minutes: float | None = None
    simulated_exit_price: Decimal | None = None
    simulated_pnl_usdt: Decimal | None = None
    actual_pnl_usdt: Decimal | None = None
    delta_pnl_usdt: Decimal | None = None
    no_lookahead_verified: bool = True
    detail: dict = Field(default_factory=dict)
    run_id: str


# --------------------------------------------------------------------
# Experience Memory
# --------------------------------------------------------------------

EdgeClass = Literal[
    "EDGE",
    "WEAK_EDGE",
    "REGIME_DEPENDENT",
    "NOISE",
    "FAILURE_PATTERN",
    "DECAYING_EDGE",
    "INSUFFICIENT_DATA",
]


class ExperiencePattern(BaseModel):
    """One pattern's complete, auditable evidence file.

    Every statistical field is stored, not just the verdict, so that
    `edge_class` can be re-derived from this row alone. A verdict nobody
    can re-derive is a belief, and this system is not allowed to hold
    beliefs about its own money.
    """

    pattern_id: str
    pattern_family: str
    pattern_key: str
    condition: dict
    computed_at: datetime
    sample_size: int
    win_count: int
    win_rate: float | None = None
    wilson_low: float | None = None
    wilson_high: float | None = None
    expectancy_usdt: Decimal | None = None
    expectancy_ci_low: Decimal | None = None
    expectancy_ci_high: Decimal | None = None
    avg_mfe_pct: Decimal | None = None
    avg_mae_pct: Decimal | None = None
    avg_minutes_to_mfe: float | None = None
    baseline_win_rate: float | None = None
    baseline_expectancy_usdt: Decimal | None = None
    lift_expectancy_usdt: Decimal | None = None
    p_value: float | None = None
    fdr_significant: bool = False
    first_half_lift: Decimal | None = None
    second_half_lift: Decimal | None = None
    regime_breakdown: dict = Field(default_factory=dict)
    edge_class: EdgeClass
    confidence: float = 0.0
    survived_walk_forward: bool = False
    detail: dict = Field(default_factory=dict)
    run_id: str


# --------------------------------------------------------------------
# Prediction Error Loop
# --------------------------------------------------------------------

PredictionErrorSource = Literal[
    "trade_thesis",
    "forecast_agent",
    "risk_agent",
    "entry_quality",
    "guardian_authority",
]


class PredictionErrorRecord(BaseModel):
    """EXPECTED / ACTUAL / ERROR / CAUSE / LESSON, exactly as the user
    specified it. `magnitude` is the signed size of the miss in whatever
    unit the source predicts in, so errors can be averaged and calibrated
    rather than only counted."""

    prediction_error_id: str
    position_id: str
    source: PredictionErrorSource
    created_at: datetime
    expected: str
    actual: str
    error: str
    cause: str
    lesson: str
    magnitude: float | None = None
    detail: dict = Field(default_factory=dict)
    run_id: str


# --------------------------------------------------------------------
# Position Thesis tracking
# --------------------------------------------------------------------

ThesisState = Literal["STRONG", "VALID", "WEAKENING", "INVALID", "EXIT"]
ThesisAction = Literal["HOLD", "PROTECT", "TIGHTEN_SL", "REDUCE", "EXIT"]


class ThesisObservation(BaseModel):
    """One deterministic thesis evaluation of one open position at one
    moment. `enforced` is False for everything this phase writes -
    Guardian Authority stays the only execution path."""

    thesis_id: str
    position_id: str
    observed_at: datetime
    thesis_state: ThesisState
    recommended_action: ThesisAction
    enforced: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    features: dict = Field(default_factory=dict)
    run_id: str


# --------------------------------------------------------------------
# Entry Quality Layer
# --------------------------------------------------------------------

EntryVerdict = Literal["TRADE", "WAIT", "REJECT"]


class EntryQualityAssessment(BaseModel):
    """The layer that is allowed to say REJECT to a signal the old Gate
    already approved (user requirement 7) - advisory in this phase, which
    is what `enforced=False` records. `expected_edge_class` comes straight
    from Experience Memory, so an INSUFFICIENT_DATA history yields an
    honest INSUFFICIENT_DATA here rather than a confident number."""

    candidate_id: str
    instrument: str
    assessed_at: datetime
    verdict: EntryVerdict
    quality_score: float
    expected_edge_class: EdgeClass
    expected_expectancy_usdt: Decimal | None = None
    risk_reward: Decimal | None = None
    regime_compatible: bool | None = None
    conflict_score: float = 0.0
    expected_cost_usdt: Decimal | None = None
    enforced: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    detail: dict = Field(default_factory=dict)
    run_id: str
