"""Tests for guardian_authority_shadow_observations (Task 1 of
docs/superpowers/plans/2026-09-15-guardian-authority-shadow.md), the
tick-time half of Guardian Authority's shadow/observation mode. Mirrors
test_repository_profit_protection.py's and
test_repository_guardian_authority.py's own repository test shapes - see
those files for the precedent this adapts (profit_protection_shadow_
positions for the shadow-table state-machine shape, guardian_authority_
decisions for the "some fields immutable once set" one-time-transition
discipline).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _seed_kwargs(**overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1", position_id="pos-1", candidate_id="cand-1",
        instrument="BTCUSDT", opened_at=_NOW, created_at=_NOW, run_id="run-1",
    )
    defaults.update(overrides)
    return defaults


def test_guardian_authority_shadow_observations_table_exists(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    columns = {
        row["name"]
        for row in repo._conn.execute(
            "PRAGMA table_info(guardian_authority_shadow_observations)"
        ).fetchall()
    }
    assert columns == {
        "shadow_id", "position_id", "candidate_id", "instrument", "opened_at",
        "status", "shadow_decision", "decided_at", "expected_outcome",
        "expected_direction", "confidence", "factors_json", "proposed_new_sl",
        "mfe", "mae", "last_factors_json", "actual_exit_reason",
        "actual_pnl_usdt", "actual_closed_at", "expectation_correct",
        "prediction_error", "created_at", "updated_at", "run_id",
    }


def test_seed_guardian_authority_shadow_creates_a_row_with_observing_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    created = repo.seed_guardian_authority_shadow(**_seed_kwargs())
    assert created is True

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["position_id"] == "pos-1"
    assert row["candidate_id"] == "cand-1"
    assert row["instrument"] == "BTCUSDT"
    assert row["opened_at"] == _NOW.isoformat()
    assert row["status"] == "OBSERVING"
    assert row["mfe"] == "0"
    assert row["mae"] == "0"
    assert row["run_id"] == "run-1"
    # All decision/resolution fields NULL at seed time.
    for field in (
        "shadow_decision", "decided_at", "expected_outcome", "expected_direction",
        "confidence", "factors_json", "proposed_new_sl", "last_factors_json",
        "actual_exit_reason", "actual_pnl_usdt", "actual_closed_at",
        "expectation_correct", "prediction_error",
    ):
        assert row[field] is None, field


def test_seed_guardian_authority_shadow_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first = repo.seed_guardian_authority_shadow(**_seed_kwargs())
    second = repo.seed_guardian_authority_shadow(**_seed_kwargs(instrument="ETHUSDT"))

    assert first is True
    assert second is False
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["instrument"] == "BTCUSDT"  # never overwritten


def test_get_guardian_authority_shadow_returns_none_before_seed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.get_guardian_authority_shadow("pos-1") is None


def test_find_open_guardian_authority_shadows_includes_observing_and_decided_only(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="a", position_id="a"))
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="b", position_id="b"))
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="c", position_id="c"))
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="d", position_id="d"))

    repo.decide_guardian_authority_shadow(
        "b", "TIGHTEN_SL", _NOW, "expect favorable", "favorable", 0.7,
        '{"a":1}', Decimal("49500"), _NOW,
    )
    repo.resolve_guardian_authority_shadow_no_action(
        "c", '{"snap":1}', "target", Decimal("150"), _NOW, _NOW,
    )
    repo.abandon_guardian_authority_shadow("d", _NOW)

    open_ids = {row["shadow_id"] for row in repo.find_open_guardian_authority_shadows()}
    assert open_ids == {"a", "b"}


def test_record_guardian_authority_shadow_tick_updates_mfe_mae_and_last_factors_json(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())

    repo.record_guardian_authority_shadow_tick(
        "pos-1", Decimal("120"), Decimal("-30"), '{"rsi": 55}', _NOW,
    )

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["mfe"] == "120"
    assert row["mae"] == "-30"
    assert row["last_factors_json"] == '{"rsi": 55}'


def test_record_guardian_authority_shadow_tick_updates_last_factors_json_on_every_call(tmp_path):
    """last_factors_json always reflects the MOST RECENT tick, even for a
    shadow that never gets a real hypothetical intervention - this is what
    later lets resolve_guardian_authority_shadow_no_action's own
    factors_json param be populated from a recent snapshot."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())

    repo.record_guardian_authority_shadow_tick(
        "pos-1", Decimal("10"), Decimal("-5"), '{"tick": 1}', _NOW,
    )
    later = _NOW + timedelta(minutes=5)
    repo.record_guardian_authority_shadow_tick(
        "pos-1", Decimal("20"), Decimal("-5"), '{"tick": 2}', later,
    )

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["mfe"] == "20"
    assert row["last_factors_json"] == '{"tick": 2}'


