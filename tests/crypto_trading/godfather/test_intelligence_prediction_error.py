"""Tests for crypto_trading/godfather/prediction_error.py.

The requirement's own final clause is the one under test here: a lesson
may influence future decisions ONLY when there is enough evidence. That
is enforced by a marker on the lesson text, so it cannot be forgotten at
a call site, and the default - for a system that has proven nothing - is
that nothing is actionable.
"""

from datetime import UTC, datetime

from crypto_trading.godfather.prediction_error import (
    build_prediction_errors,
    summarise_prediction_errors,
)
from crypto_trading.schemas.godfather import (
    ComponentVerdict,
    DecisionAudit,
    TradeInvestigation,
)

_NOW = datetime(2026, 9, 25, tzinfo=UTC)
_OBSERVATION_PREFIX = "OBSERVATION ONLY (insufficient evidence to act): "


def _investigation(classification="BAD_ENTRY", pnl="-12.5") -> TradeInvestigation:
    return TradeInvestigation(
        position_id="pos-1",
        candidate_id="cand-1",
        instrument="BTC-USDT",
        created_at=_NOW,
        classification=classification,
        entry_verdict="BAD",
        management_verdict="UNKNOWN",
        before={},
        during={
            "mfe_pct": "0.1",
            "minutes_to_mfe": 12.0,
            "giveback_ratio": "1.0",
            "max_progress_ratio": "0.1",
            "mae_pct": "-3.0",
            "sustained_decay_factors": {"momentum_decay": True, "volume_decay": False},
        },
        after={"realized_pnl_usdt": pnl, "exit_reason": "stop_loss", "hold_minutes": 90.0},
        run_id="run",
    )


def _audit(*, forecast_verdict="WRONG", risk_verdict="WRONG") -> DecisionAudit:
    return DecisionAudit(
        position_id="pos-1",
        candidate_id="cand-1",
        created_at=_NOW,
        components=[
            ComponentVerdict(
                component="bull_thesis",
                stance="BULLISH",
                expectation="momentum continues",
                verdict="WRONG",
                weight=1.0,
            ),
            ComponentVerdict(
                component="forecast",
                stance="BULLISH",
                expectation="most likely scenario 'bullish' at p=0.28",
                verdict=forecast_verdict,
                weight=0.28,
                evidence={
                    "realized_scenario": "bearish",
                    "probability_assigned_to_realized": 0.38,
                },
            ),
            ComponentVerdict(
                component="risk",
                stance="NEUTRAL",
                expectation="stop 3-4% below",
                verdict=risk_verdict,
                weight=1.0,
                evidence={"stop_loss": "95", "target": "110"},
            ),
        ],
        conflicts=["entry_rsi_at_or_above_80"],
        misleading_components=["bull_thesis"],
        missing_information=["no_instrument_specific_news_facts"],
        fault_domain="SIGNAL_SELECTION",
        run_id="run",
    )


def _by_source(records):
    return {record.source: record for record in records}


def test_every_scorable_expectation_gets_its_own_record():
    records = _by_source(build_prediction_errors(_investigation(), _audit()))

    assert set(records) == {"trade_thesis", "forecast_agent", "risk_agent"}


def test_a_lesson_is_marked_observation_only_without_supporting_evidence():
    """The default state of a young system, and the guard against a
    single fresh loss promoting itself into a rule."""
    records = _by_source(build_prediction_errors(_investigation(), _audit()))

    assert records["trade_thesis"].lesson.startswith(_OBSERVATION_PREFIX)
    assert records["trade_thesis"].detail["actionable"] is False


def test_a_lesson_drops_the_marker_once_experience_memory_backs_it():
    records = _by_source(
        build_prediction_errors(_investigation(), _audit(), {"trade_thesis"})
    )

    assert not records["trade_thesis"].lesson.startswith(_OBSERVATION_PREFIX)
    assert records["trade_thesis"].detail["actionable"] is True


def test_the_cause_is_assembled_from_observed_conditions_only():
    records = _by_source(build_prediction_errors(_investigation(), _audit()))
    cause = records["trade_thesis"].cause

    assert "sustained momentum_decay" in cause
    assert "entry_rsi_at_or_above_80" in cause
    assert "bull_thesis" in cause
    assert "no_instrument_specific_news_facts" in cause


def test_no_cause_is_invented_when_nothing_was_observed():
    investigation = _investigation()
    investigation.during["sustained_decay_factors"] = {}
    empty_audit = DecisionAudit(
        position_id="pos-1",
        candidate_id="cand-1",
        created_at=_NOW,
        components=[],
        conflicts=[],
        misleading_components=[],
        missing_information=[],
        fault_domain="UNKNOWN",
        run_id="run",
    )

    records = build_prediction_errors(investigation, empty_audit)

    assert records[0].cause == "no specific cause identifiable from the recorded evidence"


def test_the_forecast_error_magnitude_is_a_calibration_score():
    """1 - p(what actually happened): the probability mass the forecaster
    put on things that did not occur."""
    records = _by_source(build_prediction_errors(_investigation(), _audit()))

    assert records["forecast_agent"].magnitude == 0.62


def test_an_unscorable_component_produces_no_prediction_error_record():
    records = _by_source(
        build_prediction_errors(
            _investigation(), _audit(forecast_verdict="UNSCORABLE", risk_verdict="UNSCORABLE")
        )
    )

    assert set(records) == {"trade_thesis"}


def test_a_trade_with_no_known_pnl_produces_no_thesis_record():
    investigation = _investigation()
    investigation.after["realized_pnl_usdt"] = None

    records = _by_source(build_prediction_errors(investigation, _audit()))

    assert "trade_thesis" not in records


def test_the_error_text_names_selectivity_for_a_bad_entry():
    records = _by_source(build_prediction_errors(_investigation("BAD_ENTRY"), _audit()))

    assert "would continue at all" in records["trade_thesis"].error
    assert "entry selectivity" in records["trade_thesis"].lesson


def test_the_error_text_names_exit_timing_for_a_management_failure():
    records = _by_source(
        build_prediction_errors(_investigation("EXIT_TOO_LATE"), _audit())
    )

    assert "how long" in records["trade_thesis"].error
    assert "exit timing" in records["trade_thesis"].lesson


def test_a_clean_win_records_no_material_error():
    records = _by_source(
        build_prediction_errors(
            _investigation("GOOD_ENTRY_GOOD_MANAGEMENT", pnl="40"), _audit()
        )
    )

    assert "none material" in records["trade_thesis"].error


def test_the_summary_separates_actionable_lessons_from_observations():
    actionable = build_prediction_errors(_investigation(), _audit(), {"trade_thesis"})
    observation = build_prediction_errors(_investigation(), _audit())
    rows = [r.model_dump(mode="json") for r in (*actionable, *observation)]

    summary = summarise_prediction_errors(rows)

    assert summary["trade_thesis"]["actionable"] == 1
    assert summary["trade_thesis"]["observation_only"] == 1


def test_the_summary_averages_forecast_calibration_error():
    rows = [r.model_dump(mode="json") for r in build_prediction_errors(_investigation(), _audit())]

    summary = summarise_prediction_errors(rows)

    assert summary["forecast_agent"]["mean_magnitude"] == 0.62


def test_the_summary_reports_no_magnitude_when_none_was_measurable():
    rows = [
        {"source": "trade_thesis", "lesson": "x", "magnitude": None},
    ]

    assert summarise_prediction_errors(rows)["trade_thesis"]["mean_magnitude"] is None
