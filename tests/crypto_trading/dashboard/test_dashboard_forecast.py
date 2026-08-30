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


def test_dashboard_forecast_calibration_is_insufficient_data_by_default(tmp_path):
    """actual_outcome fylls aldrig i av någon produktionskod (Fas 8.5,
    PLAN_CRYPTO_PHASE8.md §0) - detta är därför det GARANTERADE default-
    läget, även med forecasts i databasen (deras actual_outcome är alltid
    None tills en framtida, separat mekanism finns)."""
    client, repo = _client(tmp_path)
    repo.save_forecast_record(_forecast())

    body = client.get("/api/forecast?limit=10").json()

    calibration = body["calibration"]
    assert calibration["brier_score"]["value"] is None
    assert calibration["brier_score"]["sample_size"] == 0
    assert calibration["brier_score"]["calibration_status"] == "insufficient_data"
    assert len(calibration["calibration_curve"]) == 10
    assert all(b["sample_size"] == 0 for b in calibration["calibration_curve"])
    assert calibration["breakdown_by_horizon"] == {}
    assert calibration["breakdown_by_scenario"] == {}


def test_dashboard_forecast_calibration_reflects_a_manually_seeded_outcome(tmp_path):
    """actual_outcome seedas direkt via rå SQL - INTE via någon
    produktionsmekanism, som inte finns (§0) - och bevisar att
    kalibreringen korrekt speglar den genom hela HTTP-vägen."""
    client, repo = _client(tmp_path)
    repo.save_forecast_record(_forecast())
    repo._conn.execute("UPDATE forecasts SET actual_outcome = 'bullish' WHERE forecast_id = 'fc-1'")
    repo._conn.commit()

    body = client.get("/api/forecast?limit=10").json()

    calibration = body["calibration"]
    # {"bullish": 0.6, "bearish": 0.4}, actual="bullish" -> BS = 0.16+0.16=0.32
    assert calibration["brier_score"]["sample_size"] == 1
    assert calibration["brier_score"]["value"] is not None
    assert abs(float(calibration["brier_score"]["value"]) - 0.32) < 1e-9
