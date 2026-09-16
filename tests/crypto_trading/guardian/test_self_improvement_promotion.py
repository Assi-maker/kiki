"""Tests for Task 5 of docs/superpowers/sdd/2026-09-15-guardian-authority-
live-autonomy/ - the PROMOTE step of the propose -> validate -> promote ->
track/demote pipeline (crypto_trading/guardian/self_improvement.py::
promote_validated_heuristic_candidates), including the co-firing cap the
design spec's "Addendum (2026-09-16)" R3 requires - and, added by Task 6, the
TIGHTEN_SL cardinality cap that same addendum's consequence 1 requires, which
is enforced at promotion time and therefore lives in this function rather than
in Task 6's own.

This is the one step of the pipeline that writes into the REAL
`guardian_authority_heuristics` table - the table `evaluate_heuristics`
reads on every live decision - so the assertions here are deliberately
end-to-end rather than mock-based: every fixture is seeded through the real
repository methods (`save_guardian_authority_heuristic_candidate` +
`record_guardian_authority_heuristic_candidate_validation`, i.e. exactly the
rows Tasks 3/4 themselves produce), and every outcome is read back through
the existing, unmodified `repo.find_guardian_authority_heuristics()` and fed
to the existing, unmodified `evaluate_heuristics` - never by inspecting an
internal value the production code happened to compute.

There is no AI call anywhere in this file; `promote_validated_heuristic_
candidates` makes none.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from crypto_trading.guardian.authority import evaluate_heuristics
from crypto_trading.guardian.self_improvement import (
    _MAX_LIVE_TIGHTEN_SL_HEURISTICS,
    promote_validated_heuristic_candidates,
)
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(days=1)
_VALIDATED_AT = _NOW - timedelta(hours=1)

# The cap's DOCUMENTED value, written out as a literal on purpose: every
# cardinality-cap fixture below is sized from this, never from the production
# constant, so raising the constant makes those tests fail instead of quietly
# resizing itself to match. Changing the cap is a deliberate, documented
# decision (see `_MAX_LIVE_TIGHTEN_SL_HEURISTICS`' own section) and should
# have to be made here too.
_CAP = 3

# The factors dict every condition in this file is matched against. Two
# DIFFERENT conditions both match it - which is exactly the co-firing shape
# R3 exists for.
_CO_FIRING_FACTORS = {"guardian_state": "PROTECT", "instrument": "BTCUSDT"}
_STATE_CONDITION = {"guardian_state": "PROTECT"}
_INSTRUMENT_CONDITION = {"instrument": "BTCUSDT"}


# --------------------------------------------------------------------------
# Seeding helper - a candidate row in exactly the state Task 4 leaves it in
# --------------------------------------------------------------------------
def _seed_validated_candidate(
    repo,
    candidate_id="cand-1",
    condition=None,
    test_sample_size=40,
    test_correct_rate=0.9,
    train_sample_size=90,
    train_correct_rate=0.85,
    # Deliberately large, deliberately wrong, and deliberately the OPPOSITE
    # shape of what the test split says: `proposed_adjustment` is the LLM's
    # own informational rationale field and must never reach the real table.
    proposed_adjustment=0.95,
    target_decision_type="TIGHTEN_SL",
    status="VALIDATED",
):
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id=candidate_id,
        description=f"{candidate_id} description",
        condition_json=json.dumps(condition if condition is not None else _STATE_CONDITION),
        proposed_adjustment=proposed_adjustment,
        rationale=f"{candidate_id} rationale",
        run_id="run-llm",
        proposed_at=_VALIDATED_AT - timedelta(hours=1),
        target_decision_type=target_decision_type,
    )
    if status == "PROPOSED":
        return
    repo.record_guardian_authority_heuristic_candidate_validation(
        candidate_id=candidate_id,
        status=status,
        train_sample_size=train_sample_size,
        train_correct_rate=train_correct_rate,
        test_sample_size=test_sample_size,
        test_correct_rate=test_correct_rate,
        validated_at=_VALIDATED_AT,
        rejected_reason=None if status == "VALIDATED" else "seeded rejection",
    )


def _heuristics_by_id(repo) -> dict:
    return {row["heuristic_id"]: row for row in repo.find_guardian_authority_heuristics()}


# --------------------------------------------------------------------------
# The promotion itself
# --------------------------------------------------------------------------
def test_promotes_a_validated_candidate_with_values_derived_from_its_test_split(tmp_path):
    """Hand-checked fixture: test_correct_rate=0.9 -> deviation=0.4 ->
    adjustment=0.4*_ADJUSTMENT_SCALE(1.0)/family_size(1)=0.4,
    confidence=|0.4|*2.0=0.8, sample_size=test_sample_size=40."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "cand-1", test_sample_size=40, test_correct_rate=0.9)

    assert promote_validated_heuristic_candidates(repo, _NOW) == 1

    heuristics = repo.find_guardian_authority_heuristics()
    assert len(heuristics) == 1
    row = heuristics[0]
    assert row["heuristic_id"] == "ga-llm:cand-1"
    assert row["description"] == "cand-1 description"
    # The candidate's own condition_json, verbatim - never re-serialized or
    # "cleaned up" on the way into the real table.
    assert json.loads(row["condition_json"]) == _STATE_CONDITION
    assert row["adjustment"] == pytest.approx(0.4)
    assert row["confidence"] == pytest.approx(0.8)
    assert row["sample_size"] == 40
    assert row["updated_at"] == _NOW.isoformat()


