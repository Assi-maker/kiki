from crypto_trading.config.loader import get_settings, is_guardian_enabled


def test_settings_load_guardian_defaults():
    settings = get_settings()
    assert settings.guardian.check_interval_seconds > 0
    assert settings.guardian.watch_decay_threshold < settings.guardian.protect_decay_threshold
    assert settings.guardian.protect_decay_threshold < settings.guardian.exit_decay_threshold
    assert len(settings.guardian.factor_weights) == 6


def test_is_guardian_enabled_reads_env_flag(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADING_GUARDIAN_ENABLED", raising=False)
    assert is_guardian_enabled() is False
    monkeypatch.setenv("CRYPTO_TRADING_GUARDIAN_ENABLED", "1")
    assert is_guardian_enabled() is True
