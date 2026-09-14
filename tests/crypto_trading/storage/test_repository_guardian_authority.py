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


def test_save_guardian_authority_decision_intervention_applied_true_round_trips(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-ia-1", None, "cand-1", "PRE_ENTRY_VETO", _NOW,
        "reasoning", "expect unfavorable", "unfavorable", 0.8, "run-1",
        intervention_applied=True,
    )

    row = repo.get_guardian_authority_decision("ga-ia-1")
    assert row["intervention_applied"] == 1


def test_save_guardian_authority_decision_intervention_applied_none_round_trips(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-ia-2", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "reasoning", "expect favorable", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500", intervention_applied=None,
    )

    row = repo.get_guardian_authority_decision("ga-ia-2")
    assert row["intervention_applied"] is None


def test_save_guardian_authority_decision_intervention_applied_defaults_to_none_when_not_passed(
    tmp_path,
):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-ia-3", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "reasoning", "expect favorable", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )

    row = repo.get_guardian_authority_decision("ga-ia-3")
    assert row["intervention_applied"] is None


def test_mark_guardian_authority_decision_intervention_applied_updates_only_that_column(tmp_path):
    """Same surgical-scope proof style as
    test_resolve_guardian_authority_decision_never_mutates_the_pre_decision_expectation:
    read the row before/after and confirm every other field - especially the
    requirement-10-protected ones (expected_outcome/expected_direction/
    confidence/decided_at) - is byte-identical, and only intervention_applied
    changed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-ia-4", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "decay accelerating", "expect small favorable move", "favorable", 0.65, "run-1",
        old_sl="49000", new_sl="49500",
    )
    before = repo.get_guardian_authority_decision("ga-ia-4")
    assert before["intervention_applied"] is None

    repo.mark_guardian_authority_decision_intervention_applied(
        "ga-ia-4", True, _NOW + timedelta(minutes=1)
    )

    after = repo.get_guardian_authority_decision("ga-ia-4")
    assert after["intervention_applied"] == 1
    assert after["expected_outcome"] == before["expected_outcome"] == "expect small favorable move"
    assert after["expected_direction"] == before["expected_direction"] == "favorable"
    assert after["confidence"] == before["confidence"] == 0.65
    assert after["decided_at"] == before["decided_at"] == _NOW.isoformat()
    assert after["reasoning"] == before["reasoning"] == "decay accelerating"
    assert after["decision_id"] == before["decision_id"] == "ga-ia-4"
    assert after["position_id"] == before["position_id"] == "pos-1"
    assert after["candidate_id"] == before["candidate_id"] == "cand-1"
    assert after["decision_type"] == before["decision_type"] == "TIGHTEN_SL"
    assert after["outcome_status"] == before["outcome_status"] == "PENDING"
    assert after["actual_exit_reason"] == before["actual_exit_reason"] is None
    assert after["actual_pnl_usdt"] == before["actual_pnl_usdt"] is None
    assert after["expectation_correct"] == before["expectation_correct"] is None
    assert after["resolved_at"] == before["resolved_at"] is None
    assert after["old_sl"] == before["old_sl"] == "49000"
    assert after["new_sl"] == before["new_sl"] == "49500"
    assert after["run_id"] == before["run_id"] == "run-1"


def test_mark_guardian_authority_decision_intervention_applied_false(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-ia-5", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "reasoning", "expect favorable", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500", intervention_applied=None,
    )

    repo.mark_guardian_authority_decision_intervention_applied(
        "ga-ia-5", False, _NOW + timedelta(minutes=1)
    )

    row = repo.get_guardian_authority_decision("ga-ia-5")
    assert row["intervention_applied"] == 0


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


def test_find_resolved_guardian_authority_decisions_returns_only_resolved_rows(tmp_path):
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

    resolved = repo.find_resolved_guardian_authority_decisions()

    assert [row["decision_id"] for row in resolved] == ["ga-2"]
    assert resolved[0]["outcome_status"] == "RESOLVED"


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


def test_resolve_guardian_authority_decision_accepts_none_expectation_correct(tmp_path):
    """Task 8 widening: expectation_correct is now typed `bool | None` (both
    in the Repository Protocol and SQLiteRepository) so the resolution pass
    can record CLOSE_EARLY rows whose expectation genuinely cannot be
    computed (see task-8-brief.md's controller ruling) without lying with a
    real boolean. Confirm None round-trips as None/SQL NULL on read-back -
    the existing real-boolean cases above (test_resolve_guardian_authority_
    decision_sets_actual_outcome_and_status and the immutability test) must
    still pass unmodified alongside this."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "CLOSE_EARLY", _NOW,
        "reasoning-1", "expect unfavorable if left open", "unfavorable", 0.9, "run-1",
    )

    repo.resolve_guardian_authority_decision(
        "ga-1", "GUARDIAN_EXIT", "1.50", None, _NOW + timedelta(minutes=10)
    )

    row = repo.get_guardian_authority_decision("ga-1")
    assert row["outcome_status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "GUARDIAN_EXIT"
    assert row["actual_pnl_usdt"] == "1.50"
    assert row["expectation_correct"] is None
    assert row["resolved_at"] == (_NOW + timedelta(minutes=10)).isoformat()


def test_upsert_guardian_authority_heuristic_creates_new_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    repo.upsert_guardian_authority_heuristic(
        "h-momentum-1",
        "Momentum breakout above 20-day band",
        '{"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}',
        0.05,
        0.75,
        42,
        _NOW,
    )

    heuristics = repo.find_guardian_authority_heuristics()
    assert len(heuristics) == 1
    assert heuristics[0]["heuristic_id"] == "h-momentum-1"
    assert heuristics[0]["description"] == "Momentum breakout above 20-day band"
    assert heuristics[0]["condition_json"] == '{"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}'
    assert heuristics[0]["adjustment"] == 0.05
    assert heuristics[0]["confidence"] == 0.75
    assert heuristics[0]["sample_size"] == 42
    assert heuristics[0]["updated_at"] == _NOW.isoformat()


def test_upsert_guardian_authority_heuristic_replaces_existing_row(tmp_path):
    """Task 2 critical distinction from Task 1: upsert uses INSERT OR REPLACE,
    not INSERT OR IGNORE. A second upsert with the same heuristic_id but
    DIFFERENT field values must overwrite the original row, not preserve it.
    This proves heuristics evolve and are meant to be refined by self-critique."""
    repo = SQLiteRepository(tmp_path / "t.db")

    # First upsert
    repo.upsert_guardian_authority_heuristic(
        "h-momentum-1",
        "Momentum breakout above 20-day band",
        '{"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}',
        0.05,
        0.75,
        42,
        _NOW,
    )

    # Second upsert with same heuristic_id but different values
    repo.upsert_guardian_authority_heuristic(
        "h-momentum-1",
        "Improved momentum rule after backtesting",
        '{"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.2}',
        0.08,
        0.82,
        127,
        _NOW + timedelta(hours=1),
    )

    # Must have exactly one row, with the NEW values (not the old ones)
    heuristics = repo.find_guardian_authority_heuristics()
    assert len(heuristics) == 1
    assert heuristics[0]["heuristic_id"] == "h-momentum-1"
    assert heuristics[0]["description"] == "Improved momentum rule after backtesting"
    assert heuristics[0]["condition_json"] == '{"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.2}'
    assert heuristics[0]["adjustment"] == 0.08
    assert heuristics[0]["confidence"] == 0.82
    assert heuristics[0]["sample_size"] == 127
    assert heuristics[0]["updated_at"] == (_NOW + timedelta(hours=1)).isoformat()


def test_find_guardian_authority_heuristics_returns_all_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    repo.upsert_guardian_authority_heuristic(
        "h-1", "Rule 1", '{"key": "value1"}', 0.01, 0.5, 10, _NOW
    )
    repo.upsert_guardian_authority_heuristic(
        "h-2", "Rule 2", '{"key": "value2"}', -0.02, 0.6, 20, _NOW + timedelta(minutes=1)
    )
    repo.upsert_guardian_authority_heuristic(
        "h-3", "Rule 3", '{"key": "value3"}', 0.03, 0.7, 30, _NOW + timedelta(minutes=2)
    )

    heuristics = repo.find_guardian_authority_heuristics()
    assert len(heuristics) == 3
    ids = {h["heuristic_id"] for h in heuristics}
    assert ids == {"h-1", "h-2", "h-3"}


def test_find_guardian_authority_heuristics_returns_empty_list_when_none_exist(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    heuristics = repo.find_guardian_authority_heuristics()
    assert heuristics == []


def test_tighten_position_stop_loss_strictly_higher_succeeds(tmp_path):
    """Task 4 AC1: A strictly-higher new stop_loss succeeds and updates the position."""
    from decimal import Decimal
    from crypto_trading.schemas.trade import Position
    from crypto_trading.schemas.event import Event

    repo = SQLiteRepository(tmp_path / "t.db")

    # Create a position with stop_loss = 49000
    position = Position(
        position_id="pos-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_NOW,
    )
    event = Event(
        event_id="POS_OPENED:pos-1",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id="pos-1",
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={"instrument": position.instrument},
    )
    repo.create_position_with_event(position, event)

    # Tighten to 49500 (strictly higher)
    result = repo.tighten_position_stop_loss("pos-1", Decimal("49500"), _NOW)

    assert result is True
    reloaded = repo.get_position("pos-1")
    assert reloaded.stop_loss == Decimal("49500")


def test_tighten_position_stop_loss_equal_or_lower_is_refused(tmp_path):
    """Task 4 AC2: An equal-or-lower new stop_loss is REFUSED (returns False, row unchanged)."""
    from decimal import Decimal
    from crypto_trading.schemas.trade import Position
    from crypto_trading.schemas.event import Event

    repo = SQLiteRepository(tmp_path / "t.db")

    # Create a position with stop_loss = 49000
    position = Position(
        position_id="pos-2",
        candidate_id="cand-2",
        instrument="ETHUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="2500",
        simulated_fill_entry="2510",
        stop_loss="49000",
        target="2600",
        size="1.0",
        fill_model_version="v1",
        opened_at=_NOW,
    )
    event = Event(
        event_id="POS_OPENED:pos-2",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id="pos-2",
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={"instrument": position.instrument},
    )
    repo.create_position_with_event(position, event)

    # Try to loosen to 48500 (strictly lower)
    result_lower = repo.tighten_position_stop_loss("pos-2", Decimal("48500"), _NOW)
    assert result_lower is False
    reloaded = repo.get_position("pos-2")
    assert reloaded.stop_loss == Decimal("49000")  # unchanged

    # Try to keep equal at 49000
    result_equal = repo.tighten_position_stop_loss("pos-2", Decimal("49000"), _NOW)
    assert result_equal is False
    reloaded = repo.get_position("pos-2")
    assert reloaded.stop_loss == Decimal("49000")  # unchanged


def test_tighten_position_stop_loss_nonexistent_position_returns_false_no_error(tmp_path):
    """Task 4 AC3: A non-existent position_id returns False, no error."""
    from decimal import Decimal

    repo = SQLiteRepository(tmp_path / "t.db")

    # Try to tighten a position that doesn't exist
    result = repo.tighten_position_stop_loss("does-not-exist", Decimal("49500"), _NOW)

    assert result is False


def test_tighten_position_stop_loss_digit_count_boundary_tighten_succeeds(tmp_path):
    """Final-review fix C1: a genuine tighten whose old/new values differ in
    digit count (crossing 9 -> 10) must succeed. Under the pre-fix lexicographic
    TEXT comparison, "10.10" < "9.87" as strings, so this tighten was
    incorrectly refused."""
    from decimal import Decimal
    from crypto_trading.schemas.trade import Position
    from crypto_trading.schemas.event import Event

    repo = SQLiteRepository(tmp_path / "t.db")

    position = Position(
        position_id="pos-digit-1",
        candidate_id="cand-digit-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="9.87",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_NOW,
    )
    event = Event(
        event_id="POS_OPENED:pos-digit-1",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id="pos-digit-1",
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={"instrument": position.instrument},
    )
    repo.create_position_with_event(position, event)

    # Tighten from 9.87 to 10.10 (numerically higher, more digits before the
    # decimal point - a genuine tighten that a lexicographic comparison gets
    # wrong).
    result = repo.tighten_position_stop_loss("pos-digit-1", Decimal("10.10"), _NOW)

    assert result is True
    reloaded = repo.get_position("pos-digit-1")
    assert reloaded.stop_loss == Decimal("10.10")


def test_tighten_position_stop_loss_digit_count_boundary_loosen_is_refused(tmp_path):
    """Final-review fix C1: a genuine loosening whose old/new values differ in
    digit count (crossing 100 -> 99) must be refused. Under the pre-fix
    lexicographic TEXT comparison, "99.00" > "100.00" as strings, so this
    loosening was incorrectly accepted."""
    from decimal import Decimal
    from crypto_trading.schemas.trade import Position
    from crypto_trading.schemas.event import Event

    repo = SQLiteRepository(tmp_path / "t.db")

    position = Position(
        position_id="pos-digit-2",
        candidate_id="cand-digit-2",
        instrument="ETHUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="2500",
        simulated_fill_entry="2510",
        stop_loss="100.00",
        target="2600",
        size="1.0",
        fill_model_version="v1",
        opened_at=_NOW,
    )
    event = Event(
        event_id="POS_OPENED:pos-digit-2",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id="pos-digit-2",
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={"instrument": position.instrument},
    )
    repo.create_position_with_event(position, event)

    # Try to loosen from 100.00 to 99.00 (numerically lower, fewer digits
    # before the decimal point - a genuine loosening a lexicographic
    # comparison gets wrong).
    result = repo.tighten_position_stop_loss("pos-digit-2", Decimal("99.00"), _NOW)

    assert result is False
    reloaded = repo.get_position("pos-digit-2")
    assert reloaded.stop_loss == Decimal("100.00")  # unchanged
