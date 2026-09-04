import os

from crypto_trading.config.loader import get_settings, is_demo_execution_enabled


def test_settings_load_demo_execution_defaults():
    settings = get_settings()
    assert settings.demo_execution.check_interval_seconds > 0
    assert settings.demo_execution.claim_stale_after_seconds > 0
    assert settings.demo_execution.max_retries > 0


def test_is_demo_execution_enabled_reads_env_flag(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADING_DEMO_EXECUTION_ENABLED", raising=False)
    assert is_demo_execution_enabled() is False
    monkeypatch.setenv("CRYPTO_TRADING_DEMO_EXECUTION_ENABLED", "1")
    assert is_demo_execution_enabled() is True
