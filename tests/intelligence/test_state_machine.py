from datetime import UTC, datetime

from intelligence.schemas.assessments import BearAssessment, QAAssessment, RiskAssessment
from intelligence.schemas.opportunity import Opportunity
from intelligence.state_machine import can_transition

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


def _opp(**overrides):
    base = dict(
        opportunity_id="opp-1",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="t",
        summary="s",
        time_horizon="7d",
        liquidity="unknown",
    )
    base.update(overrides)
    return Opportunity(**base)


def _fully_assessed(**overrides):
    from intelligence.schemas.assessments import (
        ForecastAssessment,
        MarketAssessment,
        OpportunityAssessment,
        ResearchAssessment,
    )

    assessments = dict(
        research=ResearchAssessment(
            **_A, verified_facts=["f"], source_references=["s"], assumptions=[]
        ),
        opportunity=OpportunityAssessment(
            **_A, observed_data="d", hypothesis="h", interpretation="i"
        ),
        market=MarketAssessment(**_A, market_data={}, interpretation="i"),
        forecast=ForecastAssessment(**_A, scenarios=[], confidence=0.5, uncertainty="u"),
        risk=RiskAssessment(
            **_A, downside="d", liquidity_risk="l", model_risk="m", timing_risk="t"
        ),
        bear=BearAssessment(
            **_A,
            counterarguments=[],
            alternative_explanations=[],
            falsification_conditions="f",
        ),
        qa=QAAssessment(**_A, passed=True, violations=[]),
    )
    assessments.update(overrides)
    return _opp(**assessments)


def test_missing_risk_assessment_blocks_reported():
    opp = _fully_assessed(risk=None)
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "risk" in reason.lower()


def test_missing_bear_assessment_blocks_reported():
    opp = _fully_assessed(bear=None)
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "bear" in reason.lower()


def test_missing_qa_pass_blocks_reported():
    opp = _fully_assessed(qa=QAAssessment(**_A, passed=False, violations=["schema incomplete"]))
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "qa" in reason.lower()


def test_fully_assessed_can_be_reported():
    opp = _fully_assessed()
    ok, reason = can_transition(opp, "reported")
    assert ok is True, reason


def test_rejected_cannot_become_approved():
    opp = _fully_assessed(status="rejected")
    ok, _ = can_transition(opp, "approved")
    assert ok is False


def test_rejected_cannot_become_reported():
    opp = _fully_assessed(status="rejected")
    ok, _ = can_transition(opp, "reported")
    assert ok is False


def test_failed_assessment_blocks_reported():
    failed_bear = BearAssessment(
        agent_name="x",
        run_id="r1",
        created_at=datetime.now(UTC),
        status="failed",
        counterarguments=[],
        alternative_explanations=[],
        falsification_conditions="",
    )
    opp = _fully_assessed(bear=failed_bear)
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "failed" in reason.lower() or "bear" in reason.lower()
