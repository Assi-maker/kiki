from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from crypto_trading.schemas.forecast import ForecastRecord


def test_forecast_record_probabilities_must_sum_to_one():
    with pytest.raises(ValidationError):
        ForecastRecord(
            forecast_id="fc-1",
            candidate_id="cand-1",
            instrument="BTCUSDT",
            forecast_timestamp=datetime.now(UTC),
            horizon="4h",
            scenario_probabilities={"bullish": 0.9, "bearish": 0.9},
            forecast_version="v1",
            market_state_metadata={},
        )


def test_forecast_record_outcome_fields_start_none():
    record = ForecastRecord(
        forecast_id="fc-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        forecast_timestamp=datetime.now(UTC),
        horizon="4h",
        scenario_probabilities={"bullish": 0.6, "neutral": 0.25, "bearish": 0.15},
        forecast_version="v1",
        market_state_metadata={"funding_rate": 0.01},
    )
    assert record.actual_outcome is None
    assert record.outcome_timestamp is None
