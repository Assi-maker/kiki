from crypto_trading.config.loader import get_settings, is_live_execution_enabled


def test_settings_load_live_execution_defaults():
    settings = get_settings()
    live = settings.live_execution
    assert live.check_interval_seconds > 0
    assert live.claim_stale_after_seconds > 0
    assert live.max_retries > 0
    assert live.max_concurrent_positions == 4
    assert live.margin_per_trade_usdt == 10
    assert live.leverage == 10
    assert live.max_position_hold_hours == 6
    assert live.margin_safety_buffer_usdt == 1


def test_is_live_execution_enabled_reads_env_flag(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADING_LIVE_EXECUTION_ENABLED", raising=False)
    assert is_live_execution_enabled() is False
    monkeypatch.setenv("CRYPTO_TRADING_LIVE_EXECUTION_ENABLED", "1")
    assert is_live_execution_enabled() is True
