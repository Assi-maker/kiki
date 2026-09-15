"""Tests for guardian_authority_shadow_pre_entry_observations (Task 2 of
docs/superpowers/plans/2026-09-15-guardian-authority-shadow.md), the
pre-entry half of Guardian Authority's shadow/observation mode. Mirrors
test_repository_guardian_authority_shadow.py's (Task 1) own repository test
shapes for the shared idempotency/one-time-transition discipline, but this
table is single-shot (no OBSERVING/DECIDED intermediate state - one INSERT
at confirm time sets every decision-shaped column at once, and the only
transition is PENDING -> RESOLVED).

Controller simplification (2026-09-15): shadow_id IS candidate_id (which is
also, by construction, what the real position's position_id will be if one
ever opens - position_opening.py: `position_id=candidate.candidate_id`).
There is no separate position_id column and no
link_guardian_authority_pre_entry_shadow_to_position method - a later task
resolves a shadow row via repo.get_position(shadow_id) directly.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _save_kwargs(**overrides) -> dict:
    defaults = dict(
        shadow_id="cand-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        shadow_decision="APPROVE",
        expected_outcome="expect favorable entry",
        expected_direction="favorable",
        confidence=0.8,
        factors_json='{"rsi": 55}',
        run_id="run-1",
        created_at=_NOW,
    )
    defaults.update(overrides)
    return defaults


def test_guardian_authority_shadow_pre_entry_observations_table_exists(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    columns = {
        row["name"]
        for row in repo._conn.execute(
            "PRAGMA table_info(guardian_authority_shadow_pre_entry_observations)"
        ).fetchall()
    }
    assert columns == {
        "shadow_id", "candidate_id", "instrument", "shadow_decision",
        "expected_outcome", "expected_direction", "confidence", "factors_json",
        "status", "actual_exit_reason", "actual_pnl_usdt", "actual_closed_at",
        "expectation_correct", "created_at", "updated_at", "run_id",
    }
    # No position_id column - shadow_id already serves that purpose
    # (controller simplification, see repository.py's Task 2 comments).
    assert "position_id" not in columns


def test_save_guardian_authority_pre_entry_shadow_creates_a_row_with_pending_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    created = repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs())
    assert created is True

    row = repo.get_guardian_authority_pre_entry_shadow("cand-1")
    assert row["shadow_id"] == "cand-1"
    assert row["candidate_id"] == "cand-1"
    assert row["instrument"] == "BTCUSDT"
    assert row["shadow_decision"] == "APPROVE"
    assert row["expected_outcome"] == "expect favorable entry"
    assert row["expected_direction"] == "favorable"
    assert row["confidence"] == 0.8
    assert row["factors_json"] == '{"rsi": 55}'
    assert row["status"] == "PENDING"
    assert row["run_id"] == "run-1"
    assert row["created_at"] == _NOW.isoformat()
    assert row["updated_at"] == _NOW.isoformat()
    # Resolution fields NULL at save time.
    assert row["actual_exit_reason"] is None
    assert row["actual_pnl_usdt"] is None
    assert row["actual_closed_at"] is None
    # expectation_correct is NULL at save time and, per spec, stays NULL
    # forever for every row of this table - resolve() never sets it.
    assert row["expectation_correct"] is None


def test_save_guardian_authority_pre_entry_shadow_records_pre_entry_veto_decision(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_pre_entry_shadow(
        **_save_kwargs(
            shadow_decision="PRE_ENTRY_VETO",
            expected_outcome="expect unfavorable entry",
            expected_direction="unfavorable",
            confidence=0.9,
            factors_json='{"rsi": 92}',
        )
    )

    row = repo.get_guardian_authority_pre_entry_shadow("cand-1")
    assert row["shadow_decision"] == "PRE_ENTRY_VETO"
    assert row["expected_direction"] == "unfavorable"
    assert row["confidence"] == 0.9


def test_save_guardian_authority_pre_entry_shadow_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first = repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs())
    second = repo.save_guardian_authority_pre_entry_shadow(
        **_save_kwargs(shadow_decision="PRE_ENTRY_VETO", instrument="ETHUSDT")
    )

    assert first is True
    assert second is False
    row = repo.get_guardian_authority_pre_entry_shadow("cand-1")
    assert row["instrument"] == "BTCUSDT"  # never overwritten
    assert row["shadow_decision"] == "APPROVE"  # never overwritten


def test_get_guardian_authority_pre_entry_shadow_returns_none_before_save(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.get_guardian_authority_pre_entry_shadow("cand-1") is None


def test_find_pending_guardian_authority_pre_entry_shadows_returns_only_pending_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs(shadow_id="a", candidate_id="a"))
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs(shadow_id="b", candidate_id="b"))
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs(shadow_id="c", candidate_id="c"))

    repo.resolve_guardian_authority_pre_entry_shadow(
        "b", "target", Decimal("150"), _NOW, _NOW,
    )

    pending_ids = {
        row["shadow_id"] for row in repo.find_pending_guardian_authority_pre_entry_shadows()
    }
    assert pending_ids == {"a", "c"}


def test_find_pending_guardian_authority_pre_entry_shadows_does_not_filter_on_position(tmp_path):
    """No position_id column exists to filter on - every PENDING row is
    returned regardless of whether a real position has opened yet. A later
    task's resolution logic checks repo.get_position(shadow_id) itself."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs())

    pending = repo.find_pending_guardian_authority_pre_entry_shadows()
    assert len(pending) == 1
    assert pending[0]["shadow_id"] == "cand-1"


