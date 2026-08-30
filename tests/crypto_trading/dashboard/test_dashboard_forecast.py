from datetime import UTC, datetime

from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    repo = SQLiteRepository(db_path)
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app), repo


def _forecast(forecast_id="fc-1") -> ForecastRecord:
    return ForecastRecord(
        forecast_id=forecast_id,
        candidate_id=f"cand-{forecast_id}",
        instrument="BTCUSDT",
        forecast_timestamp=_NOW,
        horizon="4h",
        scenario_probabilities={"bullish": 0.6, "bearish": 0.4},
        forecast_version="v1",
        market_state_metadata={},
    )


def test_forecast_lists_scenario_probabilities_and_version(tmp_path):
    client, repo = _client(tmp_path)
    repo.save_forecast_record(_forecast())

    body = client.get("/api/forecast?limit=10").json()

    row = body["forecasts"][0]
    assert row["forecast_id"] == "fc-1"
    assert row["instrument"] == "BTCUSDT"
    assert row["scenario_probabilities"] == {"bullish": 0.6, "bearish": 0.4}
    assert row["forecast_version"] == "v1"
    assert row["horizon"] == "4h"
    assert row["actual_outcome"] is None


def test_forecast_marks_calibration_as_not_available_yet(tmp_path):
    client, repo = _client(tmp_path)

    body = client.get("/api/forecast?limit=10").json()

    assert body["calibration"] == (
        "not_available_yet — Phase 8 (Brier score / calibration curve require "
        "accumulated ForecastRecord history and a central calibration module)"
    )
