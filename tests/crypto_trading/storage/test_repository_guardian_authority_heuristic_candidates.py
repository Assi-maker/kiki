"""Tests for guardian_authority_heuristic_candidates (Task 1 of
docs/superpowers/sdd/2026-09-15-guardian-authority-live-autonomy/), the
foundational data table for the Live Autonomy pipeline: LLM-proposed
candidate heuristics move through PROPOSED -> VALIDATED | REJECTED, then
VALIDATED -> PROMOTED, then optionally PROMOTED + a demotion marker (audit
trail only - status never reverts).

Mirrors the Shadow Mode plan's own Task 1/2 repository test shapes
(test_repository_guardian_authority_shadow.py,
test_repository_guardian_authority_pre_entry_shadow.py) for the shared
idempotency/one-time-transition discipline: every state-transition method is
gated by a WHERE-clause status guard (never caller discipline), so a
second/out-of-order call is a structural no-op, proved here via whole-row
byte-identity checks.
"""

from datetime import UTC, datetime, timedelta

from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _save_kwargs(**overrides) -> dict:
    defaults = dict(
        candidate_id="cand-1",
        description="Tighten SL when RSI > 80 and funding is extreme",
        condition_json='{"rsi_gt": 80, "funding_extreme": true}',
        proposed_adjustment=0.15,
        rationale="Historically overbought + extreme funding precedes reversals",
        run_id="run-1",
        proposed_at=_NOW,
    )
    defaults.update(overrides)
    return defaults


