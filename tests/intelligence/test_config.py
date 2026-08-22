from intelligence.config import get_settings


def test_defaults_without_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    settings = get_settings()
    assert settings.anthropic_api_key is None
    assert settings.alphavantage_api_key is None
    assert settings.max_events_per_run == 20
    assert settings.max_opportunities_per_run == 5
    assert settings.max_agent_calls_per_run == 50
    assert settings.agent_timeout_seconds == 30.0
    assert settings.connector_max_retries == 3


def test_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("MAX_EVENTS_PER_RUN", "5")
    settings = get_settings()
    assert settings.anthropic_api_key == "sk-test-123"
    assert settings.max_events_per_run == 5


def test_scoring_weights_file_exists():
    settings = get_settings()
    assert settings.scoring_weights_path.exists()


def test_db_path_override_from_env(monkeypatch, tmp_path):
    override_path = tmp_path / "custom.db"
    monkeypatch.setenv("DB_PATH_OVERRIDE", str(override_path))
    settings = get_settings()
    assert settings.db_path == override_path
