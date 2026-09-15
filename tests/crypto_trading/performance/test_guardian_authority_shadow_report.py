from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_trading.performance.guardian_authority_shadow_report import build_report
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

_TICK_SHADOW_DECISION_TYPES = ("NO_ACTION", "TIGHTEN_SL", "CLOSE_EARLY")
_PRE_ENTRY_SHADOW_DECISION_TYPES = ("APPROVE", "PRE_ENTRY_VETO")


def _seed(repo, shadow_id="pos-1", position_id=None, candidate_id="cand-1", instrument="BTCUSDT"):
    repo.seed_guardian_authority_shadow(
        shadow_id=shadow_id,
        position_id=position_id or shadow_id,
        candidate_id=candidate_id,
        instrument=instrument,
        opened_at=_NOW,
        created_at=_NOW,
        run_id="run-1",
    )


def _decide(repo, shadow_id, decision, confidence=0.7, expected_direction="favorable"):
    repo.decide_guardian_authority_shadow(
        shadow_id=shadow_id,
        decision=decision,
        decided_at=_NOW,
        expected_outcome="expect small favorable move",
        expected_direction=expected_direction,
        confidence=confidence,
        factors_json="{}",
        proposed_new_sl=Decimal("49500") if decision == "TIGHTEN_SL" else None,
        updated_at=_NOW,
    )


def _resolve_no_action(repo, shadow_id, pnl="10"):
    repo.resolve_guardian_authority_shadow_no_action(
        shadow_id=shadow_id,
        factors_json="{}",
        actual_exit_reason="target",
        actual_pnl_usdt=Decimal(pnl),
        actual_closed_at=_NOW,
        updated_at=_NOW,
    )


def _resolve_decided(repo, shadow_id, pnl, expectation_correct, prediction_error=None):
    repo.resolve_guardian_authority_shadow_decided(
        shadow_id=shadow_id,
        actual_exit_reason="stop_loss",
        actual_pnl_usdt=Decimal(pnl),
        actual_closed_at=_NOW,
        expectation_correct=expectation_correct,
        prediction_error=prediction_error,
        updated_at=_NOW,
    )


def _save_pre_entry(repo, shadow_id, decision, confidence=0.8):
    repo.save_guardian_authority_pre_entry_shadow(
        shadow_id=shadow_id,
        candidate_id=shadow_id,
        instrument="BTCUSDT",
        shadow_decision=decision,
        expected_outcome="expect favorable entry",
        expected_direction="favorable",
        confidence=confidence,
        factors_json="{}",
        run_id="run-1",
        created_at=_NOW,
    )


def _abandon(repo, shadow_id):
    repo.abandon_guardian_authority_shadow(shadow_id, _NOW)


def _resolve_pre_entry(repo, shadow_id, pnl="10"):
    repo.resolve_guardian_authority_pre_entry_shadow(
        shadow_id=shadow_id,
        actual_exit_reason="target",
        actual_pnl_usdt=Decimal(pnl),
        actual_closed_at=_NOW,
        updated_at=_NOW,
    )


def test_empty_db_produces_valid_report_with_zero_counts_and_notes(tmp_path):
    """No shadow rows, no shadow heuristics at all - must not crash, every
    count is 0, and every tick-time/pre-entry type carries an explanatory
    calibration_note (never a bare 0%/null with no explanation)."""
    repo = SQLiteRepository(tmp_path / "t.db")

    report = build_report(repo)

    assert report["active_shadow_heuristics_count"] == 0
    assert "generated_at" in report

    for decision_type in _TICK_SHADOW_DECISION_TYPES:
        entry = report["tick_time_shadow"][decision_type]
        assert entry["n_total"] == 0
        assert entry["n_pending"] == 0
        assert entry["n_resolved"] == 0
        assert "calibration_note" in entry
        assert "win_rate" not in entry
        assert "brier_score" not in entry

    for decision_type in _PRE_ENTRY_SHADOW_DECISION_TYPES:
        entry = report["pre_entry_shadow"][decision_type]
        assert entry["n_total"] == 0
        assert entry["n_pending"] == 0
        assert entry["n_resolved"] == 0
        assert "calibration_note" in entry
        assert "win_rate" not in entry
        assert "brier_score" not in entry