def test_record_guardian_authority_shadow_tick_is_a_no_op_once_resolved(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.resolve_guardian_authority_shadow_no_action(
        "pos-1", '{"snap":1}', "target", Decimal("150"), _NOW, _NOW,
    )

    repo.record_guardian_authority_shadow_tick(
        "pos-1", Decimal("999"), Decimal("-999"), '{"late": True}', _NOW,
    )

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["mfe"] == "0"  # frozen, never touched by the post-resolve tick
    assert row["mae"] == "0"
    assert row["last_factors_json"] is None


def test_record_guardian_authority_shadow_tick_is_a_no_op_once_abandoned(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.abandon_guardian_authority_shadow("pos-1", _NOW)

    repo.record_guardian_authority_shadow_tick(
        "pos-1", Decimal("999"), Decimal("-999"), '{"late": True}', _NOW,
    )

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["mfe"] == "0"
    assert row["mae"] == "0"
    assert row["last_factors_json"] is None


def test_record_guardian_authority_shadow_tick_after_decide_leaves_factors_json_frozen(tmp_path):
    """Controller ruling for last_factors_json: it keeps updating every tick
    even after factors_json is frozen by decide_guardian_authority_shadow.
    Proves the two columns are genuinely independent - factors_json is the
    one-time snapshot taken at decision time, last_factors_json keeps
    tracking every subsequent tick regardless of status."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())

    repo.decide_guardian_authority_shadow(
        "pos-1", "TIGHTEN_SL", _NOW, "expect small favorable move", "favorable",
        0.7, '{"a": 1}', Decimal("49500"), _NOW,
    )

    later = _NOW + timedelta(minutes=5)
    repo.record_guardian_authority_shadow_tick(
        "pos-1", Decimal("50"), Decimal("-10"), '{"a": 2}', later,
    )

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "DECIDED"
    assert row["factors_json"] == '{"a": 1}'  # frozen at decision time
    assert row["last_factors_json"] == '{"a": 2}'  # keeps tracking new ticks
    assert row["mfe"] == "50"
    assert row["mae"] == "-10"


def test_decide_guardian_authority_shadow_transitions_to_decided_and_sets_fields(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())

    decided = repo.decide_guardian_authority_shadow(
        "pos-1", "TIGHTEN_SL", _NOW, "expect small favorable move", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )

    assert decided is True
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "DECIDED"
    assert row["shadow_decision"] == "TIGHTEN_SL"
    assert row["decided_at"] == _NOW.isoformat()
    assert row["expected_outcome"] == "expect small favorable move"
    assert row["expected_direction"] == "favorable"
    assert row["confidence"] == 0.7
    assert row["factors_json"] == '{"rsi": 55}'
    assert row["proposed_new_sl"] == "49500"


def test_decide_guardian_authority_shadow_allows_none_proposed_new_sl_for_close_early(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())

    decided = repo.decide_guardian_authority_shadow(
        "pos-1", "CLOSE_EARLY", _NOW, "expect unfavorable move", "unfavorable",
        0.85, '{"rsi": 80}', None, _NOW,
    )

    assert decided is True
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["shadow_decision"] == "CLOSE_EARLY"
    assert row["proposed_new_sl"] is None


def test_decide_guardian_authority_shadow_is_a_one_time_transition(tmp_path):
    """Same non-overwrite proof style as
    test_mark_guardian_authority_decision_intervention_applied_updates_only_that_column:
    a second decide call after DECIDED must be a genuine no-op - returns
    False and leaves every field byte-identical to the first call."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.decide_guardian_authority_shadow(
        "pos-1", "TIGHTEN_SL", _NOW, "expect small favorable move", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )
    before = repo.get_guardian_authority_shadow("pos-1")

    later = _NOW + timedelta(minutes=5)
    second = repo.decide_guardian_authority_shadow(
        "pos-1", "CLOSE_EARLY", later, "different outcome", "unfavorable",
        0.99, '{"rsi": 99}', None, later,
    )

    assert second is False
    after = repo.get_guardian_authority_shadow("pos-1")
    assert after == before


def test_decide_guardian_authority_shadow_returns_false_for_unknown_shadow_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    decided = repo.decide_guardian_authority_shadow(
        "missing", "TIGHTEN_SL", _NOW, "expect favorable", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )
    assert decided is False


def test_resolve_guardian_authority_shadow_no_action_sets_fields_and_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())

    resolved = repo.resolve_guardian_authority_shadow_no_action(
        "pos-1", '{"rsi": 50}', "target", Decimal("150"), _NOW, _NOW,
    )

    assert resolved is True
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "RESOLVED"
    assert row["shadow_decision"] == "NO_ACTION"
    assert row["expected_direction"] == "neutral"
    assert row["confidence"] == 1.0
    assert row["factors_json"] == '{"rsi": 50}'
    assert row["actual_exit_reason"] == "target"
    assert row["actual_pnl_usdt"] == "150"
    assert row["actual_closed_at"] == _NOW.isoformat()


def test_resolve_guardian_authority_shadow_no_action_only_fires_from_observing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.decide_guardian_authority_shadow(
        "pos-1", "TIGHTEN_SL", _NOW, "expect favorable", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )

    resolved = repo.resolve_guardian_authority_shadow_no_action(
        "pos-1", '{"rsi": 50}', "target", Decimal("150"), _NOW, _NOW,
    )

    assert resolved is False
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "DECIDED"  # untouched
    assert row["shadow_decision"] == "TIGHTEN_SL"  # never overwritten to NO_ACTION


