from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.calibration.calibration_curve import (
    compute_calibration_breakdown_by_horizon,
    compute_calibration_breakdown_by_scenario,
    compute_calibration_curve,
    compute_calibration_status,
)
from crypto_trading.schemas.forecast import ForecastRecord

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _forecast(
    forecast_id: str,
    scenario_probabilities: dict[str, float],
    actual_outcome: str | None,
    horizon: str = "4h",
) -> ForecastRecord:
    return ForecastRecord(
        forecast_id=forecast_id,
        candidate_id=f"cand-{forecast_id}",
        instrument="BTCUSDT",
        forecast_timestamp=_NOW,
        horizon=horizon,
        scenario_probabilities=scenario_probabilities,
        forecast_version="v1",
        market_state_metadata={},
        actual_outcome=actual_outcome,
    )


# --- compute_calibration_status ---------------------------------------------


def test_calibration_status_boundaries():
    # preliminary_sample_size=10, min_sample_size=30
    assert compute_calibration_status(9, min_sample_size=30, preliminary_sample_size=10) == (
        "insufficient_data"
    )
    assert compute_calibration_status(10, min_sample_size=30, preliminary_sample_size=10) == (
        "preliminary"
    )
    assert compute_calibration_status(29, min_sample_size=30, preliminary_sample_size=10) == (
        "preliminary"
    )
    assert compute_calibration_status(30, min_sample_size=30, preliminary_sample_size=10) == (
        "calibrated"
    )


def test_calibration_status_zero_is_insufficient_data():
    assert compute_calibration_status(0, min_sample_size=30, preliminary_sample_size=10) == (
        "insufficient_data"
    )


# --- compute_calibration_curve ----------------------------------------------


def test_calibration_curve_empty_for_no_forecasts():
    buckets = compute_calibration_curve([], min_sample_size=30, preliminary_sample_size=10)
    assert len(buckets) == 10
    assert all(b["sample_size"] == 0 for b in buckets)
    assert all(b["mean_predicted"] is None for b in buckets)
    assert all(b["observed_frequency"] is None for b in buckets)
    assert all(b["calibration_status"] == "insufficient_data" for b in buckets)


def test_calibration_curve_bucket_matches_handcalculated_example():
    """fc-1: {"bullish":0.6,"bearish":0.4}, actual="bullish" -> punkter
    (0.6,1) i bucket [0.6,0.7), (0.4,0) i bucket [0.4,0.5).
    fc-2: {"bullish":0.65,"bearish":0.35}, actual="bearish" -> punkter
    (0.65,0) i bucket [0.6,0.7), (0.35,1) i bucket [0.3,0.4).
    Bucket [0.6,0.7): sample_size=2, mean_predicted=(0.6+0.65)/2=0.625,
    observed_frequency=(1+0)/2=0.5."""
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish"),
        _forecast("fc-2", {"bullish": 0.65, "bearish": 0.35}, "bearish"),
    ]

    buckets = compute_calibration_curve(forecasts, min_sample_size=30, preliminary_sample_size=10)
    bucket_06 = next(b for b in buckets if b["bucket_low"] == str(Decimal("0.6")))

    assert bucket_06["sample_size"] == 2
    assert Decimal(bucket_06["mean_predicted"]) == Decimal("0.625")
    assert Decimal(bucket_06["observed_frequency"]) == Decimal("0.5")


def test_calibration_curve_predicted_probability_of_exactly_one_goes_to_last_bucket():
    """p == 1.0 far inte overflowa till en icke-existerande 11:e bucket -
    ska klampas in i sista bucketen [0.9, 1.0]."""
    forecasts = [_forecast("fc-1", {"up": 1.0, "down": 0.0}, "up")]

    buckets = compute_calibration_curve(forecasts, min_sample_size=30, preliminary_sample_size=10)
    last_bucket = buckets[-1]
    first_bucket = buckets[0]

    assert last_bucket["bucket_low"] == str(Decimal("0.9"))
    assert last_bucket["sample_size"] == 1
    assert Decimal(last_bucket["mean_predicted"]) == Decimal("1.0")
    assert first_bucket["sample_size"] == 1  # the 0.0 "down" point