def test_resolve_guardian_authority_pre_entry_shadow_sets_fields_and_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs())
    later = _NOW + timedelta(hours=3)

    resolved = repo.resolve_guardian_authority_pre_entry_shadow(
        "cand-1", "stop_loss", Decimal("-25"), later, later,
    )

    assert resolved is True
    row = repo.get_guardian_authority_pre_entry_shadow("cand-1")
    assert row["status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "stop_loss"
    assert row["actual_pnl_usdt"] == "-25"
    assert row["actual_closed_at"] == later.isoformat()
    assert row["updated_at"] == later.isoformat()
    # Never set by resolve - stays NULL forever for this table.
    assert row["expectation_correct"] is None
    # Pre-registered decision fields untouched by resolution.
    assert row["shadow_decision"] == "APPROVE"
    assert row["expected_direction"] == "favorable"
    assert row["confidence"] == 0.8


def test_resolve_guardian_authority_pre_entry_shadow_is_a_one_time_transition(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs())
    repo.resolve_guardian_authority_pre_entry_shadow(
        "cand-1", "stop_loss", Decimal("-25"), _NOW, _NOW,
    )
    before = repo.get_guardian_authority_pre_entry_shadow("cand-1")

    later = _NOW + timedelta(hours=3)
    second = repo.resolve_guardian_authority_pre_entry_shadow(
        "cand-1", "target", Decimal("999"), later, later,
    )

    assert second is False
    after = repo.get_guardian_authority_pre_entry_shadow("cand-1")
    assert after == before


def test_resolve_guardian_authority_pre_entry_shadow_returns_false_for_unknown_shadow_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    resolved = repo.resolve_guardian_authority_pre_entry_shadow(
        "missing", "stop_loss", Decimal("-25"), _NOW, _NOW,
    )
    assert resolved is False


def test_find_resolved_guardian_authority_pre_entry_shadows_returns_only_resolved_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs(shadow_id="a", candidate_id="a"))
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs(shadow_id="b", candidate_id="b"))
    repo.save_guardian_authority_pre_entry_shadow(**_save_kwargs(shadow_id="c", candidate_id="c"))

    repo.resolve_guardian_authority_pre_entry_shadow(
        "a", "target", Decimal("150"), _NOW, _NOW,
    )
    # b and c stay PENDING

    resolved_ids = {
        row["shadow_id"] for row in repo.find_resolved_guardian_authority_pre_entry_shadows()
    }
    assert resolved_ids == {"a"}
