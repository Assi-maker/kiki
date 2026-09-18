"""Tests for the TAKE_PROFIT decision type added to Guardian Authority
alongside the existing, LIVE TIGHTEN_SL/CLOSE_EARLY decision types (see
guardian/authority.py::decide_take_profit's own docstring for the full
design rationale).

Mirrors test_authority.py's own conventions for decide_pre_entry/
decide_open_position/resolve_pending_decisions exactly - `_heuristic` and
the position/decision seeding helpers below are lifted from that file
rather than re-invented, so behavior asserted here is directly comparable.

THE SAFETY PROPERTY THIS FILE MUST PROVE (see authority.py's Task 5 module
comment, "R3" section, for the full existing proof this extends): TAKE_
PROFIT's factor vocabulary ({"progress_ratio", "unrealized_pnl_positive"})
shares ZERO field names with either existing heuristic vocabulary
(TIGHTEN_SL/CLOSE_EARLY's decay-factor + guardian_state vocabulary, or
PRE_ENTRY_VETO's instrument/candidate_score/trigger_reasons vocabulary), so
a heuristic written for one family can never accidentally fire under
another family's factors dict - fail-closed missing-key matching is what
makes this true, and the cross-contamination tests below prove it
end-to-end rather than merely asserting it in prose.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.guardian.authority import (
    decide_open_position,
    decide_pre_entry,
    decide_take_profit,
    evaluate_heuristics,
    resolve_pending_decisions,
)
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


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
        "updated_at": "2026-09-20T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# decide_take_profit - pure function
# ---------------------------------------------------------------------------


def test_decide_take_profit_no_action_when_no_heuristics_match():
    decision, text, direction, confidence = decide_take_profit(
        profit_factors={"progress_ratio": 0.9, "unrealized_pnl_positive": True},
        heuristics=[],
        take_profit_threshold=0.3,
    )

    assert decision == "NO_ACTION"
    assert direction == "neutral"
    assert confidence == pytest.approx(1.0)
    assert isinstance(text, str) and text


def test_decide_take_profit_no_action_at_exact_threshold():
    h = _heuristic(condition={}, adjustment=0.3, confidence=0.6)

    decision, _text, _direction, _confidence = decide_take_profit(
        profit_factors={"progress_ratio": 0.9, "unrealized_pnl_positive": True},
        heuristics=[h],
        take_profit_threshold=0.3,
    )

    assert decision == "NO_ACTION"  # strictly exceed, not equal


def test_decide_take_profit_fires_when_score_exceeds_threshold():
    h = _heuristic(
        condition={"progress_ratio_min": 0.8},
        adjustment=0.4,
        confidence=0.7,
        description="deep into target with fading momentum",
    )

    decision, text, direction, confidence = decide_take_profit(
        profit_factors={"progress_ratio": 0.85, "unrealized_pnl_positive": True},
        heuristics=[h],
        take_profit_threshold=0.3,
    )

    assert decision == "TAKE_PROFIT"
    assert direction == "unfavorable"
    assert confidence == pytest.approx(0.7)
    assert "deep into target with fading momentum" in text


def test_decide_take_profit_fails_closed_on_missing_progress_ratio():
    h = _heuristic(condition={"progress_ratio_min": 0.1}, adjustment=0.9)

    decision, _text, _direction, _confidence = decide_take_profit(
        profit_factors={"unrealized_pnl_positive": True},  # progress_ratio missing
        heuristics=[h],
        take_profit_threshold=0.3,
    )

    assert decision == "NO_ACTION"


def test_decide_take_profit_confidence_is_weighted_by_adjustment_magnitude():
    h1 = _heuristic(heuristic_id="h-1", condition={}, adjustment=0.9, confidence=0.9)
    h2 = _heuristic(heuristic_id="h-2", condition={}, adjustment=0.1, confidence=0.1)

    decision, _text, _direction, confidence = decide_take_profit(
        profit_factors={"progress_ratio": 0.5, "unrealized_pnl_positive": True},
        heuristics=[h1, h2],
        take_profit_threshold=0.5,
    )

    assert decision == "TAKE_PROFIT"  # score = 1.0
    assert confidence == pytest.approx(0.82)  # (0.9*0.9 + 0.1*0.1) / 1.0


# ---------------------------------------------------------------------------
# THE cross-contamination regression tests (the safety property this whole
# addition rests on)
# ---------------------------------------------------------------------------


def test_a_tighten_sl_vocabulary_heuristic_never_fires_under_take_profit_factors():
    """A heuristic written for TIGHTEN_SL/CLOSE_EARLY (guardian_state +
    decay-factor vocabulary) must NEVER contribute to a TAKE_PROFIT score -
    fail-closed on the missing guardian_state/decay-factor keys, which
    genuinely do not exist in a TAKE_PROFIT factors dict."""
    h = _heuristic(
        heuristic_id="h-tighten",
        condition={"guardian_state": "PROTECT", "momentum_decay_min": 0.5},
        adjustment=0.9,  # would trivially exceed any reasonable threshold if it matched
    )

    score, matched = evaluate_heuristics(
        {"progress_ratio": 0.95, "unrealized_pnl_positive": True}, [h]
    )

    assert matched == []
    assert score == pytest.approx(0.0)

    decision, _text, _direction, _confidence = decide_take_profit(
        profit_factors={"progress_ratio": 0.95, "unrealized_pnl_positive": True},
        heuristics=[h],
        take_profit_threshold=0.3,
    )
    assert decision == "NO_ACTION"


def test_a_pre_entry_veto_vocabulary_heuristic_never_fires_under_take_profit_factors():
    """Same proof for the OTHER existing family (instrument/candidate_score/
    trigger_reasons)."""
    h = _heuristic(
        heuristic_id="h-veto",
        condition={"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1},
        adjustment=0.9,
    )

    decision, _text, _direction, _confidence = decide_take_profit(
        profit_factors={"progress_ratio": 0.95, "unrealized_pnl_positive": True},
        heuristics=[h],
        take_profit_threshold=0.3,
    )
    assert decision == "NO_ACTION"


def test_a_take_profit_vocabulary_heuristic_never_fires_under_tighten_sl_or_pre_entry_factors():
    """The converse proof: a TAKE_PROFIT-shaped heuristic (progress_ratio/
    unrealized_pnl_positive) must never contribute to decide_open_position's
    TIGHTEN_SL/CLOSE_EARLY score, nor to decide_pre_entry's PRE_ENTRY_VETO
    score - fail-closed on the missing progress_ratio/unrealized_pnl_
    positive keys, which genuinely do not exist in either of those factors
    dicts."""
    h = _heuristic(
        heuristic_id="h-tp",
        condition={"progress_ratio_min": 0.1, "unrealized_pnl_positive": True},
        adjustment=0.9,
    )

    open_decision, _text, _direction, _confidence, proposed_sl = decide_open_position(
        position_factors={
            "time_decay": 0.9, "momentum_decay": 0.9, "volume_decay": 0.9,
            "funding_decay": 0.9, "secondary_confirmation_lost": 0.9, "market_regime": 0.9,
        },
        guardian_state="PROTECT",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.15,
        close_threshold=0.45,
    )
    assert open_decision == "NO_ACTION"
    assert proposed_sl is None

    pre_entry_decision, _text, _direction, _confidence = decide_pre_entry(
        candidate_evidence={"instrument": "BTCUSDT", "candidate_score": 0.05,
                             "trigger_reasons": ["momentum_breakout"]},
        heuristics=[h],
        veto_threshold=0.3,
    )
    assert pre_entry_decision == "APPROVE"


# ---------------------------------------------------------------------------
# resolve_pending_decisions - TAKE_PROFIT resolves like CLOSE_EARLY
# ---------------------------------------------------------------------------


def _open_position(repo, position_id, fill_entry="100", stop_loss="90", target="120", size="1000"):
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=fill_entry, simulated_fill_entry=fill_entry,
        stop_loss=stop_loss, target=target, size=size, fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POS_OPENED:{position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
            run_id="run-1", schema_version=1, payload={"instrument": position.instrument},
        ),
    )
    return position


def _close_position(repo, position_id, fill_exit, exit_reason="TAKE_PROFIT", fees="1", funding="0",
                     closed_at=None):
    closed_at = closed_at or (_NOW + timedelta(hours=1))
    ok = repo.close_position_with_event(
        position_id, Decimal(fill_exit), Decimal(fill_exit), exit_reason,
        Decimal(fees), Decimal(funding), closed_at,
        Event(
            event_id=f"POS_CLOSED:{position_id}", event_type="POSITION_CLOSED",
            aggregate_type="position", aggregate_id=position_id, occurred_at=closed_at,
            run_id="run-1", schema_version=1, payload={"exit_reason": exit_reason},
        ),
    )
    assert ok is True


def test_resolve_pending_decisions_take_profit_fills_actuals_but_leaves_expectation_unknown(
    tmp_path,
):
    """Exactly the same treatment as CLOSE_EARLY (see resolve_pending_
    decisions' own docstring): actual_exit_reason/actual_pnl_usdt are filled
    in from the real close, but expectation_correct stays None - the
    realized P/L of the close doesn't validly measure the counterfactual
    'continuing to hold would have gone unfavorably' actually predicts."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-tp-1")
    _close_position(
        repo, "pos-tp-1", fill_exit="110", exit_reason="TAKE_PROFIT", fees="2", funding="1"
    )
    closed_position = repo.get_position("pos-tp-1")
    expected_pnl = compute_pnl(closed_position)
    assert expected_pnl > 0  # sanity: a real, successfully-locked-in gain
    repo.save_guardian_authority_decision(
        "ga-tp-1", "pos-tp-1", "cand-tp-1", "TAKE_PROFIT", _NOW,
        "reasoning", "expect unfavorable if left open", "unfavorable", 0.8, "run-1",
    )

    count = resolve_pending_decisions(repo, _NOW + timedelta(hours=2))

    assert count == 1
    row = repo.get_guardian_authority_decision("ga-tp-1")
    assert row["outcome_status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "TAKE_PROFIT"
    assert row["actual_pnl_usdt"] == str(expected_pnl)
    assert row["expectation_correct"] is None


def test_resolve_pending_decisions_take_profit_never_falls_back_to_naive_sign_comparison(tmp_path):
    """Regression proof for the exact bug this special-case exists to
    prevent: WITHOUT the TAKE_PROFIT branch, the naive fallback would
    compute predicted_favorable=False (direction='unfavorable') and
    actual_favorable=True (a real, positive locked-in gain), scoring
    expectation_correct=False for every SUCCESSFUL take-profit - the
    opposite of a useful metric. This test proves that never happens:
    expectation_correct must be None, not False."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-tp-2")
    _close_position(repo, "pos-tp-2", fill_exit="115", exit_reason="TAKE_PROFIT")
    repo.save_guardian_authority_decision(
        "ga-tp-2", "pos-tp-2", "cand-tp-2", "TAKE_PROFIT", _NOW,
        "reasoning", "expect unfavorable if left open", "unfavorable", 0.8, "run-1",
    )

    resolve_pending_decisions(repo, _NOW + timedelta(hours=2))

    row = repo.get_guardian_authority_decision("ga-tp-2")
    assert row["expectation_correct"] is None
    assert row["expectation_correct"] != 0
