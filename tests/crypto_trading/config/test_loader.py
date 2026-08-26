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


_VALID_MAX_DATA_AGE_SECONDS = {
    "ticker": 30,
    "kline": 120,
    "funding_rate": 3600,
    "open_interest": 300,
    "contracts": 86400,
}
_VALID_REQUIRED_FIELDS = {
    "ticker": ["lastPrice"],
    "kline": ["open"],
    "funding_rate": ["fundingRate"],
    "open_interest": ["openInterest"],
    "contracts": ["symbol"],
}


def _valid_pipeline_kwargs(**overrides) -> dict:
    defaults = dict(
        discovery_interval_minutes=15,
        monitoring_interval_seconds=30,
        top_n=30,
        cooldown_minutes=60,
        max_data_age_seconds=_VALID_MAX_DATA_AGE_SECONDS,
        min_sample_size_for_calibration=30,
        calibration_preliminary_sample_size=10,
        sqlite_busy_timeout_ms=5000,
        required_fields=_VALID_REQUIRED_FIELDS,
        screener_timeframes=["1h"],
        bingx_base_url="https://open-api.bingx.com",
        bingx_requests_per_second=10,
        bingx_cache_ttl_seconds=5,
        bingx_max_retries=3,
        kline_consistency_tolerance_pct=Decimal("0.5"),
        eligibility_min_quote_volume_24h_usdt=Decimal("5000000"),
        eligibility_max_spread_pct=Decimal("0.002"),
        screener_lookback_periods=20,
        screener_price_volatility_threshold_pct=Decimal("2.0"),
        screener_rsi_period=14,
        screener_rsi_overbought_threshold=Decimal("70"),
        screener_volume_zscore_threshold=Decimal("2.5"),
        screener_funding_rate_threshold_pct=Decimal("0.05"),
        screener_funding_history_limit=10,
        evidence_change_threshold_for_reanalysis=Decimal("0.15"),
    )
    defaults.update(overrides)
    return defaults


def test_pipeline_config_rejects_zero_top_n():
    with pytest.raises(ValidationError):
        PipelineConfig(**_valid_pipeline_kwargs(top_n=0))


def test_get_settings_loads_phase1_fields():
    settings = get_settings()
    assert settings.pipeline.screener_timeframes == ["1h", "4h"]
    assert settings.pipeline.bingx_base_url == "https://open-api.bingx.com"
    assert settings.pipeline.bingx_requests_per_second > 0
    assert set(settings.pipeline.required_fields.keys()) >= {
        "ticker",
        "kline",
        "funding_rate",
        "open_interest",
        "contracts",
    }
    assert set(settings.pipeline.max_data_age_seconds.keys()) >= {
        "ticker",
        "kline",
        "funding_rate",
        "open_interest",
        "contracts",
    }


def test_pipeline_config_rejects_missing_max_data_age_seconds_key():
    incomplete = {"ticker": 30, "kline": 120, "funding_rate": 3600}
    with pytest.raises(ValidationError):
        PipelineConfig(**_valid_pipeline_kwargs(max_data_age_seconds=incomplete))


def test_pipeline_config_rejects_missing_required_fields_key():
    incomplete = {"ticker": ["lastPrice"]}
    with pytest.raises(ValidationError):
        PipelineConfig(**_valid_pipeline_kwargs(required_fields=incomplete))


def _valid_risk_limits_kwargs(**overrides) -> dict:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5,
        max_total_exposure_pct=Decimal("0.25"),
        spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"),
        fee_pct=Decimal("0.0004"),
        max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return defaults


def test_risk_limits_config_rejects_risk_pct_over_one():
    with pytest.raises(ValidationError):
        RiskLimitsConfig(**_valid_risk_limits_kwargs(risk_per_trade_pct=Decimal("1.5")))


def test_get_settings_loads_phase4_fields():
    settings = get_settings()
    assert settings.risk_limits.max_position_hold_hours > 0


def test_risk_limits_config_rejects_zero_max_position_hold_hours():
    with pytest.raises(ValidationError):
        RiskLimitsConfig(**_valid_risk_limits_kwargs(max_position_hold_hours=0))


def test_budget_limits_config_rejects_zero_calls():
    with pytest.raises(ValidationError):
        BudgetLimitsConfig(
            max_candidates_per_discovery_run=10,
            max_ai_calls_per_discovery_run=0,
            max_ai_calls_per_day=500,
            warning_threshold_pct=Decimal("0.8"),
        )


def test_get_settings_loads_phase2_fields():
    settings = get_settings()
    assert settings.pipeline.eligibility_min_quote_volume_24h_usdt > 0
    assert 0 < settings.pipeline.eligibility_max_spread_pct <= 1
    assert settings.pipeline.screener_lookback_periods > 1
    assert settings.pipeline.screener_price_volatility_threshold_pct > 0
    assert settings.pipeline.screener_rsi_period > 1
    assert 0 < settings.pipeline.screener_rsi_overbought_threshold <= 100
    assert settings.pipeline.screener_volume_zscore_threshold > 0
    assert settings.pipeline.screener_funding_rate_threshold_pct > 0
    assert settings.pipeline.screener_funding_history_limit > 1
    assert settings.pipeline.evidence_change_threshold_for_reanalysis >= 0


def test_pipeline_config_rejects_negative_eligibility_min_volume():
    with pytest.raises(ValidationError):
        PipelineConfig(
            **_valid_pipeline_kwargs(eligibility_min_quote_volume_24h_usdt=Decimal("-1"))
        )


def test_pipeline_config_rejects_spread_pct_above_one():
    with pytest.raises(ValidationError):
        PipelineConfig(**_valid_pipeline_kwargs(eligibility_max_spread_pct=Decimal("1.5")))


def test_missing_config_file_raises_config_error(tmp_path, monkeypatch):
    import crypto_trading.config.loader as loader_module

    monkeypatch.setattr(loader_module, "_CONFIG_DIR", tmp_path)
    with pytest.raises(ConfigError):
        get_settings()
