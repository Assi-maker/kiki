import logging
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
def test_real_runner_disables_sdk_level_retries(mock_anthropic_cls):
    # RealClaudeRunner.run() already implements its own retry loop
    # (self._max_retries). The SDK's own default max_retries=2 retries
    # timeouts internally too, nesting under our loop and multiplying
    # worst-case wall time (attempts x SDK_attempts x timeout_seconds) — so
    # the client must be constructed with max_retries=0 to keep a single
    # source of retry truth.
    RealClaudeRunner(api_key="fake-key", model="claude-sonnet-5", timeout_seconds=5, max_retries=3)
    mock_anthropic_cls.assert_called_once_with(api_key="fake-key", max_retries=0)


@patch("intelligence.agents.runner.Anthropic")
def test_real_runner_applies_per_agent_timeout_override(mock_anthropic_cls):
    # opportunity-hunter's real-run latency sat right at the default timeout
    # (2026-08-23 verification run) — it gets a raised per-agent timeout via
    # timeout_overrides, while every other agent keeps timeout_seconds.
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(type="text", text='{"passed": true, "violations": []}')]
    mock_client.messages.create.return_value = mock_message

    runner = RealClaudeRunner(
        api_key="fake-key",
        model="claude-sonnet-5",
        timeout_seconds=30,
        max_retries=1,
        timeout_overrides={"opportunity-hunter": 45},
    )

    runner.run(_agent_def(name="opportunity-hunter"), context={}, output_schema=QAAssessment)
    assert mock_client.messages.create.call_args.kwargs["timeout"] == 45

    runner.run(_agent_def(name="qa-agent"), context={}, output_schema=QAAssessment)
    assert mock_client.messages.create.call_args.kwargs["timeout"] == 30


@patch("intelligence.agents.runner.Anthropic")
def test_real_runner_applies_fact_checker_bear_timeout_override(mock_anthropic_cls):
    # fact-checker-bear showed the same near-default-timeout latency symptom
    # as opportunity-hunter in a later verification run — it gets its own
    # raised per-agent timeout (50s) via timeout_overrides, while every other
    # agent (including opportunity-hunter, on its own separate override)
    # keeps timeout_seconds.
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(type="text", text='{"passed": true, "violations": []}')]
    mock_client.messages.create.return_value = mock_message

    runner = RealClaudeRunner(
        api_key="fake-key",
        model="claude-sonnet-5",
        timeout_seconds=30,
        max_retries=1,
        timeout_overrides={"opportunity-hunter": 45, "fact-checker-bear": 50},
    )

    runner.run(_agent_def(name="fact-checker-bear"), context={}, output_schema=QAAssessment)
    assert mock_client.messages.create.call_args.kwargs["timeout"] == 50

    runner.run(_agent_def(name="opportunity-hunter"), context={}, output_schema=QAAssessment)
    assert mock_client.messages.create.call_args.kwargs["timeout"] == 45

    runner.run(_agent_def(name="qa-agent"), context={}, output_schema=QAAssessment)
    assert mock_client.messages.create.call_args.kwargs["timeout"] == 30


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
def test_real_runner_returns_failed_status_on_non_object_json(mock_anthropic_cls):
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(type="text", text="[1, 2, 3]")]
    mock_client.messages.create.return_value = mock_message

    runner = RealClaudeRunner(
        api_key="fake-key", model="claude-sonnet-5", timeout_seconds=5, max_retries=1
    )
    result = runner.run(_agent_def(), context={"question": "test"}, output_schema=QAAssessment)
    assert result.status == "failed"


@patch("intelligence.agents.runner.Anthropic")
def test_real_runner_logs_each_retry_failure_instead_of_swallowing_it(mock_anthropic_cls, caplog):
    # Finding #4: the retry loop's except/continue previously swallowed every
    # retry failure with zero logging, leaving no diagnostic trace of *why* a
    # fully-failed agent failed.
    caplog.set_level(logging.INFO)
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(type="text", text="detta är inte json")]
    mock_client.messages.create.return_value = mock_message

    runner = RealClaudeRunner(
        api_key="fake-key", model="claude-sonnet-5", timeout_seconds=5, max_retries=2
    )
    result = runner.run(_agent_def(), context={"run_id": "r1"}, output_schema=QAAssessment)

    assert result.status == "failed"
    combined = "\n".join(r.getMessage() for r in caplog.records if r.name == "intelligence")
    assert combined.count("agent_retry_failed") == 2
    assert "JSONDecodeError" in combined


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
