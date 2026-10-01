from crypto_trading.config.loader import ProfitProtectionExperimentConfig, get_settings


def test_profit_protection_experiment_config_has_only_an_enabled_field():
    config = ProfitProtectionExperimentConfig()
    assert config.enabled is False
    assert set(ProfitProtectionExperimentConfig.model_fields.keys()) == {"enabled"}


def test_settings_load_profit_protection_experiment_defaults():
    """Code default is OFF (test above); the running yaml carries the
    explicit activation and is exactly what get_settings() loads. Stale until
    2026-10-01: asserted the running config was still OFF."""
    from pathlib import Path

    import yaml

    raw = yaml.safe_load(
        (Path(__file__).parents[3] / "crypto_trading/config/profit_protection_experiment.yaml")
        .read_text(encoding="utf-8")
    )
    assert get_settings().profit_protection_experiment.enabled is raw["enabled"]