def test_calibration_curve_excludes_points_from_unmatchable_forecasts():
    forecasts = [_forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "unknown_scenario")]
    buckets = compute_calibration_curve(forecasts, min_sample_size=30, preliminary_sample_size=10)
    assert all(b["sample_size"] == 0 for b in buckets)


def test_calibration_curve_bucket_status_reflects_its_own_sample_size():
    """Varje bucket degraderar TRANSPARENT och OBEROENDE - en bucket med
    otillräckligt underlag markeras så, utan att påverka andra buckets."""
    forecasts = [_forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish")]
    buckets = compute_calibration_curve(forecasts, min_sample_size=2, preliminary_sample_size=1)
    bucket_06 = next(b for b in buckets if b["bucket_low"] == str(Decimal("0.6")))
    bucket_00 = next(b for b in buckets if b["bucket_low"] == str(Decimal("0.0")))
    assert bucket_06["calibration_status"] == "preliminary"  # sample_size=1 >= prelim(1) < min(2)
    assert bucket_00["calibration_status"] == "insufficient_data"  # sample_size=0


# --- compute_calibration_breakdown_by_horizon -------------------------------


def test_breakdown_by_horizon_empty_for_no_forecasts():
    assert (
        compute_calibration_breakdown_by_horizon([], min_sample_size=30, preliminary_sample_size=10)
        == {}
    )


def test_breakdown_by_horizon_separates_groups():
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish", horizon="4h"),
        _forecast("fc-2", {"up": 1.0, "down": 0.0}, "up", horizon="24h"),
    ]

    result = compute_calibration_breakdown_by_horizon(
        forecasts, min_sample_size=30, preliminary_sample_size=10
    )

    assert set(result.keys()) == {"4h", "24h"}
    assert Decimal(result["4h"]["brier_score"]["value"]) == Decimal("0.32")
    assert result["4h"]["brier_score"]["sample_size"] == 1
    assert Decimal(result["24h"]["brier_score"]["value"]) == Decimal("0")
    assert result["4h"]["calibration_status"] == "insufficient_data"


# --- compute_calibration_breakdown_by_scenario -------------------------------


def test_breakdown_by_scenario_empty_for_no_forecasts():
    assert (
        compute_calibration_breakdown_by_scenario(
            [], min_sample_size=30, preliminary_sample_size=10
        )
        == {}
    )


def test_breakdown_by_scenario_separates_groups():
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish"),
        _forecast("fc-2", {"bullish": 0.65, "bearish": 0.35}, "bearish"),
    ]

    result = compute_calibration_breakdown_by_scenario(
        forecasts, min_sample_size=30, preliminary_sample_size=10
    )

    assert set(result.keys()) == {"bullish", "bearish"}
    assert result["bullish"]["sample_size"] == 2  # 0.6 and 0.65, both "bullish" key present
    assert Decimal(result["bullish"]["mean_predicted"]) == Decimal("0.625")
    assert Decimal(result["bullish"]["observed_frequency"]) == Decimal("0.5")  # only fc-1 occurred
    assert result["bearish"]["sample_size"] == 2
    assert Decimal(result["bearish"]["observed_frequency"]) == Decimal("0.5")  # only fc-2 occurred


# --- determinism (PLAN_CRYPTO_PHASE8.md §4) ---------------------------------


def test_calibration_curve_is_deterministic_on_repeated_calls():
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish"),
        _forecast("fc-2", {"up": 1.0, "down": 0.0}, "up"),
    ]
    a = compute_calibration_curve(forecasts, min_sample_size=30, preliminary_sample_size=10)
    b = compute_calibration_curve(forecasts, min_sample_size=30, preliminary_sample_size=10)
    assert a == b


def test_calibration_breakdowns_are_deterministic_on_repeated_calls():
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish", horizon="4h"),
        _forecast("fc-2", {"up": 1.0, "down": 0.0}, "up", horizon="24h"),
    ]
    assert compute_calibration_breakdown_by_horizon(
        forecasts, min_sample_size=30, preliminary_sample_size=10
    ) == compute_calibration_breakdown_by_horizon(
        forecasts, min_sample_size=30, preliminary_sample_size=10
    )
    assert compute_calibration_breakdown_by_scenario(
        forecasts, min_sample_size=30, preliminary_sample_size=10
    ) == compute_calibration_breakdown_by_scenario(
        forecasts, min_sample_size=30, preliminary_sample_size=10
    )
