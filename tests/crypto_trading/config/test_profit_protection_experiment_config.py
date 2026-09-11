from crypto_trading.config.loader import ProfitProtectionExperimentConfig, get_settings


def test_profit_protection_experiment_config_has_only_an_enabled_field():
    config = ProfitProtectionExperimentConfig()
    assert config.enabled is False
    assert set(ProfitProtectionExperimentConfig.model_fields.keys()) == {"enabled"}


def test_settings_load_profit_protection_experiment_defaults():
    settings = get_settings()
    assert settings.profit_protection_experiment.enabled is False