def test_adjustment_comes_from_the_test_split_never_from_proposed_adjustment(tmp_path):
    """The LLM proposed +0.95; the held-out test split says the pattern runs
    the OTHER way (correct_rate 0.2 -> deviation -0.3). The real table must
    get -0.3, and nothing whatsoever from the proposal."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(
        repo,
        "cand-1",
        test_sample_size=33,
        test_correct_rate=0.2,
        train_correct_rate=0.25,
        proposed_adjustment=0.95,
    )

    assert promote_validated_heuristic_candidates(repo, _NOW) == 1

    row = _heuristics_by_id(repo)["ga-llm:cand-1"]
    assert row["adjustment"] == pytest.approx(-0.3)
    assert row["confidence"] == pytest.approx(0.6)
    assert row["sample_size"] == 33


def test_promotion_records_the_transition_on_the_candidate_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "cand-1")

    promote_validated_heuristic_candidates(repo, _NOW)

    candidate = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert candidate["status"] == "PROMOTED"
    assert candidate["promoted_heuristic_id"] == "ga-llm:cand-1"
    assert candidate["promoted_at"] == _NOW.isoformat()
    # Promotion never touches the validation record it was earned with.
    assert candidate["test_correct_rate"] == pytest.approx(0.9)
    assert candidate["validated_at"] == _VALIDATED_AT.isoformat()


def test_the_promoted_heuristic_is_live_for_the_real_decision_core(tmp_path):
    """"Genuinely visible to (and only to) find_guardian_authority_
    heuristics" - read back through that exact, unmodified method and scored
    by the exact, unmodified evaluate_heuristics, not by inspecting what the
    promotion code computed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "cand-1", condition=_STATE_CONDITION, test_correct_rate=0.9)

    promote_validated_heuristic_candidates(repo, _NOW)

    heuristics = repo.find_guardian_authority_heuristics()
    score, matched_ids = evaluate_heuristics(_CO_FIRING_FACTORS, heuristics)
    assert matched_ids == ["ga-llm:cand-1"]
    assert score == pytest.approx(0.4)

    # Fail-closed on a factors dict the condition does not describe.
    other_score, other_matched = evaluate_heuristics({"guardian_state": "HOLD"}, heuristics)
    assert other_matched == []
    assert other_score == 0.0

    # And the candidate row itself has left every pre-promotion queue.
    assert repo.find_validated_guardian_authority_heuristic_candidates() == []
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []


