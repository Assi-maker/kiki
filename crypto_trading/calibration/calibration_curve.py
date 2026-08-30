from __future__ import annotations

from decimal import Decimal

from crypto_trading.calibration.brier_score import compute_brier_score, is_forecast_matchable
from crypto_trading.schemas.forecast import ForecastRecord

_NUM_BUCKETS = 10


def compute_calibration_status(
    sample_size: int, min_sample_size: int, preliminary_sample_size: int
) -> str:
    """ "insufficient_data" (sample_size < preliminary_sample_size),
    "preliminary" (preliminary_sample_size <= sample_size < min_sample_size),
    "calibrated" (sample_size >= min_sample_size). Trösklarna skickas alltid
    in explicit av anroparen (från settings.pipeline) - aldrig hårdkodade
    här (PLAN_CRYPTO_PHASE8.md Global Constraints)."""
    if sample_size >= min_sample_size:
        return "calibrated"
    if sample_size >= preliminary_sample_size:
        return "preliminary"
    return "insufficient_data"


def _binary_points(
    forecasts: list[ForecastRecord],
) -> list[tuple[str, Decimal, int]]:
    """(scenario_key, predicted_prob, occurred 0/1) för varje
    scenario_probabilities-nyckel av varje MATCHBART forecast (samma
    matchbarhetsregel som brier_score.py - ett forecast vars actual_outcome
    inte är en av dess egna nycklar bidrar noll punkter alls, inte bara för
    den "fel" nyckeln)."""
    points: list[tuple[str, Decimal, int]] = []
    for forecast in forecasts:
        if not is_forecast_matchable(forecast):
            continue
        for scenario, predicted in forecast.scenario_probabilities.items():
            occurred = 1 if scenario == forecast.actual_outcome else 0
            points.append((scenario, Decimal(str(predicted)), occurred))
    return points


def _bucket_index(predicted: Decimal) -> int:
    """Klampar predicted==1.0 in i sista bucketen istället för att
    overflowa till ett icke-existerande 11:e index."""
    index = int(predicted / Decimal("1") * _NUM_BUCKETS)
    return min(index, _NUM_BUCKETS - 1)


def _summarize_points(
    points: list[tuple[Decimal, int]], min_sample_size: int, preliminary_sample_size: int
) -> dict:
    sample_size = len(points)
    if sample_size == 0:
        mean_predicted = None
        observed_frequency = None
    else:
        mean_predicted = str(sum((p for p, _ in points), Decimal("0")) / Decimal(sample_size))
        observed_frequency = str(
            sum((Decimal(o) for _, o in points), Decimal("0")) / Decimal(sample_size)
        )
    return {
        "sample_size": sample_size,
        "mean_predicted": mean_predicted,
        "observed_frequency": observed_frequency,
        "calibration_status": compute_calibration_status(
            sample_size, min_sample_size, preliminary_sample_size
        ),
    }


def compute_calibration_curve(
    forecasts: list[ForecastRecord],
    min_sample_size: int,
    preliminary_sample_size: int,
    bucket_width: Decimal = Decimal("0.1"),
) -> list[dict]:
    """SPEC §9: "av alla gånger agenten sa ~60%, hur ofta inträffade det
    verkligen". 10 fasta buckets [0.0,0.1), [0.1,0.2), ..., [0.9,1.0]
    (bucket_width=0.1 default). Varje bucket degraderar TRANSPARENT och
    OBEROENDE av de andra (egen sample_size/calibration_status)."""
    points_by_bucket: list[list[tuple[Decimal, int]]] = [[] for _ in range(_NUM_BUCKETS)]
    for _scenario, predicted, occurred in _binary_points(forecasts):
        points_by_bucket[_bucket_index(predicted)].append((predicted, occurred))

    buckets = []
    for i in range(_NUM_BUCKETS):
        bucket_low = Decimal(i) * bucket_width
        bucket_high = Decimal(i + 1) * bucket_width
        summary = _summarize_points(points_by_bucket[i], min_sample_size, preliminary_sample_size)
        buckets.append({"bucket_low": str(bucket_low), "bucket_high": str(bucket_high), **summary})
    return buckets


def compute_calibration_breakdown_by_horizon(
    forecasts: list[ForecastRecord], min_sample_size: int, preliminary_sample_size: int
) -> dict[str, dict]:
    """Grupperar på forecast.horizon - redan persisterad fri-textsträng,
    grupperas LITERALT, ingen parsning/tolkning av vad t.ex. "4h" betyder.
    Återanvänder compute_brier_score() på varje delmängd - ingen ny
    formel."""
    horizons = sorted({f.horizon for f in forecasts})
    result = {}
    for horizon in horizons:
        group = [f for f in forecasts if f.horizon == horizon]
        brier = compute_brier_score(group)
        result[horizon] = {
            "brier_score": brier,
            "calibration_status": compute_calibration_status(
                brier["sample_size"], min_sample_size, preliminary_sample_size
            ),
        }
    return result


def compute_calibration_breakdown_by_scenario(
    forecasts: list[ForecastRecord], min_sample_size: int, preliminary_sample_size: int
) -> dict[str, dict]:
    """Grupperar de binära punkterna (samma decomposition som
    compute_calibration_curve()) på scenario-nyckel istället för
    sannolikhetsintervall - t.ex. "bullish" för sig, "bearish" för sig."""
    points = _binary_points(forecasts)
    scenarios = sorted({scenario for scenario, _, _ in points})
    result = {}
    for scenario in scenarios:
        scenario_points = [(p, o) for s, p, o in points if s == scenario]
        result[scenario] = _summarize_points(
            scenario_points, min_sample_size, preliminary_sample_size
        )
    return result
