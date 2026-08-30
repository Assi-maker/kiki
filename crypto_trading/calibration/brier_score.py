from __future__ import annotations

from decimal import Decimal

from crypto_trading.schemas.forecast import ForecastRecord


def is_forecast_matchable(forecast: ForecastRecord) -> bool:
    """Ett forecast kan bara bedömas om dess actual_outcome är EN AV DESS
    EGNA scenario_probabilities-nycklar (SPEC §9 / PLAN_CRYPTO_PHASE8.md §2:
    scenario-etiketter är fri text, ingen central mappning existerar -
    gissar aldrig vilken kategori som "egentligen" menades)."""
    return forecast.actual_outcome is not None and (
        forecast.actual_outcome in forecast.scenario_probabilities
    )


def _forecast_brier_term(forecast: ForecastRecord) -> Decimal:
    """Multi-kategori Brier score-term för ETT forecast (Briers
    ursprungliga formulering): sum over scenario_probabilities-nycklar av
    (predicted - indicator)^2, där indicator = 1 för den nyckel som
    inträffade, 0 för övriga."""
    total = Decimal("0")
    for scenario, predicted in forecast.scenario_probabilities.items():
        indicator = Decimal("1") if scenario == forecast.actual_outcome else Decimal("0")
        predicted_decimal = Decimal(str(predicted))
        total += (predicted_decimal - indicator) ** 2
    return total


def compute_brier_score(forecasts: list[ForecastRecord]) -> dict:
    """Medel över samtliga MATCHBARA forecasts (se is_forecast_matchable)
    av deras individuella Brier-termer. Forecasts som inte kan matchas
    exkluderas explicit och räknas i excluded_count - aldrig en gissning.

    Returnerar {"value": str(Decimal)|None, "sample_size": int,
    "excluded_count": int}. value/sample_size är None/0 om ingen forecast
    kunde bedömas (tom lista in, eller alla exkluderade)."""
    matchable = [f for f in forecasts if is_forecast_matchable(f)]
    excluded_count = len(forecasts) - len(matchable)
    if not matchable:
        return {"value": None, "sample_size": 0, "excluded_count": excluded_count}
    total = sum((_forecast_brier_term(f) for f in matchable), Decimal("0"))
    average = total / Decimal(len(matchable))
    return {
        "value": str(average),
        "sample_size": len(matchable),
        "excluded_count": excluded_count,
    }
