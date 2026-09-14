import json
from decimal import Decimal

import pytest

from crypto_trading.guardian.authority import (
    decide_open_position,
    decide_pre_entry,
    evaluate_heuristics,
)


def _heuristic(
    heuristic_id="h-1",
    condition=None,
    adjustment=0.15,
    confidence=0.75,
    description="test heuristic",
    sample_size=10,
):
    return {
        "heuristic_id": heuristic_id,
        "description": description,
        "condition_json": json.dumps(condition if condition is not None else {}),
        "adjustment": adjustment,
        "confidence": confidence,
        "sample_size": sample_size,
        "updated_at": "2026-09-14T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# evaluate_heuristics / condition matching
# ---------------------------------------------------------------------------


def test_list_membership_condition_matches_on_overlap():
    h = _heuristic(condition={"trigger_reasons": ["momentum_breakout", "volume_spike"]})
    factors = {"trigger_reasons": ["momentum_breakout"]}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]
    assert score == pytest.approx(0.15)


def test_list_membership_condition_does_not_match_without_overlap():
    h = _heuristic(condition={"trigger_reasons": ["momentum_breakout"]})
    factors = {"trigger_reasons": ["volume_spike"]}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []
    assert score == pytest.approx(0.0)


def test_list_membership_condition_matches_scalar_factor_value():
    h = _heuristic(condition={"instrument_class": ["majors", "midcaps"]})
    factors = {"instrument_class": "majors"}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_empty_condition_list_never_matches():
    h = _heuristic(condition={"trigger_reasons": []})
    factors = {"trigger_reasons": ["momentum_breakout"]}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_numeric_max_condition_matches_at_or_below_bound():
    h = _heuristic(condition={"candidate_score_max": 0.1})
    factors = {"candidate_score": 0.1}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_numeric_max_condition_does_not_match_above_bound():
    h = _heuristic(condition={"candidate_score_max": 0.1})
    factors = {"candidate_score": 0.2}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_numeric_min_condition_matches_at_or_above_bound():
    h = _heuristic(condition={"decay_score_min": 0.5})
    factors = {"decay_score": 0.5}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_numeric_min_condition_does_not_match_below_bound():
    h = _heuristic(condition={"decay_score_min": 0.5})
    factors = {"decay_score": 0.4}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_scalar_equality_condition_matches():
    h = _heuristic(condition={"guardian_state": "PROTECT"})
    factors = {"guardian_state": "PROTECT"}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_scalar_equality_condition_does_not_match():
    h = _heuristic(condition={"guardian_state": "PROTECT"})
    factors = {"guardian_state": "HOLD"}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_missing_factor_key_fails_closed():
    h = _heuristic(condition={"candidate_score_max": 0.1})
    factors = {}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_multi_key_condition_requires_all_keys_satisfied():
    h = _heuristic(
        condition={"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}
    )
    # trigger_reasons matches, candidate_score_max does not
    factors = {"trigger_reasons": ["momentum_breakout"], "candidate_score": 0.5}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_multi_key_condition_matches_when_all_keys_satisfied():
    h = _heuristic(
        condition={"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}
    )
    factors = {"trigger_reasons": ["momentum_breakout"], "candidate_score": 0.05}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_multiple_matching_heuristics_sum_adjustments():
    h1 = _heuristic(heuristic_id="h-1", condition={}, adjustment=0.15)
    h2 = _heuristic(heuristic_id="h-2", condition={}, adjustment=0.30)
    factors = {}

    score, matched = evaluate_heuristics(factors, [h1, h2])

    assert score == pytest.approx(0.45)
    assert matched == ["h-1", "h-2"]


def test_non_matching_heuristics_are_excluded_from_score():
    h1 = _heuristic(heuristic_id="h-1", condition={}, adjustment=0.15)
    h2 = _heuristic(heuristic_id="h-2", condition={"guardian_state": "EXIT"}, adjustment=0.9)
    factors = {"guardian_state": "HOLD"}

    score, matched = evaluate_heuristics(factors, [h1, h2])

    assert score == pytest.approx(0.15)
    assert matched == ["h-1"]


def test_empty_condition_matches_universally():
    h = _heuristic(condition={})

    score, matched = evaluate_heuristics({"anything": "goes"}, [h])

    assert matched == ["h-1"]


# ---------------------------------------------------------------------------
# decide_pre_entry
# ---------------------------------------------------------------------------


def test_decide_pre_entry_approves_when_no_heuristics_match():
    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={"trigger_reasons": ["momentum_breakout"]},
        heuristics=[],
        veto_threshold=0.5,
    )

    assert decision == "APPROVE"
    assert direction == "neutral"
    assert confidence == pytest.approx(1.0)
    assert isinstance(text, str) and text


def test_decide_pre_entry_approves_at_exact_threshold():
    h = _heuristic(condition={}, adjustment=0.5, confidence=0.6)

    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={}, heuristics=[h], veto_threshold=0.5
    )

    assert decision == "APPROVE"


