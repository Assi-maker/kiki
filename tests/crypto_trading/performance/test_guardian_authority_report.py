from datetime import UTC, datetime

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
    assert "calibration_rate" not in report["decision_types"]["PRE_ENTRY_VETO"]
    assert "calibration_note" in report["decision_types"]["CLOSE_EARLY"]
    assert "calibration_rate" not in report["decision_types"]["CLOSE_EARLY"]


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
        old_sl="49000", new_sl="49500",
    )
    repo.resolve_guardian_authority_decision(
        "tighten-1", "stop_loss", "120", True, _NOW,
    )
    repo.save_guardian_authority_decision(
        "tighten-2", "pos-2", "cand-4", "TIGHTEN_SL", _NOW,
        "decay", "expect small favorable move", "favorable", 0.65, "run-1",
        old_sl="48000", new_sl="48500",
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
    assert "calibration_rate" not in veto

    tighten = by_type["TIGHTEN_SL"]
    assert tighten["n_total"] == 3
    assert tighten["n_pending"] == 1
    assert tighten["n_resolved"] == 2
    assert tighten["n_scored"] == 2
    assert tighten["n_correct"] == 1
    assert tighten["calibration_rate"] == 1 / 2
    assert "calibration_note" not in tighten

    close_early = by_type["CLOSE_EARLY"]
    assert close_early["n_total"] == 2
    assert close_early["n_pending"] == 1
    assert close_early["n_resolved"] == 1
    assert "calibration_note" in close_early
    assert "calibration_rate" not in close_early


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
