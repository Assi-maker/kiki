from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic import APIConnectionError

from intelligence.agents.loader import AgentDefinition
from intelligence.agents.runner import MockAgentRunner, RealClaudeRunner
from intelligence.schemas.assessments import QAAssessment


def _agent_def(name="qa-agent"):
    return AgentDefinition(
        name=name, description="d", tools=["Read"], system_prompt="Du är QA Agent."
    )


def _qa_ok():
    return QAAssessment(
        agent_name="qa-agent",
        run_id="r1",
        created_at=datetime.now(UTC),
        status="ok",
        passed=True,
        violations=[],
    )


def test_mock_runner_returns_configured_fixture():
    runner = MockAgentRunner(fixtures={"qa-agent": _qa_ok()})
    result = runner.run(_agent_def(), context={}, output_schema=QAAssessment)
    assert result.passed is True


def test_mock_runner_simulates_failure():
    runner = MockAgentRunner(fixtures={"qa-agent": _qa_ok()}, fail_agents={"qa-agent"})
    result = runner.run(_agent_def(), context={}, output_schema=QAAssessment)
    assert result.status == "failed"


def test_mock_runner_simulates_timeout():
    runner = MockAgentRunner(fixtures={"qa-agent": _qa_ok()}, timeout_agents={"qa-agent"})
    result = runner.run(_agent_def(), context={}, output_schema=QAAssessment)
    assert result.status == "timeout"


def test_mock_runner_missing_fixture_raises_key_error():
    runner = MockAgentRunner(fixtures={})
    with pytest.raises(KeyError):
        runner.run(_agent_def(), context={}, output_schema=QAAssessment)


@patch("intelligence.agents.runner.Anthropic")
def test_real_runner_returns_failed_status_on_invalid_json(mock_anthropic_cls):
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(type="text", text="detta är inte json")]
    mock_client.messages.create.return_value = mock_message

    runner = RealClaudeRunner(
        api_key="fake-key", model="claude-sonnet-5", timeout_seconds=5, max_retries=1
    )
    result = runner.run(_agent_def(), context={"question": "test"}, output_schema=QAAssessment)
    assert result.status == "failed"


@patch("intelligence.agents.runner.Anthropic")
def test_real_runner_returns_failed_status_on_api_error(mock_anthropic_cls):
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    mock_client.messages.create.side_effect = APIConnectionError(request=request)

    runner = RealClaudeRunner(
        api_key="fake-key", model="claude-sonnet-5", timeout_seconds=5, max_retries=1
    )
    result = runner.run(_agent_def(), context={"question": "test"}, output_schema=QAAssessment)
    assert result.status == "failed"
