from datetime import UTC, datetime
from pathlib import Path

from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.opportunity import Opportunity
from intelligence.scoring.model import load_weights, score_opportunity

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


def _full_opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="opp-1",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="t",
        summary="s",
        time_horizon="7d",
        liquidity="unknown",
        research=ResearchAssessment(
            **_A,
            verified_facts=["a", "b"],
            source_references=["s1", "s2"],
            assumptions=[],
        ),
        opportunity=OpportunityAssessment(
            **_A, observed_data="d", hypothesis="h", interpretation="i"
        ),
        market=MarketAssessment(
            **_A, market_data={"volatility": 0.4}, interpretation="i"
        ),
        forecast=ForecastAssessment(
            **_A,
            scenarios=[{"description": "up", "probability": 0.6}],
            confidence=0.7,
            uncertainty="u",
        ),
        risk=RiskAssessment(
            **_A,
            downside="d",
            liquidity_risk="låg",
            model_risk="m",
            timing_risk="t",
        ),
        bear=BearAssessment(
            **_A,
            counterarguments=["c1"],
            alternative_explanations=[],
            falsification_conditions="f",
        ),
        qa=QAAssessment(**_A, passed=True, violations=[]),
    )


def test_load_weights_from_yaml():
    weights = load_weights(Path("config/scoring_weights.yaml"))
    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_score_opportunity_returns_total_and_breakdown():
    weights = load_weights(Path("config/scoring_weights.yaml"))
    total, breakdown = score_opportunity(_full_opportunity(), weights)
    assert 0.0 <= total <= 1.0
    assert set(breakdown.keys()) == set(weights.keys())
    for component_score in breakdown.values():
        assert 0.0 <= component_score <= 1.0


def test_score_reflects_weighted_sum():
    weights = load_weights(Path("config/scoring_weights.yaml"))
    total, breakdown = score_opportunity(_full_opportunity(), weights)
    expected = sum(weights[k] * breakdown[k] for k in weights)
    assert abs(total - expected) < 1e-9