def test_a_second_promotion_pass_is_a_structural_no_op(tmp_path):
    """Task 1's own `WHERE status = 'VALIDATED'` guard: an already-PROMOTED
    candidate is never promoted (or re-written) a second time."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "cand-1")

    assert promote_validated_heuristic_candidates(repo, _NOW) == 1
    first = _heuristics_by_id(repo)

    assert promote_validated_heuristic_candidates(repo, _LATER) == 0
    assert _heuristics_by_id(repo) == first
    assert repo.get_guardian_authority_heuristic_candidate("cand-1")["promoted_at"] == (
        _NOW.isoformat()
    )


def test_only_validated_candidates_are_promoted(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "proposed-1", status="PROPOSED")
    _seed_validated_candidate(repo, "rejected-1", status="REJECTED")
    _seed_validated_candidate(repo, "validated-1")

    assert promote_validated_heuristic_candidates(repo, _NOW) == 1
    assert set(_heuristics_by_id(repo)) == {"ga-llm:validated-1"}


def test_nothing_is_written_when_no_candidate_is_validated(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "proposed-1", status="PROPOSED")

    assert promote_validated_heuristic_candidates(repo, _NOW) == 0
    assert repo.find_guardian_authority_heuristics() == []


# --------------------------------------------------------------------------
# R3 - the co-firing cap (design spec "Addendum (2026-09-16)", R3)
#
# Mechanism under test: every live `ga-llm:*` heuristic's stored `adjustment`
# is its own test-split adjustment divided by the number of live `ga-llm:*`
# heuristics, so the family's TOTAL contribution to any single
# `evaluate_heuristics` call is the family's AVERAGE, never its sum - and an
# average can never exceed its own largest member. Each test below therefore
# proves the bound the way a real decision would experience it: a real
# `evaluate_heuristics` call over `repo.find_guardian_authority_heuristics()`.
# --------------------------------------------------------------------------
def test_co_firing_heuristics_promoted_in_one_pass_never_beat_the_strongest_single_one(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    # Two DIFFERENT conditions, both of which match _CO_FIRING_FACTORS.
    _seed_validated_candidate(
        repo, "cand-1", condition=_STATE_CONDITION, test_correct_rate=0.9
    )  # own worth 0.4
    _seed_validated_candidate(
        repo, "cand-2", condition=_INSTRUMENT_CONDITION, test_correct_rate=0.8
    )  # own worth 0.3

    assert promote_validated_heuristic_candidates(repo, _NOW) == 2

    heuristics = repo.find_guardian_authority_heuristics()
    score, matched_ids = evaluate_heuristics(_CO_FIRING_FACTORS, heuristics)
    # Both genuinely co-fire on the same decision - the cap is not achieved
    # by making them miss.
    assert sorted(matched_ids) == ["ga-llm:cand-1", "ga-llm:cand-2"]
    # Uncapped this would be 0.4 + 0.3 = 0.7, above BOTH the default
    # authority_veto_threshold (0.3) and authority_tighten_threshold (0.15)
    # that neither heuristic alone could have reached that way.
    assert score == pytest.approx(0.35)
    assert score <= max(0.4, 0.3)


def test_the_co_firing_cap_holds_as_the_family_grows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    worths = {"cand-1": 0.4, "cand-2": 0.3, "cand-3": 0.2, "cand-4": 0.45}
    for index, (candidate_id, worth) in enumerate(worths.items()):
        _seed_validated_candidate(
            repo,
            candidate_id,
            # Four distinct conditions, all matching the same factors dict.
            condition={"guardian_state": "PROTECT"} if index % 2 else {"instrument": "BTCUSDT"},
            test_correct_rate=0.5 + worth,
            # Two of each (Task 6 amendment): the TIGHTEN_SL cardinality cap
            # below allows at most _CAP of that
            # track, and the co-firing property under test here is a
            # whole-family one anyway - mixed targets are the harder case,
            # since the real heuristics table has no target column and both
            # kinds co-fire on the same factors dict.
            target_decision_type="TIGHTEN_SL" if index % 2 else "PRE_ENTRY_VETO",
        )

    assert promote_validated_heuristic_candidates(repo, _NOW) == 4

    heuristics = repo.find_guardian_authority_heuristics()
    score, matched_ids = evaluate_heuristics(_CO_FIRING_FACTORS, heuristics)
    assert len(matched_ids) == 4
    assert score == pytest.approx(sum(worths.values()) / 4)
    assert score <= max(worths.values())


def test_an_already_live_heuristic_is_rescaled_when_a_new_one_joins_the_family(tmp_path):
    """The cap must survive promotions that happen in SEPARATE passes - the
    common real case, since a candidate is validated whenever its own
    evidence matures. The previously-promoted row is re-scaled through the
    same single write path, never left at its old, now-uncapped strength."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "cand-1", condition=_STATE_CONDITION, test_correct_rate=0.9)
    assert promote_validated_heuristic_candidates(repo, _NOW) == 1
    assert _heuristics_by_id(repo)["ga-llm:cand-1"]["adjustment"] == pytest.approx(0.4)

    _seed_validated_candidate(
        repo, "cand-2", condition=_INSTRUMENT_CONDITION, test_correct_rate=0.8
    )
    assert promote_validated_heuristic_candidates(repo, _LATER) == 1

    heuristics = _heuristics_by_id(repo)
    assert heuristics["ga-llm:cand-1"]["adjustment"] == pytest.approx(0.2)
    assert heuristics["ga-llm:cand-2"]["adjustment"] == pytest.approx(0.15)
    # The rescale refreshes updated_at (it IS a real write), but must not
    # disturb anything else about the already-live row.
    assert heuristics["ga-llm:cand-1"]["confidence"] == pytest.approx(0.8)
    assert heuristics["ga-llm:cand-1"]["sample_size"] == 40
    assert heuristics["ga-llm:cand-1"]["updated_at"] == _LATER.isoformat()
    # Re-promotion is still a no-op for the already-PROMOTED candidate.
    assert repo.get_guardian_authority_heuristic_candidate("cand-1")["promoted_at"] == (
        _NOW.isoformat()
    )

    score, matched_ids = evaluate_heuristics(
        _CO_FIRING_FACTORS, repo.find_guardian_authority_heuristics()
    )
    assert sorted(matched_ids) == ["ga-llm:cand-1", "ga-llm:cand-2"]
    assert score == pytest.approx(0.35)
    assert score <= max(0.4, 0.3)