def test_guardian_authority_heuristic_candidates_table_exists(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    columns = {
        row["name"]
        for row in repo._conn.execute(
            "PRAGMA table_info(guardian_authority_heuristic_candidates)"
        ).fetchall()
    }
    assert columns == {
        "candidate_id", "proposed_at", "description", "condition_json",
        "proposed_adjustment", "rationale", "status", "train_sample_size",
        "train_correct_rate", "test_sample_size", "test_correct_rate",
        "validated_at", "promoted_at", "promoted_heuristic_id",
        "rejected_reason", "demoted_at", "demotion_reason", "run_id",
        # Task 4B (2026-09-16 addendum): which decision type a candidate is
        # proposed FOR, and therefore which evidence pool it is validated
        # against. Nullable - a legacy row predating this column routes to
        # TIGHTEN_SL.
        "target_decision_type",
    }


def test_save_guardian_authority_heuristic_candidate_creates_a_row_with_proposed_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    created = repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    assert created is True

    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["candidate_id"] == "cand-1"
    assert row["proposed_at"] == _NOW.isoformat()
    assert row["description"] == "Tighten SL when RSI > 80 and funding is extreme"
    assert row["condition_json"] == '{"rsi_gt": 80, "funding_extreme": true}'
    assert row["proposed_adjustment"] == 0.15
    assert row["rationale"] == "Historically overbought + extreme funding precedes reversals"
    assert row["status"] == "PROPOSED"
    assert row["run_id"] == "run-1"
    # Everything downstream of PROPOSED is NULL at save time.
    assert row["train_sample_size"] is None
    assert row["train_correct_rate"] is None
    assert row["test_sample_size"] is None
    assert row["test_correct_rate"] is None
    assert row["validated_at"] is None
    assert row["promoted_at"] is None
    assert row["promoted_heuristic_id"] is None
    assert row["rejected_reason"] is None
    assert row["demoted_at"] is None
    assert row["demotion_reason"] is None
    # Task 4B: the additive keyword param defaults to None, so every
    # pre-existing caller keeps writing exactly the row it always wrote.
    assert row["target_decision_type"] is None


def test_save_guardian_authority_heuristic_candidate_persists_target_decision_type(tmp_path):
    """Task 4B (2026-09-16 addendum): persisted verbatim, per candidate - it
    is what routes the row to its own validation pool later."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(
        **_save_kwargs(candidate_id="tighten", target_decision_type="TIGHTEN_SL")
    )
    repo.save_guardian_authority_heuristic_candidate(
        **_save_kwargs(candidate_id="veto", target_decision_type="PRE_ENTRY_VETO")
    )

    assert (
        repo.get_guardian_authority_heuristic_candidate("tighten")["target_decision_type"]
        == "TIGHTEN_SL"
    )
    assert (
        repo.get_guardian_authority_heuristic_candidate("veto")["target_decision_type"]
        == "PRE_ENTRY_VETO"
    )


def test_save_guardian_authority_heuristic_candidate_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first = repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    second = repo.save_guardian_authority_heuristic_candidate(
        **_save_kwargs(description="different description", proposed_adjustment=0.99)
    )

    assert first is True
    assert second is False
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["description"] == "Tighten SL when RSI > 80 and funding is extreme"
    assert row["proposed_adjustment"] == 0.15


def test_get_guardian_authority_heuristic_candidate_returns_none_before_save(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.get_guardian_authority_heuristic_candidate("cand-1") is None


def test_find_proposed_guardian_authority_heuristic_candidates_returns_only_proposed_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="a"))
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="b"))
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="c"))

    repo.record_guardian_authority_heuristic_candidate_validation(
        "b", "VALIDATED", 100, 0.7, 40, 0.65, _NOW,
    )

    proposed_ids = {
        row["candidate_id"]
        for row in repo.find_proposed_guardian_authority_heuristic_candidates()
    }
    assert proposed_ids == {"a", "c"}


def test_record_guardian_authority_heuristic_candidate_validation_validated_sets_fields_and_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    later = _NOW + timedelta(hours=2)

    recorded = repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "VALIDATED", 200, 0.72, 80, 0.68, later,
    )

    assert recorded is True
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "VALIDATED"
    assert row["train_sample_size"] == 200
    assert row["train_correct_rate"] == 0.72
    assert row["test_sample_size"] == 80
    assert row["test_correct_rate"] == 0.68
    assert row["validated_at"] == later.isoformat()
    assert row["rejected_reason"] is None
    # Still not promoted.
    assert row["promoted_at"] is None
    assert row["promoted_heuristic_id"] is None


def test_record_guardian_authority_heuristic_candidate_validation_rejected_sets_status_and_reason(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())

    recorded = repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "REJECTED", 200, 0.51, 80, 0.49, _NOW,
        rejected_reason="test-set correct rate below threshold",
    )

    assert recorded is True
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "REJECTED"
    assert row["rejected_reason"] == "test-set correct rate below threshold"
    assert row["validated_at"] == _NOW.isoformat()


def test_record_guardian_authority_heuristic_candidate_validation_is_a_one_time_transition(tmp_path):
    """Only fires from PROPOSED - a second call (whatever its outcome) is a
    structural no-op, proved via whole-row byte-identity, same style as
    test_decide_guardian_authority_shadow_is_a_one_time_transition."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )
    before = repo.get_guardian_authority_heuristic_candidate("cand-1")

    later = _NOW + timedelta(hours=2)
    second = repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "REJECTED", 999, 0.01, 999, 0.01, later,
        rejected_reason="should never be written",
    )

    assert second is False
    after = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert after == before


def test_record_guardian_authority_heuristic_candidate_validation_returns_false_for_unknown_candidate_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    recorded = repo.record_guardian_authority_heuristic_candidate_validation(
        "missing", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )
    assert recorded is False


def test_find_validated_guardian_authority_heuristic_candidates_returns_only_validated_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="a"))
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="b"))
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="c"))

    repo.record_guardian_authority_heuristic_candidate_validation(
        "a", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )
    repo.record_guardian_authority_heuristic_candidate_validation(
        "b", "REJECTED", 200, 0.4, 80, 0.4, _NOW, rejected_reason="bad",
    )
    # c stays PROPOSED

    validated_ids = {
        row["candidate_id"]
        for row in repo.find_validated_guardian_authority_heuristic_candidates()
    }
    assert validated_ids == {"a"}


def test_promote_guardian_authority_heuristic_candidate_sets_fields_and_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )
    later = _NOW + timedelta(hours=1)

    promoted = repo.promote_guardian_authority_heuristic_candidate(
        "cand-1", "heur-99", later,
    )

    assert promoted is True
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "PROMOTED"
    assert row["promoted_heuristic_id"] == "heur-99"
    assert row["promoted_at"] == later.isoformat()
    # Validation fields untouched by promotion.
    assert row["train_sample_size"] == 200
    assert row["test_correct_rate"] == 0.68