def test_decide_pre_entry_vetoes_when_score_exceeds_threshold():
    h = _heuristic(
        condition={"candidate_score_max": 0.1},
        adjustment=0.6,
        confidence=0.8,
        description="poor historical outcomes for low-score candidates",
    )

    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={"candidate_score": 0.05},
        heuristics=[h],
        veto_threshold=0.5,
    )

    assert decision == "PRE_ENTRY_VETO"
    assert direction == "unfavorable"
    assert confidence == pytest.approx(0.8)
    assert "poor historical outcomes" in text


def test_decide_pre_entry_confidence_is_weighted_by_adjustment_magnitude():
    h1 = _heuristic(heuristic_id="h-1", condition={}, adjustment=0.9, confidence=0.9)
    h2 = _heuristic(heuristic_id="h-2", condition={}, adjustment=0.1, confidence=0.1)
    # score = 1.0, exceeds threshold -> VETO
    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={}, heuristics=[h1, h2], veto_threshold=0.5
    )

    assert decision == "PRE_ENTRY_VETO"
    # weighted average: (0.9*0.9 + 0.1*0.1) / (0.9+0.1) = 0.82
    assert confidence == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# decide_open_position - NO_ACTION / TIGHTEN_SL / CLOSE_EARLY
# ---------------------------------------------------------------------------


def test_decide_open_position_no_action_when_score_below_all_thresholds():
    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={"decay_score": 0.1},
        guardian_state="HOLD",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "NO_ACTION"
    assert direction == "neutral"
    assert proposed_sl is None
    assert confidence == pytest.approx(1.0)


def test_decide_open_position_tightens_sl_when_score_between_thresholds():
    h = _heuristic(
        condition={"guardian_state": "WATCH"},
        adjustment=0.4,
        confidence=0.7,
        description="watch-state momentum loss",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "TIGHTEN_SL"
    assert direction == "favorable"
    assert proposed_sl is not None
    assert proposed_sl > Decimal("90")
    assert proposed_sl <= Decimal("100")
    assert confidence == pytest.approx(0.7)
    assert "watch-state momentum loss" in text


def test_decide_open_position_closes_early_when_score_exceeds_close_threshold():
    h = _heuristic(
        condition={"guardian_state": "EXIT"},
        adjustment=0.9,
        confidence=0.85,
        description="exit-state deep decay",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="EXIT",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "CLOSE_EARLY"
    assert direction == "unfavorable"
    assert proposed_sl is None
    assert confidence == pytest.approx(0.85)


def test_decide_open_position_close_takes_precedence_over_tighten():
    # score exceeds BOTH thresholds -> must resolve to CLOSE_EARLY, not TIGHTEN_SL
    h = _heuristic(condition={}, adjustment=0.95, confidence=0.9)

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="EXIT",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "CLOSE_EARLY"
    assert proposed_sl is None


# ---------------------------------------------------------------------------
# THE CRITICAL SAFETY TEST
# ---------------------------------------------------------------------------


def test_decide_open_position_never_returns_invalid_tighten_sl():
    """Adversarial fixture: heuristics score comfortably above tighten_threshold
    (so the decision logic WOULD tighten), but current_sl is already at/above
    entry (e.g. Profit Protection already moved it to break-even or beyond),
    so this module's own internal proposed-SL computation has no room to
    move current_sl any closer to entry. The function must catch this
    itself and downgrade to NO_ACTION - it must NEVER return TIGHTEN_SL
    paired with a proposed_new_sl that is not strictly greater than
    current_sl.
    """
    h = _heuristic(
        condition={"guardian_state": "WATCH"},
        adjustment=0.5,
        confidence=0.7,
        description="would normally trigger a tighten",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("105"),  # already AT/ABOVE entry - no room to tighten toward entry
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.9,
    )

    assert decision == "NO_ACTION"
    assert proposed_sl is None
    # Never, under any circumstance, TIGHTEN_SL with a non-strictly-greater SL.
    assert not (decision == "TIGHTEN_SL" and (proposed_sl is None or proposed_sl <= Decimal("105")))


def test_decide_open_position_never_returns_invalid_tighten_sl_when_current_sl_equals_entry():
    """Same adversarial class, boundary case: current_sl == entry exactly
    (zero gap) rather than current_sl > entry."""
    h = _heuristic(condition={}, adjustment=0.5, confidence=0.7)

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("100"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.9,
    )

    assert decision == "NO_ACTION"
    assert proposed_sl is None


def test_decide_open_position_invalid_tighten_downgrade_is_observable_not_identical_to_no_signal():
    """The downgrade path should still be observable (text mentions why),
    not silently identical to a genuine no-signal NO_ACTION - useful for
    debugging/monitoring, and proves the downgrade branch actually ran
    rather than the score simply never having crossed the threshold."""
    h = _heuristic(
        condition={},
        adjustment=0.5,
        confidence=0.7,
        description="would normally trigger a tighten",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("105"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.9,
    )

    assert decision == "NO_ACTION"
    assert "would normally trigger a tighten" in text
    assert confidence == pytest.approx(0.7)


def test_decide_open_position_valid_tighten_never_exceeds_entry():
    h = _heuristic(condition={}, adjustment=100.0, confidence=0.5)  # extreme score

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=1000.0,  # keep it below close threshold
    )

    assert decision == "TIGHTEN_SL"
    assert proposed_sl > Decimal("90")
    assert proposed_sl <= Decimal("100")