def test_the_co_firing_cap_counts_the_whole_family_across_target_decision_types(tmp_path):
    """The real heuristics table has no `target_decision_type` column and
    `evaluate_heuristics` reads every row in it, so a PRE_ENTRY_VETO-targeted
    condition and a TIGHTEN_SL-targeted one CAN co-fire on the same factors
    dict. The divisor is therefore the whole live `ga-llm:*` family, not a
    per-target subset."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(
        repo,
        "cand-1",
        condition=_STATE_CONDITION,
        test_correct_rate=0.9,
        target_decision_type="TIGHTEN_SL",
    )
    _seed_validated_candidate(
        repo,
        "cand-2",
        condition=_INSTRUMENT_CONDITION,
        test_correct_rate=0.8,
        target_decision_type="PRE_ENTRY_VETO",
    )

    assert promote_validated_heuristic_candidates(repo, _NOW) == 2

    score, matched_ids = evaluate_heuristics(
        _CO_FIRING_FACTORS, repo.find_guardian_authority_heuristics()
    )
    assert len(matched_ids) == 2
    assert score == pytest.approx(0.35)
    assert score <= max(0.4, 0.3)


def test_a_demoted_heuristic_is_neither_counted_nor_resurrected(tmp_path):
    """Task 6 demotes by marking the candidate row and re-upserting the real
    heuristic with `adjustment=0.0`. A later promotion pass must leave that
    row exactly as demotion left it (never rescale a zero back to a live
    value) and must not count it toward the family size."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_validated_candidate(repo, "cand-1", condition=_STATE_CONDITION, test_correct_rate=0.9)
    promote_validated_heuristic_candidates(repo, _NOW)

    # Exactly what Task 6's demotion does, seeded here directly.
    repo.mark_guardian_authority_heuristic_candidate_demoted(
        "cand-1", _NOW + timedelta(hours=2), "forward performance degraded"
    )
    repo.upsert_guardian_authority_heuristic(
        heuristic_id="ga-llm:cand-1",
        description="cand-1 description",
        condition_json=json.dumps(_STATE_CONDITION),
        adjustment=0.0,
        confidence=0.0,
        sample_size=15,
        updated_at=_NOW + timedelta(hours=2),
    )

    _seed_validated_candidate(
        repo, "cand-2", condition=_INSTRUMENT_CONDITION, test_correct_rate=0.8
    )
    assert promote_validated_heuristic_candidates(repo, _LATER) == 1

    heuristics = _heuristics_by_id(repo)
    assert heuristics["ga-llm:cand-1"]["adjustment"] == 0.0
    assert heuristics["ga-llm:cand-1"]["updated_at"] == (_NOW + timedelta(hours=2)).isoformat()
    # Family size is 1 (the demoted row does not occupy a slot), so the newly
    # promoted heuristic keeps its own full test-split worth.
    assert heuristics["ga-llm:cand-2"]["adjustment"] == pytest.approx(0.3)

    score, _ = evaluate_heuristics(_CO_FIRING_FACTORS, repo.find_guardian_authority_heuristics())
    assert score == pytest.approx(0.3)


