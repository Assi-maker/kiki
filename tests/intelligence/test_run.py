from intelligence.agents.runner import MockAgentRunner
from intelligence.run import build_orchestrator


def test_build_orchestrator_uses_mock_runner_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH_OVERRIDE", str(tmp_path / "t.db"))
    orch = build_orchestrator(use_mock=True, mock_fixtures={})
    assert isinstance(orch._runner, MockAgentRunner)
