"""Tests for crypto_trading/godfather/auditor.py.

The scoreboard this module produces is only worth anything if each
component is graded on the claim it actually made. Two of these tests
exist specifically because the first real sweep got that wrong: the Bear
role was being graded against the FAVOURABLE excursion (a question it
never asks) and came out 31/67, and QA - which makes no market claim at
all - must never be silently counted right whenever a trade won.
"""

from decimal import Decimal

from crypto_trading.godfather.auditor import (
    audit_decision,
    detect_conflicts,
    detect_missing_information,
    determine_fault_domain,
)
from crypto_trading.godfather.path import compute_path_metrics, reconstruct_price_path
from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.candidate import Candidate
from tests.crypto_trading.godfather.intelligence_fixtures import (
    NOW,
    evidence_record,
    make_position,
    observation_row,
)

_WATCH = Decimal("0.35")
_EXIT = Decimal("0.75")


def _metrics(position, prices_at_minutes):
    rows = [observation_row(position, m, p) for m, p in prices_at_minutes]
    return compute_path_metrics(
        position, reconstruct_price_path(position, rows), _WATCH, _EXIT
    )


def _assessment_kwargs(name):
    return {"agent_name": name, "run_id": "r", "created_at": NOW, "status": "ok"}


def _candidate(
    *,
    evidence=None,
    counterarguments=("overbought at RSI 84",),
    bullish_probability=0.28,
    verified_facts=("Fear & Greed index reads 69",),
    suggested_stop_loss="roughly 3-4% below the last price",
    qa_passed=True,
    reference_price=Decimal("100"),
) -> Candidate:
    return Candidate(
        candidate_id="pos-1",
        idempotency_key="k",
        instrument="BTC-USDT",
        discovery_run_id="r",
        evidence_hash="h",
        status="CONFIRMED",
        evidence_record=evidence or evidence_record(),
        created_at=NOW,
        updated_at=NOW,
        reference_price=reference_price,
        news_sentiment=NewsSentimentAssessment(
            **_assessment_kwargs("news"),
            verified_facts=list(verified_facts),
            source_claims=[],
            interpretation="neutral",
        ),
        technical=TechnicalAssessment(
            **_assessment_kwargs("technical"),
            market_data={"momentum_rsi_30m": {"triggered": True, "value": 84.0}},
            interpretation="momentum breakout",
        ),
        bull_thesis=BullThesisAssessment(
            **_assessment_kwargs("bull"),
            hypothesis="momentum continues",
            catalyst="rsi breakout",
            setup="long",
        ),
        forecast=ForecastAssessment(
            **_assessment_kwargs("forecast"),
            scenario_probabilities={
                "bullish": bullish_probability,
                "neutral": 0.34,
                "bearish": round(1 - bullish_probability - 0.34, 2),
            },
            horizon="4h",
            forecast_version="v1",
        ),
        risk=RiskAssessment(
            **_assessment_kwargs("risk"),
            suggested_stop_loss=suggested_stop_loss,
            suggested_target="3-5% above",
            downside="mean reversion",
            liquidity_risk="low",
            model_risk="moderate",
            timing_risk="late entry",
        ),
        bear_adversarial=BearAdversarialAssessment(
            **_assessment_kwargs("bear"),
            counterarguments=list(counterarguments),
            alternative_explanations=[],
            falsification_conditions="invalidated below the prior swing low",
        ),
        qa=QAAssessment(**_assessment_kwargs("qa"), passed=qa_passed, violations=[]),
    )


def _audit(position, metrics, candidate, *, realized=Decimal("-10"), classification="BAD_ENTRY"):
    return audit_decision(
        position=position,
        candidate=candidate,
        opportunity_screen={"opportunity_score": 8.0},
        gate_decision={"decision": "CONFIRMED", "reasons": "[]"},
        metrics=metrics,
        realized_pnl=realized,
        classification=classification,
        entry_verdict="BAD",
        management_verdict="UNKNOWN",
        now=NOW,
        run_id="run",
    )