# --------------------------------------------------------------------------
# The TIGHTEN_SL cardinality cap (design spec "Addendum (2026-09-16)", R3,
# consequence 1 - added by Task 6, enforced HERE because it is a
# promotion-time decision).
#
# Cap B above dilutes every live `ga-llm:*` heuristic by the family's size. A
# TIGHTEN_SL-targeted member diluted below `authority_tighten_threshold` can
# never fire again, so can never accumulate the `intervention_applied`
# forward sample Task 6's TIGHTEN_SL demotion track needs - an absorbing
# state that only grows. The cap bounds how far that dilution can go by
# refusing further TIGHTEN_SL promotions once
# `_CAP` of them are live; a refused candidate
# stays VALIDATED and becomes eligible again when a demotion frees a slot.
# --------------------------------------------------------------------------
def test_the_cardinality_cap_is_the_documented_value():
    """The one place the production constant and this file's own `_CAP`
    literal are tied together - so a changed cap fails HERE, with a clear
    message, rather than as a confusing cascade of resized fixtures."""
    assert _MAX_LIVE_TIGHTEN_SL_HEURISTICS == _CAP


def _fill_the_tighten_sl_cap(repo, at=_NOW):
    """Promotes exactly `_CAP` TIGHTEN_SL candidates - the cap genuinely
    filled through the real promotion path, not asserted about."""
    for index in range(_CAP):
        _seed_validated_candidate(repo, f"cand-{index}", condition=_STATE_CONDITION)
    assert promote_validated_heuristic_candidates(repo, at) == _CAP
    return {row["heuristic_id"] for row in repo.find_guardian_authority_heuristics()}


