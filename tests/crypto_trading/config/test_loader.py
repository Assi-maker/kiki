from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    DashboardConfig,
    DetectiveConfig,
    NotifyConfig,
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
    assert settings.pipeline.screener_timeframes == ["30m", "1h"]
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
        max_position_notional_usdt=Decimal("1000"),
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


def test_get_settings_loads_phase5_5_news_urls():
    settings = get_settings()
    assert settings.pipeline.news_rss_base_url == "https://www.coindesk.com/arc/outboundfeeds/rss/"
    assert settings.pipeline.fear_greed_base_url == "https://api.alternative.me/fng/"


def test_pipeline_config_allows_overriding_news_urls():
    config = PipelineConfig(
        **_valid_pipeline_kwargs(
            news_rss_base_url="https://example.com/rss",
            fear_greed_base_url="https://example.com/fng",
        )
    )
    assert config.news_rss_base_url == "https://example.com/rss"
    assert config.fear_greed_base_url == "https://example.com/fng"


def test_get_settings_loads_phase6_notify_fields():
    settings = get_settings()
    assert settings.notify.notification_level in ("important", "decisions", "debug")
    assert settings.notify.notify_interval_seconds > 0


def test_notify_config_rejects_invalid_notification_level():
    with pytest.raises(ValidationError):
        NotifyConfig(notification_level="bogus", notify_interval_seconds=60)


def test_notify_config_rejects_zero_notify_interval_seconds():
    with pytest.raises(ValidationError):
        NotifyConfig(notification_level="important", notify_interval_seconds=0)


def test_get_settings_loads_dashboard_config_with_localhost_default():
    settings = get_settings()
    assert settings.dashboard.host == "127.0.0.1"
    assert settings.dashboard.port > 0


def test_dashboard_config_rejects_invalid_port():
    with pytest.raises(ValidationError):
        DashboardConfig(host="127.0.0.1", port=0)


def test_dashboard_config_rejects_port_above_65535():
    with pytest.raises(ValidationError):
        DashboardConfig(host="127.0.0.1", port=70000)


def test_missing_config_file_raises_config_error(tmp_path, monkeypatch):
    import crypto_trading.config.loader as loader_module

    monkeypatch.setattr(loader_module, "_CONFIG_DIR", tmp_path)
    with pytest.raises(ConfigError):
        get_settings()


def test_get_settings_loads_paper_capacity_defaults():
    """2026-09-04: PAPER-kapacitetsökning, explicit användarkrav - se
    config/risk_limits.yaml:s kommentar för den fulla motiveringen."""
    settings = get_settings()
    assert settings.risk_limits.max_concurrent_positions == 20
    assert settings.risk_limits.max_total_exposure_pct == Decimal("1.00")
    # Oförändrat - målet är mer kapacitet, inte mer risk per trade.
    assert settings.risk_limits.risk_per_trade_pct == Decimal("0.01")
    assert settings.risk_limits.starting_capital_usdt == Decimal("10000")


def test_get_settings_loads_guardian_assisted_exit_activated():
    """Guardian-assisted exit (2026-09-05, explicit användarkrav): defaultade
    till False i config/loader.py tills tillräckligt med shadow-mode-data
    verifierat exitlogiken (837/837 tester gröna) - därefter, i ett separat,
    senare, explicit beslut (2026-09-05, commit e69c701), aktiverad i den
    verkliga körande configen (config/guardian.yaml::assisted_exit_enabled).
    Denna assertion uppdaterad 2026-09-06 för att matcha det avsiktliga,
    redan godkända aktiveringsbeslutet - se docs-historik i guardian.yaml."""
    settings = get_settings()
    assert settings.guardian.assisted_exit_enabled is True


def test_get_settings_loads_guardian_authority_enabled_default_false():
    """The bare Pydantic default (GuardianConfig.authority_enabled) must stay
    False - any environment/test without this deployment's own guardian.yaml
    override ships inert, same "ships inert" discipline as every other
    Guardian flag in this codebase. This is a code-level guarantee, not a
    statement about this deployment's actual running config - see
    test_get_settings_loads_guardian_authority_enabled_is_activated below
    for that."""
    from crypto_trading.config.loader import GuardianConfig

    assert GuardianConfig().authority_enabled is False
    assert isinstance(get_settings().guardian.authority_veto_threshold, float)


def test_get_settings_loads_guardian_authority_enabled_is_activated():
    """ACTIVATED 2026-09-17 (docs/superpowers/plans/
    2026-09-15-guardian-authority-live-autonomy.md, Task 9) after the final
    whole-branch review's findings were fixed and independently re-verified
    clean - see guardian.yaml's own activation comment for the full
    grep/AST-provable guarantees restated at that commit. This is the one
    test in this file that reads the real, running guardian.yaml (not the
    bare Pydantic default checked above) and must go GREEN only because that
    specific file's authority_enabled line was deliberately flipped, not
    because the code-level default changed."""
    settings = get_settings()
    assert settings.guardian.authority_enabled is True


def test_get_settings_loads_guardian_authority_tighten_close_thresholds_defaults():
    """Guardian Authority tick-time decisions (2026-09-14, Task 7): these two
    fields are pulled forward from Task 10 for the identical reason Task 6
    already pulled forward authority_enabled/authority_veto_threshold -
    guardian/tick.py::process_one_position has a hard runtime dependency on
    them existing now. Both must be floats, and - matching
    decide_open_position's own documented precedence ("in normal
    configuration close_threshold >= tighten_threshold", CLOSE_EARLY
    evaluated first as "the more severe action") - close must be strictly
    greater than tighten."""
    settings = get_settings()
    guardian_cfg = settings.guardian
    assert isinstance(guardian_cfg.authority_tighten_threshold, float)
    assert isinstance(guardian_cfg.authority_close_threshold, float)
    assert guardian_cfg.authority_close_threshold > guardian_cfg.authority_tighten_threshold


