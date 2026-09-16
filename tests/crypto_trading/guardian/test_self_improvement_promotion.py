"""Tests for Task 5 of docs/superpowers/sdd/2026-09-15-guardian-authority-
live-autonomy/ - the PROMOTE step of the propose -> validate -> promote ->
track/demote pipeline (crypto_trading/guardian/self_improvement.py::
promote_validated_heuristic_candidates), including the co-firing cap the
design spec's "Addendum (2026-09-16)" R3 requires.

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
from crypto_trading.guardian.self_improvement import promote_validated_heuristic_candidates
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(days=1)
_VALIDATED_AT = _NOW - timedelta(hours=1)

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
