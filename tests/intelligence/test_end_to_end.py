from datetime import UTC, datetime

from intelligence.agents.runner import MockAgentRunner
from intelligence.config import get_settings
from intelligence.connectors.base import BaseConnector
from intelligence.orchestrator import Orchestrator
from intelligence.pipeline.event_pipeline import run_event_pipeline
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source
from intelligence.scoring.model import load_weights
from intelligence.storage.repository import SQLiteRepository

_A = dict(run_id="r1", created_at=datetime.now(UTC), status="ok")


class _FixtureConnector(BaseConnector):
    def fetch(self):
        payload = {"id": 1, "score": 900}
        return [
            RawRecord(
                source_id=self.source.source_id,
                fetched_at=datetime.now(UTC),
                payload=payload,
                content_hash=self._content_hash(payload),
            )
        ]


def test_full_pipeline_from_data_to_markdown_report(tmp_path):
    repo = SQLiteRepository(tmp_path / "e2e.db")
    source = Source(
        source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://x.com"
    )
    repo.save_source(source)

    connector = _FixtureConnector(source, timeout_seconds=5, max_retries=1, min_interval_seconds=0)
    events = run_event_pipeline(
        connectors=[connector],
        source_types={"hn": "forum"},
        baselines={"hn": 50.0},
        repo=repo,
        max_events=10,
        run_id="e2e-run",
    )
    assert len(events) == 1

    fixtures = {
        "research-agent": ResearchAssessment(
            **_A,
            agent_name="research-agent",
            verified_facts=["f"],
            source_references=["s"],
            assumptions=[],
        ),
        "opportunity-hunter": OpportunityAssessment(
            **_A,
            agent_name="opportunity-hunter",
            observed_data="d",
            hypothesis="h",
            interpretation="i",
        ),
        "trading-research": MarketAssessment(
            **_A,
            agent_name="trading-research",
            market_data={},
            interpretation="i",
        ),
        "forecasting-agent": ForecastAssessment(
            **_A,
            agent_name="forecasting-agent",
            scenarios=[{"description": "up", "probability": 0.5}],
            confidence=0.5,
            uncertainty="u",
        ),
        "risk-agent": RiskAssessment(
            **_A,
            agent_name="risk-agent",
            downside="d",
            liquidity_risk="l",
            model_risk="m",
            timing_risk="t",
        ),
        "fact-checker-bear": BearAssessment(
            **_A,
            agent_name="fact-checker-bear",
            counterarguments=[],
            alternative_explanations=[],
            falsification_conditions="f",
        ),
        "qa-agent": QAAssessment(**_A, agent_name="qa-agent", passed=True, violations=[]),
    }
    runner = MockAgentRunner(fixtures=fixtures)
    weights = load_weights(get_settings().scoring_weights_path)
    settings = get_settings()
    orchestrator = Orchestrator(
        repo=repo,
        runner=runner,
        weights=weights,
        settings=settings,
        report_dest_dir=tmp_path,
    )

    opportunity = orchestrator.process_event(events[0], run_id="e2e-run")

    assert opportunity.status == "reported"
    assert opportunity.score is not None

    stored = repo.get_opportunity(opportunity.opportunity_id)
    assert stored.status == "reported"

    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 1
    content = report_files[0].read_text(encoding="utf-8")
    assert f"OPPORTUNITY #{opportunity.opportunity_id}" in content
    assert "Status:" in content
