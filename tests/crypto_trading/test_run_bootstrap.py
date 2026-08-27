import pytest

from crypto_trading.agents.runner import RealClaudeRunner
from crypto_trading.config.exceptions import ConfigError
from crypto_trading.run import build_runner_from_env


def test_build_runner_from_env_raises_config_error_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_runner_from_env()


def test_build_runner_from_env_returns_real_claude_runner_when_api_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    runner = build_runner_from_env()
    assert isinstance(runner, RealClaudeRunner)