def test_resolve_guardian_authority_shadow_decided_sets_fields_and_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.decide_guardian_authority_shadow(
        "pos-1", "TIGHTEN_SL", _NOW, "expect small favorable move", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )

    resolved = repo.resolve_guardian_authority_shadow_decided(
        "pos-1", "stop_loss", Decimal("-40"), _NOW, True, 0.09, _NOW,
    )

    assert resolved is True
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "stop_loss"
    assert row["actual_pnl_usdt"] == "-40"
    assert row["actual_closed_at"] == _NOW.isoformat()
    assert row["expectation_correct"] == 1
    assert row["prediction_error"] == 0.09
    # Pre-registered expectation fields untouched by resolution.
    assert row["shadow_decision"] == "TIGHTEN_SL"
    assert row["expected_direction"] == "favorable"
    assert row["confidence"] == 0.7


def test_resolve_guardian_authority_shadow_decided_only_fires_from_decided(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())  # still OBSERVING

    resolved = repo.resolve_guardian_authority_shadow_decided(
        "pos-1", "stop_loss", Decimal("-40"), _NOW, True, 0.09, _NOW,
    )

    assert resolved is False
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "OBSERVING"
    assert row["actual_exit_reason"] is None


def test_resolve_guardian_authority_shadow_decided_accepts_none_expectation_and_error(tmp_path):
    """Task 8's own CLOSE_EARLY ruling, adapted: expectation_correct/
    prediction_error stay NULL forever for a CLOSE_EARLY shadow row."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.decide_guardian_authority_shadow(
        "pos-1", "CLOSE_EARLY", _NOW, "expect unfavorable move", "unfavorable",
        0.85, '{"rsi": 80}', None, _NOW,
    )

    resolved = repo.resolve_guardian_authority_shadow_decided(
        "pos-1", "manual_close", Decimal("10"), _NOW, None, None, _NOW,
    )

    assert resolved is True
    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["expectation_correct"] is None
    assert row["prediction_error"] is None


def test_abandon_guardian_authority_shadow_from_observing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    later = _NOW + timedelta(minutes=5)

    repo.abandon_guardian_authority_shadow("pos-1", later)

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "ABANDONED"
    assert row["updated_at"] == later.isoformat()


def test_abandon_guardian_authority_shadow_from_decided(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.decide_guardian_authority_shadow(
        "pos-1", "TIGHTEN_SL", _NOW, "expect favorable", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )

    repo.abandon_guardian_authority_shadow("pos-1", _NOW)

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "ABANDONED"


def test_abandon_guardian_authority_shadow_is_a_no_op_once_resolved(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs())
    repo.resolve_guardian_authority_shadow_no_action(
        "pos-1", '{"rsi": 50}', "target", Decimal("150"), _NOW, _NOW,
    )
    later = _NOW + timedelta(minutes=5)

    repo.abandon_guardian_authority_shadow("pos-1", later)

    row = repo.get_guardian_authority_shadow("pos-1")
    assert row["status"] == "RESOLVED"  # untouched
    assert row["updated_at"] == _NOW.isoformat()  # untouched by the no-op abandon call


def test_find_resolved_guardian_authority_shadows_returns_only_resolved_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="a", position_id="a"))
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="b", position_id="b"))
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="c", position_id="c"))

    repo.resolve_guardian_authority_shadow_no_action(
        "a", '{"rsi": 50}', "target", Decimal("150"), _NOW, _NOW,
    )
    repo.decide_guardian_authority_shadow(
        "b", "TIGHTEN_SL", _NOW, "expect favorable", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )
    # c stays OBSERVING

    resolved_ids = {row["shadow_id"] for row in repo.find_resolved_guardian_authority_shadows()}
    assert resolved_ids == {"a"}


def test_find_abandoned_guardian_authority_shadows_returns_only_abandoned_rows(tmp_path):
    """Task 9 fix round 1: find_open_guardian_authority_shadows() and
    find_resolved_guardian_authority_shadows() together silently dropped
    ABANDONED rows from every downstream report count - this method closes
    that gap, same simple SELECT-by-status shape as find_resolved_
    guardian_authority_shadows just above."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="a", position_id="a"))
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="b", position_id="b"))
    repo.seed_guardian_authority_shadow(**_seed_kwargs(shadow_id="c", position_id="c"))

    repo.abandon_guardian_authority_shadow("a", _NOW)  # abandoned from OBSERVING
    repo.decide_guardian_authority_shadow(
        "b", "TIGHTEN_SL", _NOW, "expect favorable", "favorable",
        0.7, '{"rsi": 55}', Decimal("49500"), _NOW,
    )
    repo.abandon_guardian_authority_shadow("b", _NOW)  # abandoned from DECIDED
    # c stays OBSERVING - never abandoned

    abandoned_ids = {row["shadow_id"] for row in repo.find_abandoned_guardian_authority_shadows()}
    assert abandoned_ids == {"a", "b"}
