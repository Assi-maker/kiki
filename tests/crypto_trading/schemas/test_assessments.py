from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)

_BASE_KWARGS = {"agent_name": "test-agent", "run_id": "run-1", "created_at": datetime.now(UTC)}


def test_news_sentiment_separates_fact_claim_interpretation():
    a = NewsSentimentAssessment(
        **_BASE_KWARGS,
        status="ok",
        verified_facts=["BTC traded above 50000"],
        source_claims=["source X claims institutional buying"],
        interpretation="short-term bullish sentiment",
    )
    assert a.verified_facts != a.source_claims


def test_qa_assessment_passed_and_violations():
    a = QAAssessment(**_BASE_KWARGS, status="ok", passed=False, violations=["missing risk field"])
    assert a.passed is False
    assert a.violations == ["missing risk field"]


def test_forecast_scenario_probabilities_must_sum_to_one():
    with pytest.raises(ValidationError):
        ForecastAssessment(
            **_BASE_KWARGS,
            status="ok",
            scenario_probabilities={"bullish": 0.9, "bearish": 0.5},
            horizon="4h",
            forecast_version="v1",
        )


def test_forecast_scenario_probabilities_valid_sum():
    a = ForecastAssessment(
        **_BASE_KWARGS,
        status="ok",
        scenario_probabilities={"bullish": 0.6, "neutral": 0.25, "bearish": 0.15},
        horizon="4h",
        forecast_version="v1",
    )
    assert abs(sum(a.scenario_probabilities.values()) - 1.0) < 0.001


def test_an_ok_forecast_with_empty_probabilities_is_still_invalid():
    with pytest.raises(ValidationError):
        ForecastAssessment(
            **_BASE_KWARGS, status="ok", scenario_probabilities={}, horizon="4h",
            forecast_version="v1",
        )


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_a_failed_forecast_placeholder_reads_back(status):
    """2026-09-29: the runner persists a failed role as a placeholder with
    scenario_probabilities={} (model_construct). The sum-to-one business rule
    is for REAL model answers (status="ok") - applying it on read made every
    failed forecast unreadable (310 NO_TRADE candidates ->
    CorruptCandidateStateError)."""
    raw = (
        '{"agent_name":"crypto-forecast-agent","run_id":"r","created_at":"2026-09-02T09:07:06Z",'
        f'"status":"{status}","scenario_probabilities":{{}},"horizon":"","forecast_version":""}}'
    )
    a = ForecastAssessment.model_validate_json(raw)
    assert a.status == status
    assert a.scenario_probabilities == {}


def test_risk_assessment_is_advisory_fields_only():
    a = RiskAssessment(
        **_BASE_KWARGS,
        status="ok",
        suggested_stop_loss="49000",
        suggested_target="53000",
        downside="high volatility",
        liquidity_risk="low",
        model_risk="medium",
        timing_risk="low",
    )
    assert a.suggested_stop_loss == "49000"


def test_bear_adversarial_requires_falsification_conditions():
    a = BearAdversarialAssessment(
        **_BASE_KWARGS,
        status="ok",
        counterarguments=["overbought on 4h RSI"],
        alternative_explanations=["thin weekend liquidity"],
        falsification_conditions="price closes below 48000 on daily",
    )
    assert a.falsification_conditions


def test_technical_and_bull_thesis_construct():
    TechnicalAssessment(
        **_BASE_KWARGS, status="ok", market_data={"price": 50000}, interpretation="uptrend"
    )
    BullThesisAssessment(
        **_BASE_KWARGS,
        status="ok",
        hypothesis="breakout",
        catalyst="ETF news",
        setup="range breakout",
    )
