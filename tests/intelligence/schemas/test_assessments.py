from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)

_BASE = dict(agent_name="test-agent", run_id="r1", created_at=datetime.now(UTC), status="ok")


def test_research_assessment():
    a = ResearchAssessment(
        **_BASE,
        verified_facts=["X hände enligt källa Y"],
        source_references=["https://example.com"],
        assumptions=["Antar att data är aktuell"],
    )
    assert a.status == "ok"


def test_opportunity_assessment():
    a = OpportunityAssessment(
        **_BASE,
        observed_data="Ovanlig volymökning",
        hypothesis="Efterfrågan stiger",
        interpretation="Möjlig tidig signal",
    )
    assert a.hypothesis


def test_market_assessment():
    a = MarketAssessment(
        **_BASE,
        market_data={"price_change_pct": 12.3, "volume_change_pct": 300.0},
        interpretation="Ovanlig rörelse",
    )
    assert a.market_data["price_change_pct"] == 12.3


def test_forecast_assessment():
    a = ForecastAssessment(
        **_BASE,
        scenarios=[{"description": "Fortsatt uppgång", "probability": 0.4}],
        confidence=0.5,
        uncertainty="Litet dataunderlag",
    )
    assert a.confidence == 0.5


def test_risk_assessment():
    a = RiskAssessment(
        **_BASE,
        downside="Kan reversera snabbt",
        liquidity_risk="Låg volym",
        model_risk="Litet urval",
        timing_risk="Sent i rörelsen",
    )
    assert a.downside


def test_bear_assessment():
    a = BearAssessment(
        **_BASE,
        counterarguments=["Kan vara brus"],
        alternative_explanations=["Säsongseffekt"],
        falsification_conditions="Om volymen normaliseras inom 48h",
    )
    assert a.falsification_conditions


def test_qa_assessment():
    a = QAAssessment(**_BASE, passed=True, violations=[])
    assert a.passed is True


def test_invalid_status_rejected():
    with pytest.raises(ValidationError):
        QAAssessment(
            agent_name="x",
            run_id="r1",
            created_at=datetime.now(UTC),
            status="maybe",
            passed=True,
            violations=[],
        )
