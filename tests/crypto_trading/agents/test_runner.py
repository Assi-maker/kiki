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


def test_real_claude_runner_logs_token_usage_and_estimated_cost_on_success():
    """Kostnadsoptimering (2026-09-02, item 5/6): varje lyckat anrop ska
    logga in/output-tokens + en uppskattad $-kostnad, så att kostnad per
    kandidat/CONFIRMED/paper-trade blir mätbar per discovery-cykel."""
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
    fake_message.usage = MagicMock(
        input_tokens=1000, output_tokens=500, cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )

    with (
        patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic,
        patch("crypto_trading.agents.runner.log_event") as mock_log_event,
    ):
        mock_anthropic.return_value.messages.create.return_value = fake_message
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=1
        )
        runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    usage_calls = [
        c for c in mock_log_event.call_args_list if c.kwargs.get("event") == "agent_call_usage"
    ]
    assert len(usage_calls) == 1
    kwargs = usage_calls[0].kwargs
    assert kwargs["input_tokens"] == 1000
    assert kwargs["output_tokens"] == 500
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["estimated_cost_usd"] > 0


def test_real_claude_runner_logs_usage_even_when_response_fails_to_parse():
    """En trasig/kodblocksinlindad respons som ändå genererades av modellen
    kostade riktiga tokens - den kostnaden ska synas i loggen även om
    svaret sedan misslyckas parsas/valideras (annars underskattar
    kostnadsmätningen exakt de spillanrop kostnadsoptimeringen ska
    upptäcka)."""
    from crypto_trading.agents.runner import RealClaudeRunner

    fake_message = MagicMock()
    fake_block = MagicMock(type="text", text="not valid json at all {{{")
    fake_message.content = [fake_block]
    fake_message.usage = MagicMock(
        input_tokens=800, output_tokens=200, cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )

    with (
        patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic,
        patch("crypto_trading.agents.runner.log_event") as mock_log_event,
    ):
        mock_anthropic.return_value.messages.create.return_value = fake_message
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=1
        )
        runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    usage_calls = [
        c for c in mock_log_event.call_args_list if c.kwargs.get("event") == "agent_call_usage"
    ]
    assert len(usage_calls) == 1
    assert usage_calls[0].kwargs["output_tokens"] == 200


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


def _fake_message_with_text(text: str) -> MagicMock:
    fake_message = MagicMock()
    fake_message.content = [MagicMock(type="text", text=text)]
    return fake_message


_VALID_JSON_BODY = (
    '{"run_id": "run-1", "downside": "d", "liquidity_risk": "l", '
    '"model_risk": "m", "timing_risk": "t", "suggested_stop_loss": "1", '
    '"suggested_target": "2"}'
)


def test_real_claude_runner_parses_response_wrapped_in_json_code_fence():
    """Reproducerar root cause för bear_adversarial-buggen: modellen
    svarar ibland (icke-deterministiskt, verifierat mot riktiga API-svar)
    med giltig JSON inlindad i ett ```json ... ```-kodblock trots
    instruktionen att svara med ren JSON. Innan fixen kastade json.loads()
    JSONDecodeError på HELA svaret här, tömde retry-budgeten och gav
    status="failed" trots att modellen levererade ett giltigt svar."""
    from crypto_trading.agents.runner import RealClaudeRunner

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _fake_message_with_text(
            f"```json\n{_VALID_JSON_BODY}\n```"
        )
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=1
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "ok"
    assert result.downside == "d"


def test_real_claude_runner_parses_response_wrapped_in_plain_code_fence():
    from crypto_trading.agents.runner import RealClaudeRunner

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _fake_message_with_text(
            f"```\n{_VALID_JSON_BODY}\n```"
        )
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=1
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "ok"
    assert result.downside == "d"


def test_real_claude_runner_fails_closed_on_empty_response():
    """Ett tomt svar (t.ex. content-blocket saknar text) ska - precis som
    innan fixen - tömma retry-budgeten och ge status="failed", ALDRIG
    godkännas som ok. Kodblocksstrippningen får inte försvaga detta."""
    from crypto_trading.agents.runner import RealClaudeRunner

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _fake_message_with_text("")
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=2
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "failed"


def test_real_claude_runner_fails_closed_on_invalid_json():
    """Trasig/ogiltig JSON (t.ex. modellen klipper mitt i svaret) ska
    fortfarande falla igenom till status="failed" efter uttömda försök -
    fail-closed även efter att kodblocksstrippningen lagts till."""
    from crypto_trading.agents.runner import RealClaudeRunner

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _fake_message_with_text(
            '{"run_id": "run-1", "downside": "d", '
        )
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=2
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "failed"


def test_real_claude_runner_falls_back_to_failed_status_for_forecast_without_crashing():
    """Reproducerar en live-produktionskrasch (2026-09-02, run_id
    19239634...): när ForecastAssessment (unikt fält
    scenario_probabilities: dict[str, float], plus en
    probabilities_sum_to_one-validator) tömmer sin retry-budget kraschade
    hela discovery-ticken okontrollerat istället för att ge denna roll
    status="failed" som designat. _blank_value() kände inte igen den
    parametriserade dict[str, float]-annoteringen (bara bar `dict`) och
    föll igenom till "" - och även med en korrekt tom {} hade
    model_validate() ändå kraschat på probabilities_sum_to_one (en tom
    dict summerar till 0, aldrig 1.0)."""
    from anthropic import APIError

    from crypto_trading.agents.runner import RealClaudeRunner
    from crypto_trading.schemas.assessments import ForecastAssessment

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.side_effect = APIError(
            "boom", request=MagicMock(), body=None
        )
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=2
        )
        result = runner.run(
            _agent_def(name="crypto-forecast-agent"),
            context={"run_id": "run-1"},
            output_schema=ForecastAssessment,
        )

    assert result.status == "failed"
    assert result.scenario_probabilities == {}


def test_real_claude_runner_does_not_extract_json_from_surrounding_prose():
    """Valideringen får inte försvagas till en fritextsökning efter JSON -
    om svaret inte ÄR (eventuellt kodblocksinlindad) ren JSON ska det
    fortsatt misslyckas, aldrig plockas ut ur omgivande text."""
    from crypto_trading.agents.runner import RealClaudeRunner

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _fake_message_with_text(
            f"Här är svaret:\n{_VALID_JSON_BODY}\nHoppas det hjälper!"
        )
        runner = RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=1
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "failed"
