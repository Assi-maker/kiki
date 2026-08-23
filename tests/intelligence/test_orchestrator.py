# tests/intelligence/test_orchestrator.py
import logging
from datetime import UTC, datetime

from intelligence.agents.runner import AgentRunner, MockAgentRunner
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
        event_id="evt-1",
        source_id="hn",
        observed_at=datetime.now(UTC),
        category="forum",
        metric="score",
        baseline=50.0,
        deviation=400.0,
        description="d",
        raw_ref="hash-1",
    )


def _event_with_content():
    return Event(
        event_id="evt-1",
        source_id="hn",
        observed_at=datetime.now(UTC),
        category="forum",
        metric="score",
        baseline=50.0,
        deviation=400.0,
        description="d",
        raw_ref="hash-1",
        title="Show HN: I built a thing",
        url="https://example.com/thing",
        author="someuser",
        content_excerpt="A self-text body",
    )


def _happy_fixtures():
    return {
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
            scenarios=[{"description": "up", "probability": 0.6}],
            confidence=0.6,
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


class _ContextCapturingRunner(AgentRunner):
    """Delegates to a MockAgentRunner but records every context dict it is
    called with, keyed by agent name, so a test can inspect exactly what an
    agent would have received."""

    def __init__(self, fixtures):
        self._delegate = MockAgentRunner(fixtures=fixtures)
        self.contexts_by_agent: dict[str, dict] = {}

    def run(self, agent_def, context, output_schema):
        self.contexts_by_agent[agent_def.name] = context
        return self._delegate.run(agent_def, context, output_schema)


def _orchestrator(tmp_path, fixtures=None, fail_agents=None, dest_dir=None):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(
        fixtures=fixtures or _happy_fixtures(), fail_agents=fail_agents or set()
    )
    weights = load_weights(get_settings().scoring_weights_path)
    settings = get_settings()
    return Orchestrator(
        repo=repo,
        runner=runner,
        weights=weights,
        settings=settings,
        report_dest_dir=dest_dir or tmp_path,
    )


def test_happy_path_reaches_reported_status(tmp_path):
    orch = _orchestrator(tmp_path)
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status == "reported"
    assert opp.score is not None
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 1


def test_agent_context_includes_event_content_metadata(tmp_path):
    # Fas 2: opportunity-hunter/trading-research previously only ever saw a bare
    # numeric score deviation (no title/url/author/content_excerpt) in their
    # context, which they correctly reported as insufficient underlag. Verify
    # the full pipeline (Event -> orchestrator context) actually carries it
    # through to what the agent receives, for every role, not just the DB row.
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = _ContextCapturingRunner(fixtures=_happy_fixtures())
    weights = load_weights(get_settings().scoring_weights_path)
    settings = get_settings()
    orch = Orchestrator(
        repo=repo, runner=runner, weights=weights, settings=settings, report_dest_dir=tmp_path
    )

    orch.process_event(_event_with_content(), run_id="r1")

    opportunity_context = runner.contexts_by_agent["opportunity-hunter"]
    assert opportunity_context["event"]["title"] == "Show HN: I built a thing"
    assert opportunity_context["event"]["url"] == "https://example.com/thing"
    assert opportunity_context["event"]["author"] == "someuser"
    assert opportunity_context["event"]["content_excerpt"] == "A self-text body"

    market_context = runner.contexts_by_agent["trading-research"]
    assert market_context["event"]["title"] == "Show HN: I built a thing"


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


def test_max_agent_calls_per_run_persists_across_events_not_reset_per_event(tmp_path, caplog):
    # Finding #3: agent_calls must be scoped per RUN, not reset for every event.
    # 7 roles/event; limit=10 -> event 1 consumes 7, event 2 (same run_id) should
    # be cut off after 3 more roles (at 10 total), not get a fresh budget of 7.
    caplog.set_level(logging.INFO)
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    settings = get_settings()
    weights = load_weights(settings.scoring_weights_path)
    low_limit_settings = settings.model_copy(update={"max_agent_calls_per_run": 10})
    orch = Orchestrator(
        repo=repo,
        runner=runner,
        weights=weights,
        settings=low_limit_settings,
        report_dest_dir=tmp_path,
    )

    opp1 = orch.process_event(_event(), run_id="r1")
    assert opp1.status == "reported"

    opp2 = orch.process_event(_event(), run_id="r1")
    assert opp2.status != "reported"
    assert opp2.qa is None  # cut off before reaching the qa role
    assert "max_agent_calls_reached" in "\n".join(caplog.messages)


def test_max_agent_calls_per_run_resets_on_new_run_id(tmp_path):
    # A fresh run_id must get a fresh budget, not inherit exhaustion from a
    # previous run processed by the same long-lived Orchestrator instance.
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    settings = get_settings()
    weights = load_weights(settings.scoring_weights_path)
    low_limit_settings = settings.model_copy(update={"max_agent_calls_per_run": 7})
    orch = Orchestrator(
        repo=repo,
        runner=runner,
        weights=weights,
        settings=low_limit_settings,
        report_dest_dir=tmp_path,
    )

    opp1 = orch.process_event(_event(), run_id="run-a")
    assert opp1.status == "reported"

    opp2 = orch.process_event(_event(), run_id="run-b")
    assert opp2.status == "reported"


def test_process_event_writes_log_run_event_rows_per_role(tmp_path):
    # Finding #4: Repository.log_run_event (SPEC §10 observability — started_at/
    # completed_at/latency_ms/errors) is fully implemented but previously had
    # zero production call sites. It must actually be called once per agent
    # role during a real process_event run.
    orch = _orchestrator(tmp_path)
    orch.process_event(_event(), run_id="r1")

    rows = orch._repo._conn.execute("SELECT * FROM runs WHERE run_id = 'r1'").fetchall()
    assert len(rows) == 7  # one per role in _ROLE_ORDER
    for row in rows:
        assert row["agent_name"]
        assert row["status"] == "ok"
        assert row["started_at"] is not None
        assert row["completed_at"] is not None
        assert row["latency_ms"] is not None and row["latency_ms"] >= 0


def test_report_write_failure_does_not_crash_process_event(tmp_path, monkeypatch):
    # Finding #2: a report-generation failure (e.g. a differently-shaped
    # scenario dict slipping past pydantic validation and crashing
    # render_report) must not abort the whole run. The opportunity's
    # "reported" status was already committed to the DB before write_report
    # runs, and must stay that way.
    import intelligence.orchestrator as orchestrator_module

    def _boom(*args, **kwargs):
        raise KeyError("simulated render failure")

    monkeypatch.setattr(orchestrator_module, "write_report", _boom)

    orch = _orchestrator(tmp_path)
    opp = orch.process_event(_event(), run_id="r1")

    assert opp.status == "reported"
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