def test_a_tighten_sl_promotion_is_refused_once_the_cardinality_cap_is_full(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    already_live = _fill_the_tighten_sl_cap(repo)
    before = _heuristics_by_id(repo)

    _seed_validated_candidate(repo, "one-too-many", condition=_INSTRUMENT_CONDITION)

    assert promote_validated_heuristic_candidates(repo, _LATER) == 0

    # Refusal, not failure: nothing was written to the real table at all -
    # not the refused heuristic, and not a pointless rescale of the live
    # family (whose size did not change).
    assert set(_heuristics_by_id(repo)) == already_live
    assert _heuristics_by_id(repo) == before

    # ...and the candidate is still VALIDATED, queued for a later pass.
    candidate = repo.get_guardian_authority_heuristic_candidate("one-too-many")
    assert candidate["status"] == "VALIDATED"
    assert candidate["promoted_at"] is None
    assert candidate["promoted_heuristic_id"] is None
    still_validated = repo.find_validated_guardian_authority_heuristic_candidates()
    assert [row["candidate_id"] for row in still_validated] == ["one-too-many"]


def test_only_cap_many_tighten_sl_candidates_are_promoted_from_one_oversized_pass(tmp_path):
    """A cold start where more TIGHTEN_SL candidates validate at once than
    the cap allows: exactly `_CAP` are promoted
    (deterministically, the lowest candidate_ids), the rest stay VALIDATED."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for index in range(_CAP + 2):
        _seed_validated_candidate(repo, f"cand-{index}", condition=_STATE_CONDITION)

    assert promote_validated_heuristic_candidates(repo, _NOW) == _CAP

    assert set(_heuristics_by_id(repo)) == {
        f"ga-llm:cand-{index}" for index in range(_CAP)
    }
    assert sorted(
        row["candidate_id"] for row in repo.find_validated_guardian_authority_heuristic_candidates()
    ) == [f"cand-{_CAP}", f"cand-{_CAP + 1}"]


def test_the_tighten_sl_cap_never_blocks_a_pre_entry_veto_promotion(tmp_path):
    """The cap counts and constrains the TIGHTEN_SL track only. A
    PRE_ENTRY_VETO candidate is promoted in the very same pass that refuses
    an over-cap TIGHTEN_SL one - its own forward-tracking (Task 6's
    closed-position track) does not require the heuristic to fire, so it has
    no liveness trap to protect it from."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _fill_the_tighten_sl_cap(repo)
    _seed_validated_candidate(repo, "over-cap-tighten", condition=_INSTRUMENT_CONDITION)
    _seed_validated_candidate(
        repo, "a-veto", condition=_INSTRUMENT_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )

    assert promote_validated_heuristic_candidates(repo, _LATER) == 1

    heuristics = _heuristics_by_id(repo)
    assert "ga-llm:a-veto" in heuristics
    assert "ga-llm:over-cap-tighten" not in heuristics
    assert (
        repo.get_guardian_authority_heuristic_candidate("over-cap-tighten")["status"] == "VALIDATED"
    )
    # The refused candidate never joins the divisor either: the family is the
    # three live TIGHTEN_SL rows plus the one newly promoted veto rule.
    family_size = _CAP + 1
    assert heuristics["ga-llm:a-veto"]["adjustment"] == pytest.approx(0.4 / family_size)
    assert heuristics["ga-llm:cand-0"]["adjustment"] == pytest.approx(0.4 / family_size)


def test_a_pre_entry_veto_family_never_fills_the_tighten_sl_cap(tmp_path):
    """The mirror direction: live PRE_ENTRY_VETO heuristics do not consume
    TIGHTEN_SL slots."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for index in range(_CAP + 2):
        _seed_validated_candidate(
            repo,
            f"veto-{index}",
            condition=_STATE_CONDITION,
            target_decision_type="PRE_ENTRY_VETO",
        )
    assert promote_validated_heuristic_candidates(repo, _NOW) == (
        _CAP + 2
    )

    _seed_validated_candidate(repo, "a-tighten", condition=_INSTRUMENT_CONDITION)
    assert promote_validated_heuristic_candidates(repo, _LATER) == 1
    assert "ga-llm:a-tighten" in _heuristics_by_id(repo)


def test_a_legacy_null_target_decision_type_counts_against_the_tighten_sl_cap(tmp_path):
    """A pre-Task-4B candidate row carries `target_decision_type = NULL` and
    is read as TIGHTEN_SL everywhere else in this pipeline (validation
    routing, Task 6's forward tracking). The cap must read it the same way -
    a NULL that slipped past the cap would be exactly the stuck TIGHTEN_SL
    heuristic the cap exists to prevent."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for index in range(_CAP):
        _seed_validated_candidate(
            repo, f"legacy-{index}", condition=_STATE_CONDITION, target_decision_type=None
        )
    assert promote_validated_heuristic_candidates(repo, _NOW) == _CAP

    _seed_validated_candidate(repo, "one-too-many", condition=_INSTRUMENT_CONDITION)
    assert promote_validated_heuristic_candidates(repo, _LATER) == 0
    assert "ga-llm:one-too-many" not in _heuristics_by_id(repo)


def test_a_demotion_frees_a_tighten_sl_slot_for_the_refused_candidate(tmp_path):
    """The cap is a queue, not a permanent lockout: the refused candidate
    stays VALIDATED and is promoted by the very next pass after a demotion
    frees a slot - which is exactly what makes Task 6's own demotion the
    release valve for this cap."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _fill_the_tighten_sl_cap(repo)
    _seed_validated_candidate(repo, "one-too-many", condition=_INSTRUMENT_CONDITION)
    assert promote_validated_heuristic_candidates(repo, _LATER) == 0

    # Task 6's demotion, in its own binding order: mark, then zero.
    demoted_at = _LATER + timedelta(hours=1)
    assert repo.mark_guardian_authority_heuristic_candidate_demoted(
        "cand-0", demoted_at, "forward performance degraded"
    )
    repo.upsert_guardian_authority_heuristic(
        heuristic_id="ga-llm:cand-0",
        description="cand-0 description",
        condition_json=json.dumps(_STATE_CONDITION),
        adjustment=0.0,
        confidence=0.0,
        sample_size=20,
        updated_at=demoted_at,
    )

    assert promote_validated_heuristic_candidates(repo, demoted_at + timedelta(hours=1)) == 1
    assert "ga-llm:one-too-many" in _heuristics_by_id(repo)
    assert (
        repo.get_guardian_authority_heuristic_candidate("one-too-many")["status"] == "PROMOTED"
    )


def test_the_self_critique_heuristic_family_is_never_touched_by_promotion(tmp_path):
    """`ga-hc:state:*` rows belong to authority.py's own self-critique pass
    (capped at one match per decision by `_groups_for_factors`). Promotion
    neither rescales nor counts them - it only ever writes its own
    `ga-llm:*` family."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.upsert_guardian_authority_heuristic(
        heuristic_id="ga-hc:state:PROTECT",
        description="TIGHTEN_SL outcomes while guardian_state=PROTECT",
        condition_json=json.dumps(_STATE_CONDITION),
        adjustment=0.25,
        confidence=0.5,
        sample_size=40,
        updated_at=_NOW,
    )
    _seed_validated_candidate(
        repo, "cand-1", condition=_INSTRUMENT_CONDITION, test_correct_rate=0.9
    )

    assert promote_validated_heuristic_candidates(repo, _LATER) == 1

    heuristics = _heuristics_by_id(repo)
    assert heuristics["ga-hc:state:PROTECT"]["adjustment"] == pytest.approx(0.25)
    assert heuristics["ga-hc:state:PROTECT"]["updated_at"] == _NOW.isoformat()
    assert heuristics["ga-llm:cand-1"]["adjustment"] == pytest.approx(0.4)
