import pytest

from crypto_trading.agents.runner import RealClaudeRunner
from crypto_trading.config.exceptions import ConfigError
from crypto_trading.notify.telegram import TelegramNotifier
from crypto_trading.run import build_notifier_from_env, build_runner_from_env


def test_build_runner_from_env_raises_config_error_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_runner_from_env()


def test_build_runner_from_env_returns_real_claude_runner_when_api_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    runner = build_runner_from_env()
    assert isinstance(runner, RealClaudeRunner)


def test_build_notifier_from_env_returns_none_when_bot_token_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    assert build_notifier_from_env() is None


def test_build_notifier_from_env_returns_none_when_chat_id_missing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert build_notifier_from_env() is None


def test_build_notifier_from_env_returns_notifier_when_both_present(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    notifier = build_notifier_from_env()
    assert isinstance(notifier, TelegramNotifier)
