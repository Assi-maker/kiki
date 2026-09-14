from datetime import UTC, datetime, timedelta

from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def test_save_guardian_authority_decision_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    first = repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "decay accelerating", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )
    second = repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "decay accelerating", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )

    assert first is True
    assert second is False


def test_save_guardian_authority_decision_preserves_original_data_on_second_save(tmp_path):
    """Same non-overwrite proof style as
    test_repository_live_profit_protection.py: a second save with DIFFERENT
    values must not silently overwrite the original row - INSERT OR IGNORE
    alone can't prove that with identical arguments."""
    repo = SQLiteRepository(tmp_path / "t.db")

    first = repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "decay accelerating", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )
    second = repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "CLOSE_EARLY", _NOW + timedelta(seconds=5),
        "different reasoning", "expect unfavorable move", "unfavorable", 0.2, "run-2",
    )

    assert first is True
    assert second is False
    row = repo.get_guardian_authority_decision("ga-1")
    assert row["decision_type"] == "TIGHTEN_SL"
    assert row["reasoning"] == "decay accelerating"
    assert row["expected_outcome"] == "expect small favorable move"
    assert row["expected_direction"] == "favorable"
    assert row["confidence"] == 0.7
    assert row["run_id"] == "run-1"
    assert row["old_sl"] == "49000"
    assert row["new_sl"] == "49500"
    assert row["decided_at"] == _NOW.isoformat()


def test_get_guardian_authority_decision_returns_none_before_save(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    assert repo.get_guardian_authority_decision("ga-1") is None


def test_get_guardian_authority_decision_returns_full_row_with_pending_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "decay accelerating", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )

    row = repo.get_guardian_authority_decision("ga-1")
    assert row["decision_id"] == "ga-1"
    assert row["position_id"] == "pos-1"
    assert row["candidate_id"] == "cand-1"
    assert row["decision_type"] == "TIGHTEN_SL"
    assert row["decided_at"] == _NOW.isoformat()
    assert row["reasoning"] == "decay accelerating"
    assert row["expected_outcome"] == "expect small favorable move"
    assert row["expected_direction"] == "favorable"
    assert row["confidence"] == 0.7
    assert row["outcome_status"] == "PENDING"
    assert row["actual_exit_reason"] is None
    assert row["actual_pnl_usdt"] is None
    assert row["expectation_correct"] is None
    assert row["resolved_at"] is None
    assert row["old_sl"] == "49000"
    assert row["new_sl"] == "49500"
    assert row["run_id"] == "run-1"


def test_save_guardian_authority_decision_allows_null_position_id_for_pre_entry_veto(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    saved = repo.save_guardian_authority_decision(
        "ga-veto-1", None, "cand-1", "PRE_ENTRY_VETO", _NOW,
        "poor historical pattern match", "expect unfavorable if opened", "unfavorable",
        0.8, "run-1",
    )

    assert saved is True
    row = repo.get_guardian_authority_decision("ga-veto-1")
    assert row["position_id"] is None
    assert row["decision_type"] == "PRE_ENTRY_VETO"
    assert row["old_sl"] is None
    assert row["new_sl"] is None


def test_find_pending_guardian_authority_decisions_returns_only_pending_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "reasoning-1", "expect favorable", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )
    repo.save_guardian_authority_decision(
        "ga-2", "pos-2", "cand-2", "CLOSE_EARLY", _NOW,
        "reasoning-2", "expect unfavorable if left open", "unfavorable", 0.9, "run-1",
    )
    repo.resolve_guardian_authority_decision(
        "ga-2", "GUARDIAN_EXIT", "-5.00", True, _NOW + timedelta(minutes=10)
    )

    pending = repo.find_pending_guardian_authority_decisions()

    assert [row["decision_id"] for row in pending] == ["ga-1"]
    assert pending[0]["outcome_status"] == "PENDING"


def test_resolve_guardian_authority_decision_sets_actual_outcome_and_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "CLOSE_EARLY", _NOW,
        "reasoning-1", "expect unfavorable if left open", "unfavorable", 0.9, "run-1",
    )

    repo.resolve_guardian_authority_decision(
        "ga-1", "GUARDIAN_EXIT", "-3.50", True, _NOW + timedelta(minutes=10)
    )

    row = repo.get_guardian_authority_decision("ga-1")
    assert row["outcome_status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "GUARDIAN_EXIT"
    assert row["actual_pnl_usdt"] == "-3.50"
    assert row["expectation_correct"] == 1
    assert row["resolved_at"] == (_NOW + timedelta(minutes=10)).isoformat()


def test_resolve_guardian_authority_decision_never_mutates_the_pre_decision_expectation(tmp_path):
    """The single most important test in this task (per the task brief):
    the expectation fields recorded at save_...() time are the record of
    what Guardian Authority believed BEFORE the outcome was known - spec
    requirement 10 requires them to be immutable once written, so later
    self-critique compares "was I right" honestly, never with hindsight
    bias. Prove it by asserting byte-identical values before and after
    resolution, not just "resolution doesn't crash"."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "decay accelerating on weak volume", "expect small favorable move",
        "favorable", 0.65, "run-1", old_sl="49000", new_sl="49500",
    )
    before = repo.get_guardian_authority_decision("ga-1")

    repo.resolve_guardian_authority_decision(
        "ga-1", "STOP_LOSS_HIT", "-1.20", False, _NOW + timedelta(minutes=30)
    )

    after = repo.get_guardian_authority_decision("ga-1")
    assert after["expected_outcome"] == before["expected_outcome"] == "expect small favorable move"
    assert after["expected_direction"] == before["expected_direction"] == "favorable"
    assert after["confidence"] == before["confidence"] == 0.65
    assert after["decided_at"] == before["decided_at"] == _NOW.isoformat()
    assert after["reasoning"] == before["reasoning"] == "decay accelerating on weak volume"
    # Only the actual-outcome fields and status change:
    assert after["outcome_status"] == "RESOLVED"
    assert after["actual_exit_reason"] == "STOP_LOSS_HIT"
    assert after["actual_pnl_usdt"] == "-1.20"
    assert after["expectation_correct"] == 0
    assert after["resolved_at"] == (_NOW + timedelta(minutes=30)).isoformat()
