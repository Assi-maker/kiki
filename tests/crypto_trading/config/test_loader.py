from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    PipelineConfig,
    RiskLimitsConfig,
    get_settings,
)


def test_get_settings_loads_real_yaml_files_successfully():
    settings = get_settings()
    assert settings.pipeline.top_n > 0
    assert settings.pipeline.discovery_interval_minutes > 0
    assert settings.risk_limits.starting_capital_usdt > 0
    assert isinstance(settings.risk_limits.starting_capital_usdt, Decimal)
    assert settings.budget_limits.max_ai_calls_per_day > 0


def test_pipeline_config_rejects_zero_top_n():
    with pytest.raises(ValidationError):
        PipelineConfig(
            discovery_interval_minutes=15,
            monitoring_interval_seconds=30,
            top_n=0,
            cooldown_minutes=60,
            max_data_age_seconds={"ticker": 30},
            min_sample_size_for_calibration=30,
            calibration_preliminary_sample_size=10,
            sqlite_busy_timeout_ms=5000,
        )


def test_risk_limits_config_rejects_risk_pct_over_one():
    with pytest.raises(ValidationError):
        RiskLimitsConfig(
            starting_capital_usdt=Decimal("10000"),
            risk_per_trade_pct=Decimal("1.5"),
            max_concurrent_positions=5,
            max_total_exposure_pct=Decimal("0.25"),
            spread_pct=Decimal("0.0005"),
            slippage_pct=Decimal("0.0005"),
            fee_pct=Decimal("0.0004"),
        )


def test_budget_limits_config_rejects_zero_calls():
    with pytest.raises(ValidationError):
        BudgetLimitsConfig(
            max_candidates_per_discovery_run=10,
            max_ai_calls_per_discovery_run=0,
            max_ai_calls_per_day=500,
            warning_threshold_pct=Decimal("0.8"),
        )


def test_missing_config_file_raises_config_error(tmp_path, monkeypatch):
    import crypto_trading.config.loader as loader_module

    monkeypatch.setattr(loader_module, "_CONFIG_DIR", tmp_path)
    with pytest.raises(ConfigError):
        get_settings()
