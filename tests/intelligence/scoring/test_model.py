from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

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
        market=MarketAssessment(**_A, market_data={"volatility": 0.4}, interpretation="i"),
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


def test_load_weights_raises_value_error_on_missing_key(tmp_path):
    bad = tmp_path / "bad_weights.yaml"
    # "novelty" key missing entirely — the classic YAML-typo case.
    bad.write_text(
        "signal_strength: 0.30\n"
        "data_quality: 0.15\n"
        "source_reliability: 0.15\n"
        "potential: 0.20\n"
        "risk: 0.15\n"
        "confidence: 0.05\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="novelty"):
        load_weights(bad)


def test_load_weights_raises_value_error_on_extra_key(tmp_path):
    bad = tmp_path / "bad_weights.yaml"
    weights = {
        "signal_strength": 0.15,
        "data_quality": 0.15,
        "source_reliability": 0.15,
        "potential": 0.20,
        "risk": 0.15,
        "confidence": 0.10,
        "novelty": 0.05,
        "noveltyy": 0.05,  # typo'd extra key, sum now > 1.0 too
    }
    bad.write_text(yaml.safe_dump(weights), encoding="utf-8")
    with pytest.raises(ValueError, match="noveltyy"):
        load_weights(bad)


def test_load_weights_raises_value_error_when_sum_is_not_one(tmp_path):
    bad = tmp_path / "bad_weights.yaml"
    weights = {
        "signal_strength": 0.10,
        "data_quality": 0.10,
        "source_reliability": 0.10,
        "potential": 0.10,
        "risk": 0.10,
        "confidence": 0.10,
        "novelty": 0.10,
    }  # sums to 0.70, not ~1.0
    bad.write_text(yaml.safe_dump(weights), encoding="utf-8")
    with pytest.raises(ValueError, match="summerar"):
        load_weights(bad)


def test_potential_component_stays_within_bounds_when_scenario_probability_exceeds_one():
    # Finding #7: `probability` is an untyped dict value — an LLM (or a bad
    # fixture) can hand back something outside [0, 1]. The `potential`
    # component must stay clamped to [0, 1] regardless.
    weights = load_weights(Path("config/scoring_weights.yaml"))
    opp = _full_opportunity()
    opp.forecast.scenarios = [{"description": "up", "probability": 1.5}]
    total, breakdown = score_opportunity(opp, weights)
    assert 0.0 <= breakdown["potential"] <= 1.0
    assert 0.0 <= total <= 1.0