def test_promote_guardian_authority_heuristic_candidate_only_fires_from_validated(tmp_path):
    """A candidate still PROPOSED (never validated) cannot be promoted -
    the WHERE status = 'VALIDATED' guard rejects it structurally."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())

    promoted = repo.promote_guardian_authority_heuristic_candidate(
        "cand-1", "heur-99", _NOW,
    )

    assert promoted is False
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "PROPOSED"
    assert row["promoted_heuristic_id"] is None


def test_promote_guardian_authority_heuristic_candidate_is_a_one_time_transition(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )
    repo.promote_guardian_authority_heuristic_candidate("cand-1", "heur-99", _NOW)
    before = repo.get_guardian_authority_heuristic_candidate("cand-1")

    later = _NOW + timedelta(hours=1)
    second = repo.promote_guardian_authority_heuristic_candidate(
        "cand-1", "heur-different", later,
    )

    assert second is False
    after = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert after == before


def test_promote_guardian_authority_heuristic_candidate_returns_false_for_unknown_candidate_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    promoted = repo.promote_guardian_authority_heuristic_candidate(
        "missing", "heur-99", _NOW,
    )
    assert promoted is False


def test_find_promoted_guardian_authority_heuristic_candidates_returns_only_promoted_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="a"))
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs(candidate_id="b"))
    for cid in ("a", "b"):
        repo.record_guardian_authority_heuristic_candidate_validation(
            cid, "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
        )
    repo.promote_guardian_authority_heuristic_candidate("a", "heur-a", _NOW)
    # b stays VALIDATED, not promoted

    promoted_ids = {
        row["candidate_id"]
        for row in repo.find_promoted_guardian_authority_heuristic_candidates()
    }
    assert promoted_ids == {"a"}


def test_mark_guardian_authority_heuristic_candidate_demoted_sets_fields_and_keeps_status_promoted(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )
    repo.promote_guardian_authority_heuristic_candidate("cand-1", "heur-99", _NOW)
    later = _NOW + timedelta(days=10)

    demoted = repo.mark_guardian_authority_heuristic_candidate_demoted(
        "cand-1", later, "live sample diverged from validation expectations",
    )

    assert demoted is True
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    # status is an audit trail - never reverted to a prior state.
    assert row["status"] == "PROMOTED"
    assert row["demoted_at"] == later.isoformat()
    assert row["demotion_reason"] == "live sample diverged from validation expectations"
    # Promotion fields untouched.
    assert row["promoted_heuristic_id"] == "heur-99"


def test_mark_guardian_authority_heuristic_candidate_demoted_only_fires_from_promoted(tmp_path):
    """A candidate that is only VALIDATED (never promoted) cannot be marked
    demoted - the WHERE status = 'PROMOTED' guard rejects it structurally."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )

    demoted = repo.mark_guardian_authority_heuristic_candidate_demoted(
        "cand-1", _NOW, "should not apply",
    )

    assert demoted is False
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "VALIDATED"
    assert row["demoted_at"] is None
    assert row["demotion_reason"] is None


def test_mark_guardian_authority_heuristic_candidate_demoted_is_a_one_time_transition(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(**_save_kwargs())
    repo.record_guardian_authority_heuristic_candidate_validation(
        "cand-1", "VALIDATED", 200, 0.72, 80, 0.68, _NOW,
    )
    repo.promote_guardian_authority_heuristic_candidate("cand-1", "heur-99", _NOW)
    repo.mark_guardian_authority_heuristic_candidate_demoted(
        "cand-1", _NOW, "first demotion reason",
    )
    before = repo.get_guardian_authority_heuristic_candidate("cand-1")

    later = _NOW + timedelta(days=20)
    second = repo.mark_guardian_authority_heuristic_candidate_demoted(
        "cand-1", later, "second demotion reason should never be written",
    )

    assert second is False
    after = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert after == before


def test_mark_guardian_authority_heuristic_candidate_demoted_returns_false_for_unknown_candidate_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    demoted = repo.mark_guardian_authority_heuristic_candidate_demoted(
        "missing", _NOW, "reason",
    )
    assert demoted is False
