from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from intelligence.agents.runner import AgentRunner, MockAgentRunner, RealClaudeRunner
from intelligence.config import get_settings
from intelligence.connectors.alpha_vantage import AlphaVantageConnector
from intelligence.connectors.exceptions import ConnectorConfigError
from intelligence.connectors.hackernews import HackerNewsConnector
from intelligence.logging import log_event, new_run_id
from intelligence.orchestrator import Orchestrator
from intelligence.pipeline.event_pipeline import run_event_pipeline
from intelligence.schemas.assessments import (
    AssessmentBase,
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.source import Source
from intelligence.scoring.model import load_weights
from intelligence.storage.repository import SQLiteRepository

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_mock_fixtures() -> dict[str, AssessmentBase]:
    """Happy-path fixtures for --mock's demo run against real HN data.

    One passing (status="ok") assessment per agent role, matching the shape
    proven by ``_happy_fixtures()`` in tests/intelligence/test_orchestrator.py
    and tests/intelligence/test_end_to_end.py — enough to satisfy the state
    machine's REQUIRED_FOR_REPORTED gate so the demo run reaches a terminal
    status instead of crashing on a missing fixture.
    """
    common = dict(run_id="mock", created_at=datetime.now(UTC), status="ok")
    return {
        "research-agent": ResearchAssessment(
            **common,
            agent_name="research-agent",
            verified_facts=["f"],
            source_references=["s"],
            assumptions=[],
        ),
        "opportunity-hunter": OpportunityAssessment(
            **common,
            agent_name="opportunity-hunter",
            observed_data="d",
            hypothesis="h",
            interpretation="i",
        ),
        "trading-research": MarketAssessment(
            **common,
            agent_name="trading-research",
            market_data={},
            interpretation="i",
        ),
        "forecasting-agent": ForecastAssessment(
            **common,
            agent_name="forecasting-agent",
            scenarios=[{"description": "up", "probability": 0.6}],
            confidence=0.6,
            uncertainty="u",
        ),
        "risk-agent": RiskAssessment(
            **common,
            agent_name="risk-agent",
            downside="d",
            liquidity_risk="l",
            model_risk="m",
            timing_risk="t",
        ),
        "fact-checker-bear": BearAssessment(
            **common,
            agent_name="fact-checker-bear",
            counterarguments=[],
            alternative_explanations=[],
            falsification_conditions="f",
        ),
        "qa-agent": QAAssessment(**common, agent_name="qa-agent", passed=True, violations=[]),
    }


def build_orchestrator(use_mock: bool, mock_fixtures: dict | None = None) -> Orchestrator:
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path)
    weights = load_weights(settings.scoring_weights_path)

    runner: AgentRunner
    if use_mock or not settings.anthropic_api_key:
        fixtures = mock_fixtures if mock_fixtures is not None else _default_mock_fixtures()
        runner = MockAgentRunner(fixtures=fixtures)
    else:
        runner = RealClaudeRunner(
            api_key=settings.anthropic_api_key,
            model="claude-sonnet-5",
            timeout_seconds=settings.agent_timeout_seconds,
            max_retries=3,
        )

    return Orchestrator(
        repo=repo,
        runner=runner,
        weights=weights,
        settings=settings,
        report_dest_dir=_PROJECT_ROOT / "research",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Kör en Fas 1-pipeline-cykel")
    parser.add_argument(
        "--mock", action="store_true", help="Använd MockAgentRunner även om ANTHROPIC_API_KEY finns"
    )
    args = parser.parse_args()

    settings = get_settings()
    run_id = new_run_id()
    repo = SQLiteRepository(settings.db_path)

    hn_source = Source(
        source_id="hn",
        name="Hacker News",
        type="forum",
        reliability_score=0.6,
        url="https://news.ycombinator.com",
    )
    av_source = Source(
        source_id="alpha_vantage",
        name="Alpha Vantage",
        type="market_data",
        reliability_score=0.8,
        url="https://www.alphavantage.co",
    )
    repo.save_source(hn_source)
    repo.save_source(av_source)

    connectors = [
        HackerNewsConnector(
            hn_source,
            settings.connector_timeout_seconds,
            settings.connector_max_retries,
            min_interval_seconds=1.0,
        )
    ]
    try:
        connectors.append(
            AlphaVantageConnector(
                av_source,
                settings.connector_timeout_seconds,
                settings.connector_max_retries,
                api_key=settings.alphavantage_api_key,
                symbols=["IBM"],
                min_interval_seconds=12.0,
            )
        )
    except ConnectorConfigError as exc:
        log_event(run_id, event="connector_skipped", source_id="alpha_vantage", error=str(exc))

    events = run_event_pipeline(
        connectors=connectors,
        source_types={"hn": "forum", "alpha_vantage": "market_data"},
        baselines={"hn": 50.0, "alpha_vantage": 100.0},
        repo=repo,
        max_events=settings.max_events_per_run,
        run_id=run_id,
    )

    if len(events) > settings.max_opportunities_per_run:
        log_event(
            run_id,
            event="max_opportunities_truncated",
            total_events=len(events),
            limit=settings.max_opportunities_per_run,
        )

    orchestrator = build_orchestrator(use_mock=args.mock)
    reported = 0
    for event in events[: settings.max_opportunities_per_run]:
        opportunity = orchestrator.process_event(event, run_id)
        if opportunity.status == "reported":
            reported += 1

    print(f"Körning {run_id}: {len(events)} events, {reported} opportunities rapporterade.")


if __name__ == "__main__":
    main()
