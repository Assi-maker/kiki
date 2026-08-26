from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.schemas.assessments import RiskAssessment


def _agent_def(name="crypto-risk-agent") -> AgentDefinition:
    return AgentDefinition(name=name, description="d", tools=["Read"], system_prompt="p")


def _risk_assessment(status="ok") -> RiskAssessment:
    return RiskAssessment(
        agent_name="crypto-risk-agent",
        run_id="run-1",
        created_at=datetime.now(UTC),
        status=status,
        suggested_stop_loss="42000",
        suggested_target="45000",
        downside="d",
        liquidity_risk="l",
        model_risk="m",
        timing_risk="t",
    )


def test_mock_runner_returns_configured_fixture():
    runner = MockAgentRunner(fixtures={"crypto-risk-agent": _risk_assessment()})
    result = runner.run(_agent_def(), context={}, output_schema=RiskAssessment)
    assert result.status == "ok"
    assert result.downside == "d"


def test_mock_runner_returns_timeout_status_for_configured_agent():
    runner = MockAgentRunner(
        fixtures={"crypto-risk-agent": _risk_assessment()},
        timeout_agents={"crypto-risk-agent"},
    )
    result = runner.run(_agent_def(), context={}, output_schema=RiskAssessment)
    assert result.status == "timeout"


def test_mock_runner_returns_failed_status_for_configured_agent():
    runner = MockAgentRunner(
        fixtures={"crypto-risk-agent": _risk_assessment()},
        fail_agents={"crypto-risk-agent"},
    )
    result = runner.run(_agent_def(), context={}, output_schema=RiskAssessment)
    assert result.status == "failed"


def test_real_claude_runner_parses_valid_json_response():
    from crypto_trading.agents.runner import RealClaudeRunner

    fake_message = MagicMock()
    fake_block = MagicMock(
        type="text",
        text=(
            '{"run_id": "run-1", "downside": "d", "liquidity_risk": "l", '
            '"model_risk": "m", "timing_risk": "t", "suggested_stop_loss": "1", '
            '"suggested_target": "2"}'
        ),
    )
    fake_message.content = [fake_block]

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = fake_message
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=1
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "ok"
    assert result.downside == "d"


def test_real_claude_runner_falls_back_to_failed_status_after_retries_exhausted():
    from anthropic import APIError

    from crypto_trading.agents.runner import RealClaudeRunner

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.side_effect = APIError(
            "boom", request=MagicMock(), body=None
        )
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=2
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "failed"
    assert result.agent_name == "crypto-risk-agent"
