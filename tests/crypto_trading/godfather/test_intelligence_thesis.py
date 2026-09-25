"""Tests for crypto_trading/godfather/thesis.py.

Two things are being protected here. The first is that the thesis state
machine is genuinely deterministic and reachable in every state - a
classifier with an unreachable branch is a classifier that quietly never
fires. The second is the safety contract: whatever this module
recommends, it may never widen a stop, never drop one, and never emit an
action outside the five permitted ones. Those invariants are tested even
though nothing enforces thesis actions in this phase, because a later
activation must change only WHO reads the row - never whether the row was
safe.
"""

from decimal import Decimal

from crypto_trading.godfather.thesis import (
    ThesisDecision,
    ThesisThresholds,
    build_thesis_features,
    classify_thesis_state,
    evaluate_thesis,
    validate_action_is_safe,
)
from tests.crypto_trading.godfather.intelligence_fixtures import make_position, path_point

_THRESHOLDS = ThesisThresholds(
    watch=Decimal("0.35"), protect=Decimal("0.55"), exit=Decimal("0.75"), max_hold_hours=24
)


def _features(position, points):
    return build_thesis_features(position, points, _THRESHOLDS.max_hold_hours)


def test_a_healthy_position_past_halfway_is_strong():
    position = make_position()
    points = [
        path_point(0, Decimal("100"), position=position),
        path_point(30, Decimal("106"), position=position),
    ]

    state, reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "STRONG"
    assert "more_than_half_way_to_target_in_profit" in reasons


def test_a_quiet_early_position_is_valid():
    position = make_position()
    points = [path_point(5, Decimal("100.5"), position=position)]

    state, _reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "VALID"


def test_guardian_watch_level_decay_weakens_the_thesis():
    position = make_position()
    points = [path_point(30, Decimal("100.2"), position=position, decay=Decimal("0.4"))]

    state, reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "WEAKENING"
    assert "guardian_decay_watch" in reasons


def test_giving_back_half_a_favourable_move_weakens_the_thesis():
    position = make_position()
    points = [
        path_point(10, Decimal("104"), position=position),
        path_point(40, Decimal("102"), position=position),
    ]

    state, reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "WEAKENING"
    assert "gave_back_half_of_favourable_move" in reasons


def test_giving_back_almost_all_of_a_favourable_move_invalidates_the_thesis():
    position = make_position()
    points = [
        path_point(10, Decimal("104"), position=position),
        path_point(40, Decimal("100.5"), position=position),
    ]

    state, reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "INVALID"
    assert "severe_giveback_of_favourable_move" in reasons


def test_a_losing_position_with_collapsed_momentum_and_volume_is_invalid():
    position = make_position()
    points = [
        path_point(
            60,
            Decimal("98"),
            position=position,
            factors={"momentum_decay": 0.9, "volume_decay": 0.95},
        )
    ]

    state, reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "INVALID"
    assert "entry_thesis_fully_invalidated" in reasons


def test_guardian_exit_level_decay_maps_to_the_exit_state():
    position = make_position()
    points = [path_point(60, Decimal("97"), position=position, decay=Decimal("0.9"))]

    state, reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "EXIT"
    assert "guardian_decay_exit" in reasons


def test_time_running_out_without_progress_weakens_the_thesis():
    position = make_position()
    # 20 of 24 hours gone, still essentially at entry.
    points = [path_point(20 * 60, Decimal("100.1"), position=position)]

    state, reasons = classify_thesis_state(_features(position, points), _THRESHOLDS)

    assert state == "WEAKENING"
    assert "time_exhausted_without_progress" in reasons


def test_an_intact_thesis_recommends_hold():
    position = make_position()
    points = [path_point(5, Decimal("100.5"), position=position)]

    decision = evaluate_thesis(position, _features(position, points), _THRESHOLDS)

    assert decision.action == "HOLD"
    assert decision.proposed_stop_loss is None


def test_a_weakening_profitable_position_with_big_giveback_tightens_the_stop():
    position = make_position()
    points = [
        path_point(10, Decimal("106"), position=position),
        path_point(40, Decimal("102"), position=position),
    ]

    decision = evaluate_thesis(position, _features(position, points), _THRESHOLDS)

    assert decision.action == "TIGHTEN_SL"
    assert decision.proposed_stop_loss == position.simulated_fill_entry
    assert validate_action_is_safe(position, decision) == []


def test_a_weakening_losing_position_reduces_exposure():
    position = make_position()
    points = [path_point(30, Decimal("99"), position=position, decay=Decimal("0.4"))]

    decision = evaluate_thesis(position, _features(position, points), _THRESHOLDS)

    assert decision.action == "REDUCE"
    assert decision.proposed_stop_loss is None


def test_an_invalid_thesis_recommends_exit():
    position = make_position()
    points = [path_point(60, Decimal("97"), position=position, decay=Decimal("0.9"))]

    decision = evaluate_thesis(position, _features(position, points), _THRESHOLDS)

    assert decision.action == "EXIT"


def test_a_stop_that_is_already_tighter_than_breakeven_is_never_widened():
    """The invariant in its sharpest form: a position whose stop is
    already ABOVE the entry must not be handed a breakeven proposal,
    because that would move the stop further away."""
    position = make_position(stop_loss=Decimal("103"))
    points = [
        path_point(10, Decimal("108"), position=position),
        path_point(40, Decimal("104"), position=position),
    ]

    decision = evaluate_thesis(position, _features(position, points), _THRESHOLDS)

    assert decision.proposed_stop_loss is None
    assert decision.action == "PROTECT"
    assert validate_action_is_safe(position, decision) == []


def test_validation_rejects_a_tighten_that_would_widen_the_stop():
    position = make_position(stop_loss=Decimal("99"))
    unsafe = ThesisDecision(
        state="WEAKENING",
        action="TIGHTEN_SL",
        reason_codes=[],
        proposed_stop_loss=Decimal("90"),
    )

    violations = validate_action_is_safe(position, unsafe)

    assert violations == ["proposed stop is not strictly tighter than the current stop"]


def test_validation_rejects_a_tighten_with_no_proposed_stop():
    unsafe = ThesisDecision(
        state="WEAKENING", action="TIGHTEN_SL", reason_codes=[], proposed_stop_loss=None
    )

    assert validate_action_is_safe(make_position(), unsafe) == [
        "TIGHTEN_SL without a proposed stop"
    ]


def test_validation_rejects_a_non_tighten_action_that_carries_a_stop():
    unsafe = ThesisDecision(
        state="VALID", action="HOLD", reason_codes=[], proposed_stop_loss=Decimal("99")
    )

    assert validate_action_is_safe(make_position(), unsafe) == [
        "HOLD must not carry a proposed stop"
    ]


def test_features_are_none_for_an_empty_path():
    assert build_thesis_features(make_position(), [], 24) is None


def test_features_only_see_the_prefix_they_are_given():
    """The property counterfactual replay depends on: a later point
    cannot change an earlier evaluation."""
    position = make_position()
    early = [path_point(10, Decimal("104"), position=position)]
    later = early + [path_point(40, Decimal("130"), position=position)]

    assert _features(position, early).mfe_pct_so_far == Decimal("4")
    assert _features(position, later).mfe_pct_so_far == Decimal("30")
