from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.calibration.brier_score import compute_brier_score
from crypto_trading.schemas.forecast import ForecastRecord

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _forecast(
    forecast_id: str,
    scenario_probabilities: dict[str, float],
    actual_outcome: str | None,
    horizon: str = "4h",
    outcome_timestamp: datetime | None = None,
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
        outcome_timestamp=outcome_timestamp,
    )


def test_brier_score_is_none_when_sample_size_zero():
    result = compute_brier_score([])
    assert result == {"value": None, "sample_size": 0, "excluded_count": 0}


def test_brier_score_matches_a_handcalculated_example():
    """{"bullish": 0.6, "bearish": 0.4}, actual="bullish":
    BS = (0.6-1)^2 + (0.4-0)^2 = 0.16 + 0.16 = 0.32"""
    forecasts = [_forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish")]

    result = compute_brier_score(forecasts)

    assert Decimal(result["value"]) == Decimal("0.32")
    assert result["sample_size"] == 1
    assert result["excluded_count"] == 0


def test_brier_score_averages_over_multiple_forecasts():
    # fc-1: BS = 0.32 (see above). fc-2: {"up":1.0,"down":0.0}, actual="up"
    # -> BS = (1.0-1)^2 + (0.0-0)^2 = 0. Average = (0.32 + 0) / 2 = 0.16
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish"),
        _forecast("fc-2", {"up": 1.0, "down": 0.0}, "up"),
    ]

    result = compute_brier_score(forecasts)

    assert Decimal(result["value"]) == Decimal("0.16")
    assert result["sample_size"] == 2
    assert result["excluded_count"] == 0


def test_brier_score_excludes_forecast_with_unmatched_actual_outcome():
    """actual_outcome="unknown_scenario" är INTE en av forecastens egna
    scenario_probabilities-nycklar - exkluderas explicit, gissas aldrig."""
    forecasts = [_forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "unknown_scenario")]

    result = compute_brier_score(forecasts)

    assert result == {"value": None, "sample_size": 0, "excluded_count": 1}


def test_brier_score_excludes_forecast_with_none_actual_outcome():
    """Defensivt: find_forecasts_with_outcome() filtrerar redan bort
    actual_outcome IS NULL på DB-nivå, men funktionen ska ändå aldrig
    krascha eller räkna med ett None-outcome om den råkar få in en."""
    forecasts = [_forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, None)]

    result = compute_brier_score(forecasts)

    assert result == {"value": None, "sample_size": 0, "excluded_count": 1}


def test_brier_score_mix_of_matched_and_unmatched():
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish"),
        _forecast("fc-2", {"bullish": 0.6, "bearish": 0.4}, "unknown_scenario"),
    ]

    result = compute_brier_score(forecasts)

    assert Decimal(result["value"]) == Decimal("0.32")  # only fc-1 counted
    assert result["sample_size"] == 1
    assert result["excluded_count"] == 1


def test_brier_score_is_deterministic_on_repeated_calls():
    forecasts = [
        _forecast("fc-1", {"bullish": 0.6, "bearish": 0.4}, "bullish"),
        _forecast("fc-2", {"up": 1.0, "down": 0.0}, "up"),
    ]
    assert compute_brier_score(forecasts) == compute_brier_score(forecasts)


def test_brier_score_result_depends_only_on_its_explicit_input_never_hidden_state():
    """No-look-ahead (PLAN_CRYPTO_PHASE8.md §4): compute_brier_score() har
    ingen egen klocka, ingen DB-åtkomst, ingen global state - dess resultat
    beror ENDAST på vad som skickas in. En "as of T"-gräns (om den någonsin
    byggs) skulle helt kunna implementeras genom att FILTRERA listan innan
    anrop - bevisas här genom att ett tidigt och ett sent forecast (skilda
    på outcome_timestamp) ger olika resultat beroende på vilken delmängd
    som skickas in, aldrig på en tidpunkt funktionen själv känner till."""
    early = _forecast(
        "fc-early",
        {"bullish": 0.6, "bearish": 0.4},
        "bullish",
        outcome_timestamp=datetime(2026, 8, 30, 10, tzinfo=UTC),
    )
    late = _forecast(
        "fc-late",
        {"up": 1.0, "down": 0.0},
        "up",
        outcome_timestamp=datetime(2026, 8, 30, 20, tzinfo=UTC),
    )

    result_early_only = compute_brier_score([early])
    result_both = compute_brier_score([early, late])

    assert result_early_only["sample_size"] == 1
    assert result_both["sample_size"] == 2
    assert result_early_only != result_both