def test_get_settings_loads_guardian_authority_yaml_thresholds_byte_identical():
    """Task 10 originally documented all four authority_* fields in
    guardian.yaml with values DELIBERATELY identical to the Pydantic
    defaults (a documentation-only change). The three threshold fields are
    still exactly that - untouched by the 2026-09-17 activation, which only
    ever changed authority_enabled itself (see
    test_get_settings_loads_guardian_authority_enabled_is_activated and
    guardian.yaml's own activation comment). This test now pins only the
    thresholds' continued byte-identity to the bare Pydantic defaults;
    authority_enabled is deliberately NOT compared here, since the whole
    point of activation is that the YAML value and the bare code default no
    longer agree."""
    settings = get_settings()
    guardian_cfg = settings.guardian
    assert guardian_cfg.authority_veto_threshold == 0.3
    assert guardian_cfg.authority_tighten_threshold == 0.15
    assert guardian_cfg.authority_close_threshold == 0.45
    # Cross-check directly against the Pydantic model's own bare defaults
    # (no YAML involved at all) - the thresholds must still be identical,
    # proving the YAML's threshold entries genuinely changed nothing.
    from crypto_trading.config.loader import GuardianConfig

    bare_defaults = GuardianConfig()
    assert guardian_cfg.authority_veto_threshold == bare_defaults.authority_veto_threshold
    assert guardian_cfg.authority_tighten_threshold == bare_defaults.authority_tighten_threshold
    assert guardian_cfg.authority_close_threshold == bare_defaults.authority_close_threshold
    # The one field that now deliberately diverges from the bare default:
    assert guardian_cfg.authority_enabled is True
    assert bare_defaults.authority_enabled is False


def test_get_settings_loads_guardian_authority_shadow_enabled_default_false():
    """Guardian Authority Shadow/Observation Mode (2026-09-15,
    docs/superpowers/plans/2026-09-15-guardian-authority-shadow.md Task 3):
    add the authority_shadow_enabled flag to gate only the new shadow-
    observation code paths from this plan, separate and independent from
    authority_enabled. Must default to False - landing this config flag
    changes zero runtime behavior until a later, separate, explicit
    activation decision."""
    settings = get_settings()
    assert settings.guardian.authority_shadow_enabled is False


def test_get_settings_loads_guardian_authority_shadow_yaml_documentation_byte_identical():
    """Task 3: config/guardian.yaml now carries an explicit entry for
    authority_shadow_enabled. The value written to YAML is DELIBERATELY the
    exact same value already in effect as Pydantic default (GuardianConfig's
    own field declaration in config/loader.py) - this is a documentation-only
    change, and this test is the explicit byte-identical-behavior proof:
    loading the real, running guardian.yaml must produce EXACTLY the same
    GuardianConfig.authority_shadow_enabled value as the bare Pydantic
    default, not just "close enough" or "the right type"."""
    settings = get_settings()
    guardian_cfg = settings.guardian
    assert guardian_cfg.authority_shadow_enabled is False
    # Cross-check directly against the Pydantic model's own bare defaults
    # (no YAML involved at all) - the two must be identical, proving the
    # new YAML entry genuinely changed nothing about the resulting config.
    from crypto_trading.config.loader import GuardianConfig

    bare_defaults = GuardianConfig()
    assert guardian_cfg.authority_shadow_enabled == bare_defaults.authority_shadow_enabled


def test_get_settings_loads_detective_config_from_real_yaml():
    settings = get_settings()
    assert settings.detective.batch_size == 10
    assert settings.detective.check_interval_seconds == 300
    assert settings.detective.min_history_for_win_loss_comparison == 20


def test_detective_config_rejects_zero_batch_size():
    with pytest.raises(ValidationError):
        DetectiveConfig(
            batch_size=0, check_interval_seconds=300, min_history_for_win_loss_comparison=20
        )


def test_detective_config_defaults_when_omitted():
    """Settings.detective har ett default (Field(default_factory=...)) -
    de ~25 befintliga testfilerna som konstruerar Settings(...) utan
    detective= (t.ex. tests/crypto_trading/test_market_snapshot.py::
    _settings()) får fortfarande ett giltigt, validerat värde."""
    config = DetectiveConfig()
    assert config.batch_size == 10
    assert config.check_interval_seconds == 300
    assert config.min_history_for_win_loss_comparison == 20


def test_get_settings_loads_live_profit_protection_defaults():
    settings = get_settings()
    assert settings.live_execution.profit_protection_enabled is False
    assert settings.live_execution.profit_protection_threshold_pct == Decimal("0.01")


def test_get_settings_loads_godfather_priority_boost_enabled_default_false():
    """GODFATHER priority-boost (2026-09-18 expansion): the bare Pydantic
    default (GodfatherConfig.priority_boost_enabled) must stay False -
    ships inert until a later, separate, explicit activation decision, same
    discipline as every other flag in this file."""
    from crypto_trading.config.loader import GodfatherConfig

    assert GodfatherConfig().priority_boost_enabled is False


def test_get_settings_loads_godfather_config_from_real_yaml():
    settings = get_settings()
    assert settings.godfather.priority_boost_enabled is False