def _component(audit, name):
    return next(c for c in audit.components if c.component == name)


def test_a_bullish_component_is_right_when_a_real_move_happened():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    audit = _audit(position, metrics, _candidate())

    assert _component(audit, "bull_thesis").verdict == "RIGHT"
    assert _component(audit, "quant_screener").verdict == "RIGHT"


def test_a_bullish_component_is_wrong_when_the_price_never_moved():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("100.1"))])

    audit = _audit(position, metrics, _candidate())

    assert _component(audit, "bull_thesis").verdict == "WRONG"


def test_a_directional_call_in_the_middle_band_is_unscorable():
    """Between "never moved" and "moved properly" there is a region where
    the call was neither vindicated nor refuted; forcing a verdict there
    manufactures signal."""
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("100.5"))])

    audit = _audit(position, metrics, _candidate())

    assert _component(audit, "bull_thesis").verdict == "UNSCORABLE"


def test_the_bear_is_graded_on_downside_that_materialised_not_on_upside():
    """The trade rose 4% AND fell 2%. The Bear warned about downside, and
    the downside happened - so it was right, even though the price also
    went up, which is a question it never asked."""
    position = make_position()
    metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("104")), (60, Decimal("98"))]
    )

    audit = _audit(position, metrics, _candidate())

    assert _component(audit, "bear_adversarial").verdict == "RIGHT"
    assert _component(audit, "bull_thesis").verdict == "RIGHT"


def test_the_bear_is_wrong_only_when_the_price_never_went_against_the_position():
    position = make_position()
    metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("104")), (60, Decimal("106"))]
    )

    audit = _audit(position, metrics, _candidate())

    assert _component(audit, "bear_adversarial").verdict == "WRONG"


def test_a_bear_with_no_counterargument_made_no_claim_to_grade():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("90"))])

    audit = _audit(position, metrics, _candidate(counterarguments=()))

    assert _component(audit, "bear_adversarial").verdict == "UNSCORABLE"


def test_qa_is_structurally_unscorable_and_carries_no_weight():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    audit = _audit(position, metrics, _candidate())
    qa = _component(audit, "qa")

    assert qa.verdict == "UNSCORABLE"
    assert qa.weight == 0.0


def test_news_without_a_directional_claim_is_unscorable():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    audit = _audit(position, metrics, _candidate())

    assert _component(audit, "news_sentiment").verdict == "UNSCORABLE"


def test_the_forecast_is_graded_on_the_scenario_that_actually_happened():
    position = make_position(exit_price=Decimal("104"))
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    audit = _audit(position, metrics, _candidate(bullish_probability=0.6))
    forecast = _component(audit, "forecast")

    assert forecast.evidence["realized_scenario"] == "bullish"
    assert forecast.verdict == "RIGHT"
    assert forecast.evidence["probability_assigned_to_realized"] == 0.6


def test_the_forecast_records_the_probability_it_gave_to_reality_even_when_wrong():
    """Calibration material: being wrong while having assigned 0.28 to
    what happened is a different failure from being wrong at 0.05."""
    position = make_position(exit_price=Decimal("104"))
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    audit = _audit(position, metrics, _candidate(bullish_probability=0.28))
    forecast = _component(audit, "forecast")

    assert forecast.verdict == "WRONG"
    assert forecast.evidence["probability_assigned_to_realized"] == 0.28


def test_the_gate_is_the_one_component_graded_on_money():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    losing = _audit(position, metrics, _candidate(), realized=Decimal("-10"))
    winning = _audit(position, metrics, _candidate(), realized=Decimal("10"))

    assert _component(losing, "gate").verdict == "WRONG"
    assert _component(winning, "gate").verdict == "RIGHT"


