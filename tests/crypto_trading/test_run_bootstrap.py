import pytest
from fastapi import FastAPI

from crypto_trading.agents.runner import RealClaudeRunner
from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import get_settings
from crypto_trading.notify.telegram import TelegramNotifier
from crypto_trading.run import (
    build_dashboard_app_from_env,
    build_detective_runner_from_env,
    build_notifier_from_env,
    build_runner_from_env,
    build_screener_runner_from_env,
)


def test_build_runner_from_env_raises_config_error_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_runner_from_env()


def test_build_runner_from_env_returns_real_claude_runner_when_api_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    runner = build_runner_from_env()
    assert isinstance(runner, RealClaudeRunner)


def test_build_screener_runner_from_env_raises_config_error_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_screener_runner_from_env()


def test_build_screener_runner_from_env_defaults_to_haiku(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    monkeypatch.delenv("CRYPTO_TRADING_SCREENER_MODEL", raising=False)
    runner = build_screener_runner_from_env()
    assert isinstance(runner, RealClaudeRunner)
    assert runner._model == "claude-haiku-4-5"


def test_build_screener_runner_from_env_honors_model_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    monkeypatch.setenv("CRYPTO_TRADING_SCREENER_MODEL", "claude-sonnet-5")
    runner = build_screener_runner_from_env()
    assert runner._model == "claude-sonnet-5"


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


def test_build_dashboard_app_from_env_returns_none_without_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("CRYPTO_TRADING_DASHBOARD_ENABLED", raising=False)
    settings = get_settings()
    assert build_dashboard_app_from_env(lambda: None, settings) is None


def test_build_dashboard_app_from_env_returns_app_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CRYPTO_TRADING_DASHBOARD_ENABLED", "1")
    settings = get_settings()
    app = build_dashboard_app_from_env(lambda: None, settings)
    assert isinstance(app, FastAPI)


def test_build_detective_runner_from_env_raises_config_error_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_detective_runner_from_env()


def test_build_detective_runner_from_env_defaults_to_haiku(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    monkeypatch.delenv("CRYPTO_TRADING_DETECTIVE_MODEL", raising=False)
    runner = build_detective_runner_from_env()
    assert isinstance(runner, RealClaudeRunner)
    assert runner._model == "claude-haiku-4-5"


def test_build_detective_runner_from_env_honors_model_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    monkeypatch.setenv("CRYPTO_TRADING_DETECTIVE_MODEL", "claude-sonnet-5")
    runner = build_detective_runner_from_env()
    assert runner._model == "claude-sonnet-5"


def test_build_detective_runner_from_env_returns_a_distinct_instance_from_screener_runner(
    monkeypatch,
):
    """last_call_billed/last_call_cost_usd är mutabel instansstate
    (agents/runner.py) - Detective kör i sin egen tråd (run.py::
    _run_detective_forever) och FÅR ALDRIG dela runner-instans med
    screener_runner/discovery-runnern, annars racear de skrivningarna."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    detective_runner = build_detective_runner_from_env()
    screener_runner = build_screener_runner_from_env()
    assert detective_runner is not screener_runner