def test_tick_time_shadow_mixed_counts_and_tighten_sl_calibration(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    # NO_ACTION: one still-observing row (hypothetically no-action so far,
    # not yet decided otherwise), one resolved-as-NO_ACTION row (position
    # closed while the shadow was still OBSERVING).
    _seed(repo, shadow_id="pos-1")
    _seed(repo, shadow_id="pos-2")
    _resolve_no_action(repo, "pos-2")

    # TIGHTEN_SL: one pending (DECIDED, position not yet closed), two
    # resolved (one correct, one incorrect) - same hand-verified numbers
    # as guardian_authority_report.py's own precedent test.
    _seed(repo, shadow_id="pos-3")
    _decide(repo, "pos-3", "TIGHTEN_SL", confidence=0.6)

    _seed(repo, shadow_id="pos-4")
    _decide(repo, "pos-4", "TIGHTEN_SL", confidence=0.7)
    _resolve_decided(repo, "pos-4", "120", True, prediction_error=(0.7 - 1.0) ** 2)

    _seed(repo, shadow_id="pos-5")
    _decide(repo, "pos-5", "TIGHTEN_SL", confidence=0.65)
    _resolve_decided(repo, "pos-5", "-40", False, prediction_error=(0.65 - 0.0) ** 2)

    # CLOSE_EARLY: one pending (DECIDED), one resolved with
    # expectation_correct always None (no counterfactual-of-inaction is
    # scored for this type, same as the real Guardian Authority's own
    # CLOSE_EARLY ruling).
    _seed(repo, shadow_id="pos-6")
    _decide(repo, "pos-6", "CLOSE_EARLY", confidence=0.8, expected_direction="unfavorable")

    _seed(repo, shadow_id="pos-7")
    _decide(repo, "pos-7", "CLOSE_EARLY", confidence=0.75, expected_direction="unfavorable")
    _resolve_decided(repo, "pos-7", "10", None, prediction_error=None)

    report = build_report(repo)
    by_type = report["tick_time_shadow"]

    no_action = by_type["NO_ACTION"]
    assert no_action["n_total"] == 2
    assert no_action["n_pending"] == 1
    assert no_action["n_resolved"] == 1
    assert "calibration_note" in no_action
    assert "win_rate" not in no_action

    tighten = by_type["TIGHTEN_SL"]
    assert tighten["n_total"] == 3
    assert tighten["n_pending"] == 1
    assert tighten["n_resolved"] == 2
    assert tighten["n_scored"] == 2
    assert tighten["n_correct"] == 1
    assert tighten["win_rate"] == 1 / 2
    # Hand-computed: pos-4 confidence=0.7, expectation_correct=True ->
    # (0.7 - 1.0) ** 2 = 0.09. pos-5 confidence=0.65,
    # expectation_correct=False -> (0.65 - 0.0) ** 2 = 0.4225.
    # mean = (0.09 + 0.4225) / 2 = 0.25625.
    assert tighten["brier_score"] == pytest.approx(0.25625)
    assert "calibration_note" not in tighten
    assert isinstance(tighten["brier_score_note"], str)
    note_lower = tighten["brier_score_note"].lower()
    assert "confidence" in note_lower
    assert "not a probability" in note_lower
    assert "signal-strength" in note_lower or "signal strength" in note_lower
    # pos-4 and pos-5 are two distinct positions among the 2 scored rows.
    assert tighten["n_distinct_positions"] == 2

    close_early = by_type["CLOSE_EARLY"]
    assert close_early["n_total"] == 2
    assert close_early["n_pending"] == 1
    assert close_early["n_resolved"] == 1
    assert "calibration_note" in close_early
    assert "win_rate" not in close_early
    assert "brier_score" not in close_early


def test_tighten_sl_no_scored_data_omits_brier_note_and_distinct_positions(tmp_path):
    """A pending (not-yet-resolved) TIGHTEN_SL row alone must not produce
    win_rate/brier_score/brier_score_note/n_distinct_positions - only the
    calibration_note fallback, mirroring guardian_authority_report.py's
    own precedent test for this same empty-`scored`-list branch."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, shadow_id="pos-1")
    _decide(repo, "pos-1", "TIGHTEN_SL")

    report = build_report(repo)
    tighten = report["tick_time_shadow"]["TIGHTEN_SL"]

    assert tighten["n_pending"] == 1
    assert tighten["n_resolved"] == 0
    assert "calibration_note" in tighten
    assert "brier_score_note" not in tighten
    assert "n_distinct_positions" not in tighten
    assert "win_rate" not in tighten


def test_tighten_sl_n_distinct_positions_can_be_less_than_n_scored(tmp_path):
    """Task 4's design guarantees one shadow row per position (shadow_id =
    position_id, 1:1) - so within a single build_report call,
    n_distinct_positions cannot exceed n_scored, but different positions
    can still repeat the SAME instrument. This test just confirms the
    counting is a real set-based aggregation, not a hardcoded pass-
    through of n_scored."""
    repo = SQLiteRepository(tmp_path / "t.db")

    for i in range(3):
        shadow_id = f"pos-{i}"
        _seed(repo, shadow_id=shadow_id)
        _decide(repo, shadow_id, "TIGHTEN_SL", confidence=0.7)
        _resolve_decided(repo, shadow_id, "120", True, prediction_error=(0.7 - 1.0) ** 2)

    report = build_report(repo)
    tighten = report["tick_time_shadow"]["TIGHTEN_SL"]

    assert tighten["n_scored"] == 3
    assert tighten["n_distinct_positions"] == 3


def test_pre_entry_shadow_counts_no_win_rate_or_brier(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    # APPROVE: 2 total - 1 pending, 1 resolved.
    _save_pre_entry(repo, "cand-1", "APPROVE")
    _save_pre_entry(repo, "cand-2", "APPROVE")
    _resolve_pre_entry(repo, "cand-2")

    # PRE_ENTRY_VETO: 1 total, always pending (a vetoed candidate never
    # opens a real position, so nothing ever resolves it).
    _save_pre_entry(repo, "cand-3", "PRE_ENTRY_VETO")

    report = build_report(repo)
    by_type = report["pre_entry_shadow"]

    approve = by_type["APPROVE"]
    assert approve["n_total"] == 2
    assert approve["n_pending"] == 1
    assert approve["n_resolved"] == 1
    assert "calibration_note" in approve
    assert "win_rate" not in approve
    assert "brier_score" not in approve

    veto = by_type["PRE_ENTRY_VETO"]
    assert veto["n_total"] == 1
    assert veto["n_pending"] == 1
    assert veto["n_resolved"] == 0
    assert "calibration_note" in veto
    assert "win_rate" not in veto
    assert "brier_score" not in veto


def test_abandoned_shadows_counted_separately_not_silently_dropped(tmp_path):
    """ABANDONED is a fourth, real status on this table (set by
    `abandon_guardian_authority_shadow` when a shadow's real position
    vanishes from `open_positions` mid-observation - see
    `guardian_authority_shadow.py::run_guardian_authority_shadow_tick`'s
    stranded-shadow handling). It must never be silently excluded from
    every count in this report the way an earlier version of this module
    did. A row abandoned while still OBSERVING never reached a decision
    (shadow_decision stays NULL) and is counted under NO_ACTION's own
    n_abandoned; a row abandoned after DECIDED carries a real
    shadow_decision and is counted under that same type's n_abandoned -
    same mapping the pending/resolved buckets already use."""
    repo = SQLiteRepository(tmp_path / "t.db")

    # Abandoned while still OBSERVING (no decision ever registered).
    _seed(repo, shadow_id="pos-1")
    _abandon(repo, "pos-1")

    # Abandoned after being DECIDED as TIGHTEN_SL (position vanished
    # before it could ever resolve).
    _seed(repo, shadow_id="pos-2")
    _decide(repo, "pos-2", "TIGHTEN_SL")
    _abandon(repo, "pos-2")

    # Abandoned after being DECIDED as CLOSE_EARLY.
    _seed(repo, shadow_id="pos-3")
    _decide(repo, "pos-3", "CLOSE_EARLY", expected_direction="unfavorable")
    _abandon(repo, "pos-3")

    report = build_report(repo)
    by_type = report["tick_time_shadow"]

    assert by_type["NO_ACTION"]["n_abandoned"] == 1
    assert by_type["TIGHTEN_SL"]["n_abandoned"] == 1
    assert by_type["CLOSE_EARLY"]["n_abandoned"] == 1

    # Abandoned rows are never a real pending or resolved outcome - they
    # must not leak into n_total/n_pending/n_resolved.
    assert by_type["NO_ACTION"]["n_total"] == 0
    assert by_type["NO_ACTION"]["n_pending"] == 0
    assert by_type["NO_ACTION"]["n_resolved"] == 0
    assert by_type["TIGHTEN_SL"]["n_total"] == 0
    assert by_type["CLOSE_EARLY"]["n_total"] == 0


def test_active_shadow_heuristics_count(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.upsert_guardian_authority_shadow_heuristic(
        "h1", "widen SL on high-vol candidates (shadow)", "{}", 0.1, 0.5, 10, _NOW,
    )
    repo.upsert_guardian_authority_shadow_heuristic(
        "h2", "tighten SL on decaying momentum (shadow)", "{}", -0.1, 0.6, 5, _NOW,
    )

    report = build_report(repo)

    assert report["active_shadow_heuristics_count"] == 2