def test_guardian_is_wrong_when_it_called_a_losing_position_healthy_throughout():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("96"))])

    audit = _audit(position, metrics, _candidate(), realized=Decimal("-40"))

    assert _component(audit, "guardian").verdict == "WRONG"


def test_guardian_is_unscorable_on_a_winning_trade():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    audit = _audit(position, metrics, _candidate(), realized=Decimal("40"))

    assert _component(audit, "guardian").verdict == "UNSCORABLE"


def test_the_risk_role_is_graded_on_stop_and_target_placement_not_direction():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    wide = _audit(position, metrics, _candidate(), classification="SL_TOO_WIDE")
    hit_target = _audit(
        make_position(exit_reason="target"), metrics, _candidate(), classification="NOISE"
    )

    assert _component(wide, "risk").verdict == "WRONG"
    assert _component(hit_target, "risk").verdict == "RIGHT"


def test_a_missing_candidate_makes_every_pre_entry_component_unscorable():
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    audit = _audit(position, metrics, None)

    for name in ("bull_thesis", "bear_adversarial", "forecast", "risk", "qa", "technical"):
        assert _component(audit, name).verdict == "UNSCORABLE"


def test_only_weighted_wrong_components_count_as_misleading():
    """A zero-weight component that was wrong misled nobody."""
    position = make_position()
    metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("100.1"))])

    audit = _audit(position, metrics, _candidate())

    assert "qa" not in audit.misleading_components
    assert "news_sentiment" not in audit.misleading_components
    assert "bull_thesis" in audit.misleading_components


# ---------------------------------------------------------------------
# Conflicts and missing information
# ---------------------------------------------------------------------


def test_conflicts_are_detected_as_countable_codes():
    candidate = _candidate(
        evidence=evidence_record(
            rsi=84.0, volume_zscore=-0.5, volume_triggered=False, momentum_triggered=True
        ),
        counterarguments=("a", "b", "c"),
        bullish_probability=0.2,
    )

    conflicts = detect_conflicts(candidate, {"decision": "CONFIRMED"})

    assert "entry_rsi_at_or_above_80" in conflicts
    assert "momentum_triggered_without_volume_confirmation" in conflicts
    assert "entered_on_below_average_volume" in conflicts
    assert "bear_raised_3_counterarguments" in conflicts
    assert "confirmed_while_forecast_most_likely_bearish" in conflicts
    assert "confirmed_with_bullish_probability_below_0.35" in conflicts
    assert "single_trigger_reason_only" in conflicts


def test_no_conflicts_are_invented_when_the_candidate_is_missing():
    assert detect_conflicts(None, {"decision": "CONFIRMED"}) == []


def test_missing_information_names_what_the_decision_never_had():
    candidate = _candidate(
        evidence=evidence_record(secondary_triggered=None),
        verified_facts=("nothing specific",),
        suggested_stop_loss="a bit below the recent swing low",
        reference_price=None,
    )

    missing = detect_missing_information(candidate)

    assert "no_secondary_timeframe_evidence" in missing
    assert "no_instrument_specific_news_facts" in missing
    assert "risk_agent_gave_no_numeric_stop_loss" in missing
    assert "no_reference_price_for_risk_anchoring" in missing


def test_instrument_specific_news_is_recognised_when_present():
    candidate = _candidate(verified_facts=("BTC funding turned positive overnight",))

    assert "no_instrument_specific_news_facts" not in detect_missing_information(candidate)


def test_fault_domain_follows_the_two_quality_verdicts():
    assert determine_fault_domain("BAD", "GOOD") == "SIGNAL_SELECTION"
    assert determine_fault_domain("GOOD", "BAD") == "POSITION_MANAGEMENT"
    assert determine_fault_domain("BAD", "BAD") == "BOTH"
    assert determine_fault_domain("GOOD", "GOOD") == "NEITHER"
    assert determine_fault_domain("UNKNOWN", "GOOD") == "UNKNOWN"
