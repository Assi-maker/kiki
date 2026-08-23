import logging
import sys
from datetime import UTC, datetime

from intelligence.agents.runner import MockAgentRunner
from intelligence.run import build_orchestrator
from intelligence.schemas.event import Event


def test_build_orchestrator_uses_mock_runner_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH_OVERRIDE", str(tmp_path / "t.db"))
    orch = build_orchestrator(use_mock=True, mock_fixtures={})
    assert isinstance(orch._runner, MockAgentRunner)


def test_build_orchestrator_default_mock_fixtures_reach_reported_status(tmp_path, monkeypatch):
    # No mock_fixtures passed -> build_orchestrator must fall back to its own
    # default happy-path fixtures instead of MockAgentRunner's empty-dict
    # KeyError-on-lookup behavior (that behavior is only exercised when a
    # caller explicitly passes fixtures={}, as in the test above).
    monkeypatch.setenv("DB_PATH_OVERRIDE", str(tmp_path / "t.db"))
    orch = build_orchestrator(use_mock=True)
    assert isinstance(orch._runner, MockAgentRunner)

    event = Event(
        event_id="evt-run-1",
        source_id="hn",
        observed_at=datetime.now(UTC),
        category="forum",
        metric="score",
        baseline=50.0,
        deviation=400.0,
        description="d",
        raw_ref="hash-run-1",
    )

    opportunity = orch.process_event(event, run_id="run-1")

    assert opportunity.status == "reported"


def test_main_logs_when_events_truncated_by_max_opportunities(tmp_path, monkeypatch, caplog):
    # Finding #3 (item 2): unlike the other run.py limits, event-count
    # truncation by max_opportunities_per_run previously logged nothing.
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("DB_PATH_OVERRIDE", str(tmp_path / "t.db"))
    monkeypatch.setenv("MAX_OPPORTUNITIES_PER_RUN", "1")
    monkeypatch.setattr(sys, "argv", ["run.py", "--mock"])

    import intelligence.run as run_module

    events = [
        Event(
            event_id=f"evt-{i}",
            source_id="hn",
            observed_at=datetime.now(UTC),
            category="forum",
            metric="score",
            baseline=50.0,
            deviation=400.0,
            description="d",
            raw_ref=f"hash-{i}",
        )
        for i in range(3)
    ]
    monkeypatch.setattr(run_module, "run_event_pipeline", lambda **kwargs: events)

    run_module.main()

    combined = "\n".join(r.getMessage() for r in caplog.records if r.name == "intelligence")
    assert "max_opportunities_truncated" in combined
