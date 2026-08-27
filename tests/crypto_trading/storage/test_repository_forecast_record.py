from datetime import UTC, datetime

from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _forecast_record(
    candidate_id: str = "cand-1", scenario_probabilities: dict | None = None
) -> ForecastRecord:
    return ForecastRecord(
        forecast_id=candidate_id,
        candidate_id=candidate_id,
        instrument="BTCUSDT",
        forecast_timestamp=_NOW,
        horizon="4h",
        scenario_probabilities=scenario_probabilities
        or {"bullish": 0.6, "neutral": 0.3, "bearish": 0.1},
        forecast_version="v1",
        market_state_metadata={"candidate_score": 0.8},
    )


def test_save_forecast_record_persists_a_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_forecast_record(_forecast_record())

    record = repo.get_forecast_record("cand-1")

    assert record is not None
    assert record.candidate_id == "cand-1"
    assert record.instrument == "BTCUSDT"
    assert record.horizon == "4h"
    assert record.forecast_version == "v1"
    assert record.scenario_probabilities == {"bullish": 0.6, "neutral": 0.3, "bearish": 0.1}
    assert record.market_state_metadata == {"candidate_score": 0.8}
    assert record.forecast_timestamp == _NOW
    assert record.actual_outcome is None  # oförändrat i denna fas
    assert record.outcome_timestamp is None  # oförändrat i denna fas


def test_save_forecast_record_upserts_on_resumed_candidate(tmp_path):
    """AC6: en återupptagen candidates ANDRA forecast-bedömning skriver
    över, dubblerar aldrig (samma princip som save_assessment)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_forecast_record(
        _forecast_record(scenario_probabilities={"bullish": 0.6, "neutral": 0.3, "bearish": 0.1})
    )
    repo.save_forecast_record(
        _forecast_record(scenario_probabilities={"bullish": 0.2, "neutral": 0.3, "bearish": 0.5})
    )

    record = repo.get_forecast_record("cand-1")
    assert record.scenario_probabilities == {"bullish": 0.2, "neutral": 0.3, "bearish": 0.5}

    count = repo._conn.execute("SELECT COUNT(*) AS n FROM forecasts").fetchone()["n"]
    assert count == 1


def test_get_forecast_record_returns_none_when_missing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.get_forecast_record("nonexistent") is None


def test_forecast_record_survives_a_fresh_repository_instance(tmp_path):
    db_path = tmp_path / "t.db"
    repo1 = SQLiteRepository(db_path)
    repo1.save_forecast_record(_forecast_record())

    repo2 = SQLiteRepository(db_path)
    record = repo2.get_forecast_record("cand-1")

    assert record is not None
    assert record.instrument == "BTCUSDT"
    assert record.scenario_probabilities == {"bullish": 0.6, "neutral": 0.3, "bearish": 0.1}
