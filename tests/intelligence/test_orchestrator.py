# tests/intelligence/test_orchestrator.py
from datetime import UTC, datetime

from intelligence.agents.runner import MockAgentRunner
from intelligence.config import get_settings
from intelligence.orchestrator import Orchestrator
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.event import Event
from intelligence.scoring.model import load_weights
from intelligence.storage.repository import SQLiteRepository

_A = dict(run_id="r1", created_at=datetime.now(UTC), status="ok")


def _event():
    return Event(
        event_id="evt-1", source_id="hn", observed_at=datetime.now(UTC), category="forum",
        metric="score", baseline=50.0, deviation=400.0, description="d", raw_ref="hash-1",
    )


def _happy_fixtures():
    return {
        "research-agent": ResearchAssessment(
            **_A, agent_name="research-agent",
            verified_facts=["f"], source_references=["s"], assumptions=[],
        ),
        "opportunity-hunter": OpportunityAssessment(
            **_A, agent_name="opportunity-hunter",
            observed_data="d", hypothesis="h", interpretation="i",
        ),
        "trading-research": MarketAssessment(
            **_A, agent_name="trading-research", market_data={}, interpretation="i",
        ),
        "forecasting-agent": ForecastAssessment(
            **_A, agent_name="forecasting-agent",
            scenarios=[{"description": "up", "probability": 0.6}],
            confidence=0.6, uncertainty="u",
        ),
        "risk-agent": RiskAssessment(
            **_A, agent_name="risk-agent",
            downside="d", liquidity_risk="l", model_risk="m", timing_risk="t",
        ),
        "fact-checker-bear": BearAssessment(
            **_A, agent_name="fact-checker-bear",
            counterarguments=[], alternative_explanations=[], falsification_conditions="f",
        ),
        "qa-agent": QAAssessment(**_A, agent_name="qa-agent", passed=True, violations=[]),
    }


def _orchestrator(tmp_path, fixtures=None, fail_agents=None, dest_dir=None):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(
        fixtures=fixtures or _happy_fixtures(), fail_agents=fail_agents or set()
    )
    weights = load_weights(get_settings().scoring_weights_path)
    settings = get_settings()
    return Orchestrator(
        repo=repo, runner=runner, weights=weights, settings=settings,
        report_dest_dir=dest_dir or tmp_path,
    )


def test_happy_path_reaches_reported_status(tmp_path):
    orch = _orchestrator(tmp_path)
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status == "reported"
    assert opp.score is not None
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 1


def test_failed_risk_agent_blocks_reported(tmp_path):
    orch = _orchestrator(tmp_path, fail_agents={"risk-agent"})
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status != "reported"
    assert opp.risk is not None
    assert opp.risk.status == "failed"
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 0


def test_qa_rejection_sets_status_rejected(tmp_path):
    fixtures = _happy_fixtures()
    fixtures["qa-agent"] = QAAssessment(
        **_A, agent_name="qa-agent", passed=False, violations=["saknar riskbedömning"]
    )
    orch = _orchestrator(tmp_path, fixtures=fixtures)
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status == "rejected"
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 0


def test_qa_agent_infra_failure_sets_status_under_review_not_rejected(tmp_path):
    # A pure infrastructure failure of the qa-agent (status="failed") must never be
    # mislabeled as the terminal "rejected" status, even though a real runner's
    # blank-fill would leave passed=False on such a failure. Only a qa-agent that
    # actually ran (status="ok") and explicitly failed (passed=False) is "rejected".
    fixtures = _happy_fixtures()
    qa_kwargs = {**_A, "agent_name": "qa-agent", "status": "failed"}
    fixtures["qa-agent"] = QAAssessment(**qa_kwargs, passed=False, violations=[])
    orch = _orchestrator(tmp_path, fixtures=fixtures)
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status == "under_review"
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 0
