from datetime import UTC, datetime

import pytest

from crypto_trading.performance.guardian_authority_report import build_report
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

_DECISION_TYPES = ("PRE_ENTRY_VETO", "TIGHTEN_SL", "CLOSE_EARLY")


def test_empty_db_produces_valid_report_with_zero_counts_and_notes(tmp_path):
    """No decisions, no heuristics at all - must not crash, every count is
    0, and PRE_ENTRY_VETO/CLOSE_EARLY carry an explanatory calibration_note
    (never a bare 0%/null with no explanation)."""
    repo = SQLiteRepository(tmp_path / "t.db")

    report = build_report(repo)

    assert report["active_heuristics_count"] == 0
    assert "generated_at" in report
    for decision_type in _DECISION_TYPES:
        entry = report["decision_types"][decision_type]
        assert entry["n_total"] == 0
        assert entry["n_pending"] == 0
        assert entry["n_resolved"] == 0
    assert "calibration_note" in report["decision_types"]["PRE_ENTRY_VETO"]
    assert "win_rate" not in report["decision_types"]["PRE_ENTRY_VETO"]
    assert "brier_score" not in report["decision_types"]["PRE_ENTRY_VETO"]
    assert "calibration_note" in report["decision_types"]["CLOSE_EARLY"]
    assert "win_rate" not in report["decision_types"]["CLOSE_EARLY"]
    assert "brier_score" not in report["decision_types"]["CLOSE_EARLY"]


