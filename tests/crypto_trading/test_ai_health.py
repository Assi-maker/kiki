"""AI health (2026-09-28): on 2026-09-27 04:13-06:37 UTC the Anthropic credit
ran out, 399 calls failed with HTTP 400 and nothing told anyone. Every
Anthropic failure is now classified, logged as `ai_api_error`, and folded
into one process-wide health state the watchdog alerts on."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import anthropic
import httpx

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.ai_health import AIHealth, classify_api_error
from crypto_trading.schemas.assessments import RiskAssessment

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status, message):
    response = httpx.Response(status, request=_REQUEST)
    return cls(message, response=response, body={"type": "error", "error": {"message": message}})


_CREDIT = (
    anthropic.BadRequestError,
    400,
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Your credit balance is too low to access the Anthropic API. Please go "
    "to Plans & Billing to upgrade or purchase credits.'}}",
)


def test_credit_exhaustion_is_classified_as_credit_exhausted():
    assert classify_api_error(_status_error(*_CREDIT)) == "CREDIT_EXHAUSTED"


def test_other_http_errors_are_classified_by_status():
    assert (
        classify_api_error(_status_error(anthropic.BadRequestError, 400, "prompt too long"))
        == "BAD_REQUEST"
    )
    assert (
        classify_api_error(_status_error(anthropic.AuthenticationError, 401, "bad key")) == "AUTH"
    )
    assert classify_api_error(_status_error(anthropic.PermissionDeniedError, 403, "no")) == "AUTH"
    assert (
        classify_api_error(_status_error(anthropic.RateLimitError, 429, "slow down"))
        == "RATE_LIMITED"
    )
    assert (
        classify_api_error(_status_error(anthropic.InternalServerError, 529, "overloaded"))
        == "OVERLOADED"
    )
    assert (
        classify_api_error(_status_error(anthropic.InternalServerError, 500, "oops"))
        == "OVERLOADED"
    )


def test_timeouts_and_connection_errors_are_classified():
    assert classify_api_error(anthropic.APITimeoutError(request=_REQUEST)) == "TIMEOUT"
    assert classify_api_error(anthropic.APIConnectionError(request=_REQUEST)) == "CONNECTION"


def test_health_is_ok_until_something_fails():
    health = AIHealth()
    assert health.snapshot(_NOW)["status"] == "OK"
    health.record_success(_NOW)
    assert health.snapshot(_NOW)["status"] == "OK"


def test_one_credit_exhaustion_makes_the_ai_down_immediately():
    health = AIHealth()
    health.record_success(_NOW - timedelta(minutes=5))
    health.record_failure("CREDIT_EXHAUSTED", "crypto-risk-agent", _NOW)
    snapshot = health.snapshot(_NOW)
    assert snapshot["status"] == "DOWN"
    assert snapshot["last_error_kind"] == "CREDIT_EXHAUSTED"
    assert snapshot["failing_since"] == _NOW.isoformat()


def test_transient_failures_degrade_only_after_several_in_a_row():
    health = AIHealth(degraded_after=3)
    health.record_failure("TIMEOUT", "a", _NOW)
    health.record_failure("TIMEOUT", "a", _NOW)
    assert health.snapshot(_NOW)["status"] == "OK"
    health.record_failure("TIMEOUT", "a", _NOW)
    assert health.snapshot(_NOW)["status"] == "DEGRADED"


def test_a_success_recovers_the_health():
    health = AIHealth()
    health.record_failure("CREDIT_EXHAUSTED", "a", _NOW)
    health.record_success(_NOW + timedelta(minutes=1))
    snapshot = health.snapshot(_NOW + timedelta(minutes=1))
    assert snapshot["status"] == "OK"
    assert snapshot["consecutive_failures"] == 0


def _agent_def():
    return AgentDefinition(
        name="crypto-risk-agent", description="d", tools=["Read"], system_prompt="p"
    )


def test_runner_stops_retrying_on_credit_exhaustion_and_fails_closed(caplog):
    """No retry can succeed while the account has no credit - the call fails
    closed at once (status=failed -> the Gate's missing_or_failed_assessment
    NO_TRADE), the error is logged with its class, and health goes DOWN."""
    from crypto_trading.agents import runner as runner_module

    health = AIHealth()
    with (
        patch.object(runner_module, "AI_HEALTH", health),
        patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic,
        caplog.at_level("INFO", logger="crypto_trading"),
    ):
        mock_anthropic.return_value.messages.create.side_effect = _status_error(*_CREDIT)
        runner = runner_module.RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=3
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "failed"
    assert mock_anthropic.return_value.messages.create.call_count == 1
    errors = [r.getMessage() for r in caplog.records if '"ai_api_error"' in r.getMessage()]
    assert len(errors) == 1
    assert '"kind": "CREDIT_EXHAUSTED"' in errors[0]
    assert '"status_code": 400' in errors[0]
    assert health.snapshot(datetime.now(UTC))["status"] == "DOWN"


def test_runner_still_retries_transient_errors():
    from crypto_trading.agents import runner as runner_module

    health = AIHealth()
    with (
        patch.object(runner_module, "AI_HEALTH", health),
        patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic,
    ):
        mock_anthropic.return_value.messages.create.side_effect = anthropic.APITimeoutError(
            request=_REQUEST
        )
        runner = runner_module.RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=3
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "failed"
    assert mock_anthropic.return_value.messages.create.call_count == 3
    assert health.snapshot(datetime.now(UTC))["consecutive_failures"] == 3


def test_runner_success_records_healthy_ai():
    from crypto_trading.agents import runner as runner_module

    health = AIHealth()
    health.record_failure("CREDIT_EXHAUSTED", "x", datetime.now(UTC))
    fake_message = MagicMock()
    fake_message.content = [
        MagicMock(
            type="text",
            text=(
                '{"suggested_stop_loss": "1", "suggested_target": "2", "downside": "d", '
                '"liquidity_risk": "l", "model_risk": "m", "timing_risk": "t", "run_id": "run-1"}'
            ),
        )
    ]
    fake_message.usage = MagicMock(
        input_tokens=10, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    with (
        patch.object(runner_module, "AI_HEALTH", health),
        patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic,
    ):
        mock_anthropic.return_value.messages.create.return_value = fake_message
        runner = runner_module.RealClaudeRunner(
            api_key="fake", model="claude-sonnet-5", timeout_seconds=30, max_retries=1
        )
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "ok"
    assert health.snapshot(datetime.now(UTC))["status"] == "OK"