def test_mixed_decisions_produce_correct_counts_and_calibration_only_for_tighten_sl(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    # PRE_ENTRY_VETO: 2 decisions, always PENDING forever (never resolves).
    repo.save_guardian_authority_decision(
        "veto-1", None, "cand-1", "PRE_ENTRY_VETO", _NOW,
        "reasoning", "expect unfavorable if entered", "unfavorable", 0.6, "run-1",
    )
    repo.save_guardian_authority_decision(
        "veto-2", None, "cand-2", "PRE_ENTRY_VETO", _NOW,
        "reasoning", "expect unfavorable if entered", "unfavorable", 0.7, "run-1",
    )

    # TIGHTEN_SL: 3 decisions - 2 resolved (one correct, one incorrect), 1 still pending.
    repo.save_guardian_authority_decision(
        "tighten-1", "pos-1", "cand-3", "TIGHTEN_SL", _NOW,
        "decay", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500", intervention_applied=True,
    )
    repo.resolve_guardian_authority_decision(
        "tighten-1", "stop_loss", "120", True, _NOW,
    )
    repo.save_guardian_authority_decision(
        "tighten-2", "pos-2", "cand-4", "TIGHTEN_SL", _NOW,
        "decay", "expect small favorable move", "favorable", 0.65, "run-1",
        old_sl="48000", new_sl="48500", intervention_applied=True,
    )
    repo.resolve_guardian_authority_decision(
        "tighten-2", "stop_loss", "-40", False, _NOW,
    )
    repo.save_guardian_authority_decision(
        "tighten-3", "pos-3", "cand-5", "TIGHTEN_SL", _NOW,
        "decay", "expect small favorable move", "favorable", 0.55, "run-1",
        old_sl="47000", new_sl="47500",
    )

    # CLOSE_EARLY: 1 resolved with expectation_correct always None (Task 8's
    # own established reasoning: no counterfactual-of-inaction is scored),
    # 1 still pending.
    repo.save_guardian_authority_decision(
        "close-1", "pos-4", "cand-6", "CLOSE_EARLY", _NOW,
        "reasoning", "expect continuing to hold would go unfavorably", "unfavorable", 0.8, "run-1",
    )
    repo.resolve_guardian_authority_decision(
        "close-1", "manual_close", "10", None, _NOW,
    )
    repo.save_guardian_authority_decision(
        "close-2", "pos-5", "cand-7", "CLOSE_EARLY", _NOW,
        "reasoning", "expect continuing to hold would go unfavorably", "unfavorable", 0.75, "run-1",
    )

    report = build_report(repo)
    by_type = report["decision_types"]

    veto = by_type["PRE_ENTRY_VETO"]
    assert veto["n_total"] == 2
    assert veto["n_pending"] == 2
    assert veto["n_resolved"] == 0
    assert "calibration_note" in veto
    assert "win_rate" not in veto
    assert "brier_score" not in veto

    tighten = by_type["TIGHTEN_SL"]
    assert tighten["n_total"] == 3
    assert tighten["n_pending"] == 1
    assert tighten["n_resolved"] == 2
    assert tighten["n_scored"] == 2
    assert tighten["n_correct"] == 1
    assert tighten["win_rate"] == 1 / 2
    # Hand-computed: tighten-1 confidence=0.7, expectation_correct=True ->
    # (0.7 - 1.0) ** 2 = 0.09. tighten-2 confidence=0.65,
    # expectation_correct=False -> (0.65 - 0.0) ** 2 = 0.4225.
    # mean = (0.09 + 0.4225) / 2 = 0.25625.
    assert tighten["brier_score"] == pytest.approx(0.25625)
    assert "calibration_note" not in tighten
    # Final-review fix (2026-09-15), Important #3 (interpretability gap):
    # brier_score's forecast variable is confidence (a signal-strength
    # score, |2*correct_rate-1|), NOT a probability of a favorable outcome
    # - brier_score_note must say so plainly, since main() prints bare JSON
    # with no other documentation attached at call time.
    assert isinstance(tighten["brier_score_note"], str)
    note_lower = tighten["brier_score_note"].lower()
    assert "confidence" in note_lower
    assert "not a probability" in note_lower or "not the probability" in note_lower
    assert "signal-strength" in note_lower or "signal strength" in note_lower
    # Final-review fix (2026-09-15), Important #2 (design gap made visible,
    # not fixed): n_distinct_positions, a pure read-only aggregation over
    # the SAME already-filtered `scored` population n_scored/win_rate/
    # brier_score use. tighten-1 is pos-1, tighten-2 is pos-2 -> 2 distinct
    # positions among the 2 scored rows.
    assert tighten["n_distinct_positions"] == 2

    close_early = by_type["CLOSE_EARLY"]
    assert close_early["n_total"] == 2
    assert close_early["n_pending"] == 1
    assert close_early["n_resolved"] == 1
    assert "calibration_note" in close_early
    assert "win_rate" not in close_early
    assert "brier_score" not in close_early


def test_tighten_sl_without_genuine_intervention_excluded_from_win_rate_and_brier(tmp_path):
    """A resolved, scored (non-null expectation_correct) TIGHTEN_SL decision
    that was never a genuine applied intervention (intervention_applied is
    False or still None - e.g. the tighten write itself failed or was never
    attempted) must NOT count toward n_scored/win_rate/brier_score. An
    unfiltered computation over this fixture would show win_rate=1.0 (the
    lone resolved+scored row is "correct") and brier_score=0.0 (perfect
    calibration) - the filtered computation must instead fall back to the
    calibration_note branch, proving intervention_applied actually changes
    this report's output, not just I2's own already-reviewed authority.py
    test."""
    repo = SQLiteRepository(tmp_path / "t.db")

    # intervention_applied left at its default (None) - never actually
    # applied to the live stop-loss, e.g. the write attempt failed.
    repo.save_guardian_authority_decision(
        "tighten-unapplied", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "decay", "expect small favorable move", "favorable", 0.9, "run-1",
        old_sl="49000", new_sl="49500",
    )
    repo.resolve_guardian_authority_decision(
        "tighten-unapplied", "stop_loss", "120", True, _NOW,
    )

    report = build_report(repo)
    tighten = report["decision_types"]["TIGHTEN_SL"]

    assert tighten["n_total"] == 1
    assert tighten["n_resolved"] == 1
    assert "n_scored" not in tighten
    assert "win_rate" not in tighten
    assert "brier_score" not in tighten
    assert "calibration_note" in tighten


def test_tighten_sl_no_scored_data_omits_brier_note_and_distinct_positions(tmp_path):
    """When `scored` is empty (the calibration_note branch), neither
    brier_score_note nor n_distinct_positions should appear - both are
    computed only inside the same `if scored:` branch as brier_score/
    n_scored/win_rate themselves."""
    repo = SQLiteRepository(tmp_path / "t.db")

    report = build_report(repo)
    tighten = report["decision_types"]["TIGHTEN_SL"]

    assert "calibration_note" in tighten
    assert "brier_score_note" not in tighten
    assert "n_distinct_positions" not in tighten


def test_tighten_sl_n_distinct_positions_can_be_less_than_n_scored(tmp_path):
    """Final-review fix (2026-09-15), Important #2: makes visible (without
    changing any gating/threshold logic) that n_scored>=30-style sample
    counts can come from far fewer distinct positions - here, a single
    position (pos-1) supplies ALL 3 scored TIGHTEN_SL samples on its own,
    so n_scored=3 but n_distinct_positions=1."""
    repo = SQLiteRepository(tmp_path / "t.db")

    for i in range(3):
        decision_id = f"tighten-{i}"
        repo.save_guardian_authority_decision(
            decision_id, "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
            "decay", "expect small favorable move", "favorable", 0.7, "run-1",
            old_sl="49000", new_sl=str(49500 + i), intervention_applied=True,
        )
        repo.resolve_guardian_authority_decision(decision_id, "stop_loss", "120", True, _NOW)

    report = build_report(repo)
    tighten = report["decision_types"]["TIGHTEN_SL"]

    assert tighten["n_scored"] == 3
    assert tighten["n_distinct_positions"] == 1


def test_active_heuristics_count(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.upsert_guardian_authority_heuristic(
        "h1", "widen SL on high-vol candidates", "{}", 0.1, 0.5, 10, _NOW,
    )
    repo.upsert_guardian_authority_heuristic(
        "h2", "tighten SL on decaying momentum", "{}", -0.1, 0.6, 5, _NOW,
    )

    report = build_report(repo)

    assert report["active_heuristics_count"] == 2
