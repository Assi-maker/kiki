"""Tests for Task 6 of docs/superpowers/sdd/2026-09-15-guardian-authority-
live-autonomy/ - the TRACK/DEMOTE step of the propose -> validate -> promote
-> track/demote pipeline (crypto_trading/guardian/self_improvement.py::
track_and_demote_underperforming_heuristics), plus the two 2026-09-16 addenda
it owns directly: the PRE_ENTRY_VETO forward-tracking track and the binding
demote-before-zero write ordering.

(The third addendum - the TIGHTEN_SL cardinality cap - is enforced at
PROMOTION time and is therefore tested in test_self_improvement_promotion.py,
next to the function that owns it.)

Like Task 5's own tests, everything here is end-to-end against the real
SQLite repository: every fixture is seeded through the same repository
methods the trading pipeline itself writes with, and every outcome is read
back through the existing, unmodified `repo.find_guardian_authority_
heuristics()` / `repo.get_guardian_authority_heuristic_candidate()` and fed
to the existing, unmodified `evaluate_heuristics` - never by inspecting an
internal value the production code happened to compute.

There is no AI call anywhere in this file;
`track_and_demote_underperforming_heuristics` makes none.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from crypto_trading.guardian.authority import evaluate_heuristics
from crypto_trading.guardian.self_improvement import (
    _FORWARD_MAX_SILENT_DAYS,
    _days_since_promotion,
    promote_validated_heuristic_candidates,
    track_and_demote_underperforming_heuristics,
)
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.guardian.test_self_improvement_pre_entry_pool import (
    _LOSS_EXIT,
    _WIN_EXIT,
    _seed_closed_position,
)

# One coherent timeline for every fixture in this file: a candidate proposed
# and validated in early September, promoted on the 10th, and tracked on the
# 20th. "Forward" means strictly after _PROMOTED_AT, and every "before"
# fixture row sits in the _BEFORE window on purpose.
_BEFORE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
_PROMOTED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
# Strictly after _PROMOTED_AT: a decision made in the same instant as the
# promotion was not made BY the promoted heuristic, and the window is a
# strict `>` - pinned on its own by the boundary test below.
_FORWARD_START = _PROMOTED_AT + timedelta(minutes=1)

_STATE_CONDITION = {"guardian_state": "PROTECT"}
_INSTRUMENT_CONDITION = {"instrument": "BTCUSDT"}
_VETO_CONDITION = {"trigger_reasons": ["momentum_breakout"]}
_CO_FIRING_FACTORS = {"guardian_state": "PROTECT", "instrument": "BTCUSDT"}


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------
def _seed_promoted_heuristic(
    repo,
    candidate_id="cand-1",
    condition=None,
    target_decision_type="TIGHTEN_SL",
    test_correct_rate=0.9,
    test_sample_size=40,
    promoted_at=_PROMOTED_AT,
):
    """A candidate carried all the way to PROMOTED through the REAL pipeline
    writes - `save_...` (Task 3's own write), `record_..._validation` (Task
    4's own transition) and `promote_validated_heuristic_candidates` (Task
    5's own function, which is what actually writes the `ga-llm:*` row into
    the real heuristics table). Nothing about the promoted state is
    hand-faked, so what this task's demotion later reads is exactly what
    promotion really leaves behind."""
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id=candidate_id,
        description=f"{candidate_id} description",
        condition_json=json.dumps(condition if condition is not None else _STATE_CONDITION),
        proposed_adjustment=0.95,
        rationale=f"{candidate_id} rationale",
        run_id="run-llm",
        proposed_at=_BEFORE,
        target_decision_type=target_decision_type,
    )
    repo.record_guardian_authority_heuristic_candidate_validation(
        candidate_id=candidate_id,
        status="VALIDATED",
        train_sample_size=90,
        train_correct_rate=0.85,
        test_sample_size=test_sample_size,
        test_correct_rate=test_correct_rate,
        validated_at=_BEFORE + timedelta(hours=1),
        rejected_reason=None,
    )
    promote_validated_heuristic_candidates(repo, promoted_at)
    return f"ga-llm:{candidate_id}"


def _seed_forward_tighten_sl_decision(
    repo,
    decision_id,
    decided_at,
    expectation_correct,
    matched_heuristic_ids,
    intervention_applied=True,
):
    """A resolved real TIGHTEN_SL decision carrying the
    `matched_heuristic_ids_json` attribution this task's forward tracking
    reads. No `guardian_observations` row is needed here (unlike Task 4's
    own pool seeding): forward attribution is RECORDED at decision time, not
    re-derived from factors, which is the whole point of that column."""
    repo.save_guardian_authority_decision(
        decision_id=decision_id,
        position_id=f"pos-{decision_id}",
        candidate_id=f"cand-{decision_id}",
        decision_type="TIGHTEN_SL",
        decided_at=decided_at,
        reasoning="seeded",
        expected_outcome="seeded",
        expected_direction="favorable",
        confidence=0.6,
        run_id="run-0",
        old_sl="90",
        new_sl="95",
        intervention_applied=intervention_applied,
        matched_heuristic_ids_json=json.dumps(list(matched_heuristic_ids)),
    )
    repo.resolve_guardian_authority_decision(
        decision_id,
        "stop_loss",
        "-1",
        expectation_correct,
        decided_at + timedelta(minutes=5),
    )


def _seed_forward_tighten_sl_record(
    repo,
    heuristic_id,
    count,
    correct_count,
    first_decided_at,
    prefix="fwd",
    intervention_applied=True,
):
    """`count` resolved TIGHTEN_SL decisions attributed to `heuristic_id`,
    `correct_count` of which had a correct expectation."""
    for index in range(count):
        _seed_forward_tighten_sl_decision(
            repo,
            f"{prefix}-{index:04d}",
            first_decided_at + timedelta(minutes=index),
            index < correct_count,
            [heuristic_id],
            intervention_applied=intervention_applied,
        )


def _heuristics_by_id(repo) -> dict:
    return {row["heuristic_id"]: row for row in repo.find_guardian_authority_heuristics()}


# --------------------------------------------------------------------------
# TIGHTEN_SL track
# --------------------------------------------------------------------------
def test_a_promoted_tighten_sl_heuristic_with_a_poor_forward_record_is_demoted(tmp_path):
    """20 real, resolved, genuinely-applied TIGHTEN_SL interventions
    attributed to this heuristic since it was promoted; only 5 of them
    turned out correct (0.25, well under 0.4). The heuristic is retired:
    `adjustment` becomes exactly 0.0 in the real table, which
    `evaluate_heuristics` treats as a genuine no-op."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)

    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=20, correct_count=5, first_decided_at=_FORWARD_START
    )
    # Noise: real forward interventions that matched a DIFFERENT heuristic.
    # All wrong, and numerous enough to swamp the tally if attribution were
    # ignored - they must not touch this heuristic's own record.
    _seed_forward_tighten_sl_record(
        repo,
        "ga-llm:someone-else",
        count=40,
        correct_count=0,
        first_decided_at=_PROMOTED_AT + timedelta(days=1),
        prefix="other",
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1

    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["confidence"] == 0.0
    assert row["sample_size"] == 20  # the FORWARD sample that retired it
    assert row["updated_at"] == _NOW.isoformat()
    # The row is kept on file, verbatim, never deleted.
    assert row["description"] == "cand-1 description"
    assert json.loads(row["condition_json"]) == _STATE_CONDITION

    # ...and it is genuinely inert for the real decision core: the condition
    # still MATCHES (the cap is not achieved by making it miss), it just
    # contributes nothing at all to the summed score.
    score, matched_ids = evaluate_heuristics(
        _CO_FIRING_FACTORS, repo.find_guardian_authority_heuristics()
    )
    assert matched_ids == [heuristic_id]
    assert score == 0.0

    candidate = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert candidate["status"] == "PROMOTED"  # full audit trail, never reverted
    assert candidate["demoted_at"] == _NOW.isoformat()
    assert "0.2500" in candidate["demotion_reason"]
    assert "n=20" in candidate["demotion_reason"]
    assert "TIGHTEN_SL" in candidate["demotion_reason"]


def test_forward_decisions_without_intervention_applied_never_count(tmp_path):
    """The R1 fix, isolated: 30 repeated-tick decisions for the SAME position
    that never produced a real intervention, all resolving wrong, plus 5
    genuine interventions that also resolved wrong. Counting the repeated
    ticks would give n=35 at correct_rate 0.0 - comfortably past both bars.
    Excluding them leaves n=5, under the sample-size floor, so nothing is
    demoted: the `intervention_applied` filter is the ONLY thing standing
    between demoted and not demoted here."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)

    # One losing position sitting above the tighten threshold, re-deciding
    # every tick - the exact shape R1 describes.
    for index in range(30):
        _seed_forward_tighten_sl_decision(
            repo,
            f"tick-{index:04d}",
            _FORWARD_START + timedelta(minutes=index),
            False,
            [heuristic_id],
            intervention_applied=False,
        )
    _seed_forward_tighten_sl_record(
        repo,
        heuristic_id,
        count=5,
        correct_count=0,
        first_decided_at=_PROMOTED_AT + timedelta(days=1),
        prefix="applied",
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0

    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)
    assert repo.get_guardian_authority_heuristic_candidate("cand-1")["demoted_at"] is None


def test_tighten_sl_decisions_from_before_promotion_are_never_counted(tmp_path):
    """Forward-only means a heuristic's OWN track record. 15 genuine forward
    interventions at correct_rate 0.6 keep it alive; 25 pre-promotion rows at
    correct_rate 0.0 would drag the combined rate to 9/40 = 0.225 over n=40
    and demote it. Including them would flip the verdict - so they must not
    be included."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)

    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=25, correct_count=0, first_decided_at=_BEFORE, prefix="past"
    )
    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=15, correct_count=9, first_decided_at=_FORWARD_START
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0

    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)
    assert repo.get_guardian_authority_heuristic_candidate("cand-1")["demoted_at"] is None


def test_a_decision_decided_in_the_very_instant_of_promotion_is_not_forward(tmp_path):
    """The window is a strict `>`: a decision whose `decided_at` equals
    `promoted_at` exactly was not made BY the promoted heuristic (the
    promotion write and that tick's decision are the same instant). 15
    forward rows all wrong would demote; one of them landing exactly on the
    boundary leaves only 14 and it survives."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)

    _seed_forward_tighten_sl_decision(repo, "boundary", _PROMOTED_AT, False, [heuristic_id])
    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=14, correct_count=0, first_decided_at=_FORWARD_START
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)


def test_a_promoted_tighten_sl_heuristic_with_a_good_forward_record_is_untouched(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)
    before = _heuristics_by_id(repo)[heuristic_id]

    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=40, correct_count=32, first_decided_at=_FORWARD_START
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == before
    assert repo.get_guardian_authority_heuristic_candidate("cand-1")["demoted_at"] is None


def test_the_forward_sample_size_floor_is_genuinely_enforced(tmp_path):
    """14 forward interventions, every one of them wrong, is NOT enough - the
    canary threshold is a real bar, not a formality. The 15th, identical row
    is what tips it over."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)

    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=14, correct_count=0, first_decided_at=_FORWARD_START
    )
    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)

    _seed_forward_tighten_sl_decision(
        repo, "fwd-0014", _FORWARD_START + timedelta(minutes=14), False, [heuristic_id]
    )
    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == 0.0
    assert _heuristics_by_id(repo)[heuristic_id]["sample_size"] == 15


def test_the_forward_bar_is_an_adverse_deviation_of_0_10_from_the_baseline(tmp_path):
    """The bar is "the forward record has moved 0.10 against this heuristic's
    own direction", not "not perfect". Pinned tightly, and deliberately NOT
    on the knife edge: for a positive-adjustment heuristic 41/100 = 0.41 (a
    deviation of -0.09) survives and 39/100 = 0.39 (-0.11) does not, which
    brackets the bar at 0.10 to within 0.01 without depending on how a
    deviation of exactly -0.10 rounds in IEEE-754."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)

    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=100, correct_count=41, first_decided_at=_FORWARD_START
    )
    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)

    # `fwd-0039`/`fwd-0040` were among the 41 correct rows; re-resolving two
    # of them wrong takes the rate to 39/100 without changing the sample size.
    for decision_id in ("fwd-0039", "fwd-0040"):
        repo.resolve_guardian_authority_decision(
            decision_id, "stop_loss", "-1", False, _PROMOTED_AT + timedelta(hours=5)
        )
    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == 0.0
    assert _heuristics_by_id(repo)[heuristic_id]["sample_size"] == 100


# --------------------------------------------------------------------------
# Direction-awareness (controller ruling, 2026-09-16)
#
# A promoted heuristic's stored adjustment can legitimately be NEGATIVE: a
# candidate whose held-out split said "under this condition the action was
# usually the WRONG call" is promoted with a negative adjustment, i.e. a rule
# that pushes AWAY from the action. For such a rule a LOW forward correct_rate
# is CONFIRMING evidence, not disconfirming - the flat `correct_rate < 0.4`
# bar this replaced would have retired it for continuing to be right.
#
# Each pair below is deliberately built so that the ONLY difference from an
# existing positive-adjustment fixture above is the promoted direction, and
# the verdict flips with it.
# --------------------------------------------------------------------------
def test_a_negative_adjustment_tighten_sl_heuristic_is_not_demoted_for_being_right(tmp_path):
    """Promoted at test_correct_rate 0.1 (adjustment -0.4): "tightening under
    this condition is usually the wrong call". Its forward record is the
    SAME 20-interventions-at-0.25 fixture that demotes the positive-adjustment
    heuristic at the top of this file - and here it must NOT demote, because
    0.25 is exactly what a rule arguing against tightening predicted. Under
    the old flat `correct_rate < 0.4` bar this heuristic was retired for being
    right; that is the bug this test pins closed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "cand-1", condition=_STATE_CONDITION, test_correct_rate=0.1
    )
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(-0.4)

    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=20, correct_count=5, first_decided_at=_FORWARD_START
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(-0.4)
    assert repo.get_guardian_authority_heuristic_candidate("cand-1")["demoted_at"] is None


def test_a_negative_adjustment_tighten_sl_heuristic_is_demoted_when_forward_evidence_reverses(
    tmp_path,
):
    """The same heuristic, genuinely contradicted: 16 of 20 forward
    interventions it co-fired on turned out CORRECT (0.8), i.e. tightening
    under this condition is now usually right - the opposite of what the rule
    argues. That is real disconfirmation and it is retired."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "cand-1", condition=_STATE_CONDITION, test_correct_rate=0.1
    )

    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=20, correct_count=16, first_decided_at=_FORWARD_START
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1
    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["sample_size"] == 20
    candidate = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert candidate["demoted_at"] == _NOW.isoformat()
    assert "-0.4000" in candidate["demotion_reason"]  # the direction it contradicted


def test_a_negative_adjustment_pre_entry_veto_heuristic_is_not_demoted_for_being_right(tmp_path):
    """Promoted at test_correct_rate 0.1 (adjustment -0.4): "positions
    matching this condition usually WIN, so do not veto here". The forward
    fixture is byte-for-byte the one that demotes the positive-adjustment veto
    heuristic above (20 forward positions, 16 profitable -> veto-correct 0.2);
    the only thing that differs is the promoted direction, and the verdict
    flips with it."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo,
        "veto-1",
        condition=_VETO_CONDITION,
        target_decision_type="PRE_ENTRY_VETO",
        test_correct_rate=0.1,
    )
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(-0.4)

    for index in range(20):
        _seed_closed_position(
            repo,
            f"fwd-{index:04d}",
            _PROMOTED_AT + timedelta(minutes=index + 1),
            _LOSS_EXIT if index < 4 else _WIN_EXIT,
        )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(-0.4)
    assert repo.get_guardian_authority_heuristic_candidate("veto-1")["demoted_at"] is None


def test_a_negative_adjustment_pre_entry_veto_heuristic_is_demoted_when_forward_evidence_reverses(
    tmp_path,
):
    """The same "do not veto here" heuristic, genuinely contradicted: 16 of
    20 forward positions matching its condition actually LOST money, so a veto
    would have been correct 0.8 of the time - the opposite of what the rule
    argues. Retired, via the closed-position pool and not the
    resolved-decisions mechanism (asserted)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo,
        "veto-1",
        condition=_VETO_CONDITION,
        target_decision_type="PRE_ENTRY_VETO",
        test_correct_rate=0.1,
    )

    for index in range(20):
        _seed_closed_position(
            repo,
            f"fwd-{index:04d}",
            _PROMOTED_AT + timedelta(minutes=index + 1),
            _WIN_EXIT if index < 4 else _LOSS_EXIT,
        )

    assert repo.find_resolved_guardian_authority_decisions() == []

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1
    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["sample_size"] == 20
    candidate = repo.get_guardian_authority_heuristic_candidate("veto-1")
    assert candidate["demoted_at"] == _NOW.isoformat()
    assert "PRE_ENTRY_VETO" in candidate["demotion_reason"]
    assert "-0.4000" in candidate["demotion_reason"]


def test_an_already_demoted_candidate_is_never_reprocessed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)
    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=20, correct_count=5, first_decided_at=_FORWARD_START
    )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1
    after_first = _heuristics_by_id(repo)
    first_candidate = repo.get_guardian_authority_heuristic_candidate("cand-1")

    later = _NOW + timedelta(days=1)
    assert track_and_demote_underperforming_heuristics(repo, later) == 0
    # Neither write ran a second time: the heuristic row's updated_at and the
    # candidate's demoted_at both still carry the FIRST demotion's timestamp.
    assert _heuristics_by_id(repo) == after_first
    assert repo.get_guardian_authority_heuristic_candidate("cand-1") == first_candidate


def test_nothing_happens_when_no_candidate_has_been_promoted(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert repo.find_guardian_authority_heuristics() == []


# --------------------------------------------------------------------------
# PRE_ENTRY_VETO track (2026-09-16 addendum)
#
# `resolve_pending_decisions` permanently skips resolving a PRE_ENTRY_VETO
# decision's counterfactual by the original Guardian Authority plan's own
# documented scope limit, so a promoted PRE_ENTRY_VETO heuristic's real fired
# decisions can NEVER appear in `find_resolved_guardian_authority_decisions()`.
# Every test below therefore asserts that table is EMPTY - the demotion it
# proves cannot have come from the resolved-decisions mechanism.
# --------------------------------------------------------------------------
def test_a_promoted_pre_entry_veto_heuristic_with_a_poor_forward_record_is_demoted(tmp_path):
    """20 real positions closed since promotion whose entry evidence matches
    this heuristic's own condition; 16 of them actually MADE money, so a veto
    would have been correct only 4/20 = 0.2 of the time. The heuristic is
    vetoing profitable entries and is retired."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo,
        "veto-1",
        condition=_VETO_CONDITION,
        target_decision_type="PRE_ENTRY_VETO",
        test_correct_rate=0.8,
    )
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.3)

    for index in range(20):
        _seed_closed_position(
            repo,
            f"fwd-{index:04d}",
            _PROMOTED_AT + timedelta(minutes=index + 1),
            _LOSS_EXIT if index < 4 else _WIN_EXIT,
        )

    assert repo.find_resolved_guardian_authority_decisions() == []

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1

    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["confidence"] == 0.0
    assert row["sample_size"] == 20
    candidate = repo.get_guardian_authority_heuristic_candidate("veto-1")
    assert candidate["status"] == "PROMOTED"
    assert candidate["demoted_at"] == _NOW.isoformat()
    assert "0.2000" in candidate["demotion_reason"]
    assert "n=20" in candidate["demotion_reason"]
    assert "PRE_ENTRY_VETO" in candidate["demotion_reason"]

    score, matched_ids = evaluate_heuristics(
        {"trigger_reasons": ["momentum_breakout"]}, repo.find_guardian_authority_heuristics()
    )
    assert matched_ids == [heuristic_id]
    assert score == 0.0


def test_positions_closed_before_promotion_are_never_counted_on_the_veto_track(tmp_path):
    """The same flip-the-verdict construction as the TIGHTEN_SL track: 15
    forward positions at a 0.6 veto-correct rate keep the heuristic alive,
    while 25 pre-promotion positions at 0.0 would drag the combined rate to
    9/40 = 0.225 over n=40 and demote it."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "veto-1", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )

    for index in range(25):
        _seed_closed_position(
            repo, f"past-{index:04d}", _BEFORE + timedelta(minutes=index), _WIN_EXIT
        )
    for index in range(15):
        _seed_closed_position(
            repo,
            f"fwd-{index:04d}",
            _PROMOTED_AT + timedelta(minutes=index + 1),
            _LOSS_EXIT if index < 9 else _WIN_EXIT,
        )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)
    assert repo.get_guardian_authority_heuristic_candidate("veto-1")["demoted_at"] is None


def test_the_veto_track_only_counts_positions_matching_the_heuristics_own_condition(tmp_path):
    """The heuristic's OWN condition, re-applied forward through the real,
    unmodified `heuristic_condition_matches` - exactly mirroring what
    validation did retrospectively. 30 forward positions the condition does
    not describe, all profitable, must not contribute; the 8 that DO match
    leave the sample under the floor, so nothing is demoted."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "veto-1", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )

    for index in range(30):
        _seed_closed_position(
            repo,
            f"other-{index:04d}",
            _PROMOTED_AT + timedelta(minutes=index + 1),
            _WIN_EXIT,
            trigger_reasons=("volume_spike",),
        )
    for index in range(8):
        _seed_closed_position(
            repo, f"match-{index:04d}", _PROMOTED_AT + timedelta(hours=index + 1), _WIN_EXIT
        )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)


def test_a_promoted_pre_entry_veto_heuristic_with_a_good_forward_record_is_untouched(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "veto-1", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )
    before = _heuristics_by_id(repo)[heuristic_id]

    for index in range(20):
        _seed_closed_position(
            repo,
            f"fwd-{index:04d}",
            _PROMOTED_AT + timedelta(minutes=index + 1),
            _LOSS_EXIT if index < 16 else _WIN_EXIT,
        )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == before
    assert repo.get_guardian_authority_heuristic_candidate("veto-1")["demoted_at"] is None


def test_each_track_reads_only_its_own_evidence(tmp_path):
    """A TIGHTEN_SL-targeted heuristic is never judged on closed-position
    PnL, and a PRE_ENTRY_VETO-targeted one is never judged on resolved
    TIGHTEN_SL decisions - the same "untested stays untested" routing Task 4B
    established for validation, applied forward. Both heuristics share one
    condition, so the only thing separating them is
    `target_decision_type`."""
    repo = SQLiteRepository(tmp_path / "t.db")
    tighten_id = _seed_promoted_heuristic(
        repo, "cand-tighten", condition=_VETO_CONDITION, target_decision_type="TIGHTEN_SL"
    )
    veto_id = _seed_promoted_heuristic(
        repo, "cand-veto", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )

    # Damning evidence on the veto track ONLY (20 matching forward positions,
    # every one profitable -> veto-correct rate 0.0).
    for index in range(20):
        _seed_closed_position(
            repo, f"fwd-{index:04d}", _PROMOTED_AT + timedelta(minutes=index + 1), _WIN_EXIT
        )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1

    heuristics = _heuristics_by_id(repo)
    assert heuristics[veto_id]["adjustment"] == 0.0
    assert heuristics[tighten_id]["adjustment"] != 0.0
    assert repo.get_guardian_authority_heuristic_candidate("cand-tighten")["demoted_at"] is None


# --------------------------------------------------------------------------
# The binding demote-before-zero write ordering (2026-09-16 addendum)
#
# The two demotion writes are separately committed - there is no shared
# transaction, same as every other write pair against this table. Task 5's
# promotion pass rescales every live (`demoted_at IS NULL`) `ga-llm:*`
# heuristic. If a promotion pass interleaves BETWEEN the two demotion writes,
# the order decides whether the demoted heuristic stays retired or is
# resurrected at a nonzero adjustment it can never be rescaled out of again.
# --------------------------------------------------------------------------
def _promotion_pass_after_every_demotion_write(repo, at):
    """Runs a real promotion pass immediately after EVERY write a demotion
    makes - both of them - so a concurrent promotion genuinely lands in the
    window between the two, whichever of them the production code happens to
    perform first.

    Hooking both writes rather than one is what makes this a real test of the
    ORDER: a hook that knew which write came first would move with the
    production code and could never detect the forbidden order. This one
    cannot tell them apart, so it fails loudly if they are swapped (verified
    by mutation: reversing the two writes in `track_and_demote_
    underperforming_heuristics` makes this test fail on a resurrected 0.2).

    The re-entrancy flag exists because the promotion pass itself writes
    through `upsert_guardian_authority_heuristic`, which is one of the hooked
    methods - without it the hook would recurse into itself."""
    real_mark = repo.mark_guardian_authority_heuristic_candidate_demoted
    real_upsert = repo.upsert_guardian_authority_heuristic
    passes = []
    running = []

    def interleave_a_promotion_pass():
        if running:
            return  # the promotion pass's own writes must not re-trigger it
        running.append(True)
        try:
            passes.append(promote_validated_heuristic_candidates(repo, at))
        finally:
            running.clear()

    def hooked_mark(*args, **kwargs):
        result = real_mark(*args, **kwargs)
        interleave_a_promotion_pass()
        return result

    def hooked_upsert(*args, **kwargs):
        result = real_upsert(*args, **kwargs)
        interleave_a_promotion_pass()
        return result

    repo.mark_guardian_authority_heuristic_candidate_demoted = hooked_mark
    repo.upsert_guardian_authority_heuristic = hooked_upsert
    return passes


def _seed_second_validated_candidate(repo, candidate_id="cand-2"):
    """A VALIDATED candidate waiting to be promoted - without one, a
    promotion pass has nothing to do and would not rescale anything, so the
    interleaving below would prove nothing."""
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id=candidate_id,
        description=f"{candidate_id} description",
        condition_json=json.dumps(_INSTRUMENT_CONDITION),
        proposed_adjustment=0.5,
        rationale=f"{candidate_id} rationale",
        run_id="run-llm",
        proposed_at=_BEFORE,
        target_decision_type="PRE_ENTRY_VETO",
    )
    repo.record_guardian_authority_heuristic_candidate_validation(
        candidate_id=candidate_id,
        status="VALIDATED",
        train_sample_size=90,
        train_correct_rate=0.85,
        test_sample_size=40,
        test_correct_rate=0.8,
        validated_at=_BEFORE + timedelta(hours=1),
        rejected_reason=None,
    )


def test_a_promotion_pass_interleaved_between_the_two_demotion_writes_cannot_resurrect_it(
    tmp_path,
):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)
    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=20, correct_count=5, first_decided_at=_FORWARD_START
    )
    _seed_second_validated_candidate(repo)

    interleaved_promotions = _promotion_pass_after_every_demotion_write(repo, _NOW)

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1

    # The interleaving genuinely happened: two real promotion passes ran
    # inside the demotion's own window, and the first of them really did
    # promote a candidate (the second had nothing left to promote).
    assert interleaved_promotions == [1, 0]
    heuristics = _heuristics_by_id(repo)
    assert "ga-llm:cand-2" in heuristics

    # And the demoted heuristic is still exactly 0.0 - never rescaled back to
    # life by that pass, because `demoted_at` was already set when it ran.
    assert heuristics[heuristic_id]["adjustment"] == 0.0
    assert heuristics[heuristic_id]["updated_at"] == _NOW.isoformat()
    # The promoted newcomer saw a family of exactly ONE (itself): the demoted
    # row never occupied a divisor slot.
    assert heuristics["ga-llm:cand-2"]["adjustment"] == pytest.approx(0.3)


def test_the_reverse_write_order_would_have_resurrected_the_demoted_heuristic(tmp_path):
    """Why the ordering rule is binding, demonstrated rather than asserted:
    the SAME interleaving, with the two writes performed by hand in the
    FORBIDDEN order (zero first, mark second). The promotion pass sees
    `demoted_at IS NULL`, counts the row as live, and rescales its 0.0 back
    to a nonzero value - which the subsequent mark then freezes in place
    forever, since every later promotion pass will skip it as demoted. This
    test does NOT call the production function; it exists to prove the
    scenario the production function's ordering avoids is real."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)
    _seed_second_validated_candidate(repo)
    candidate = repo.get_guardian_authority_heuristic_candidate("cand-1")

    # 1. The zeroing upsert FIRST (the forbidden order).
    repo.upsert_guardian_authority_heuristic(
        heuristic_id=heuristic_id,
        description=candidate["description"],
        condition_json=candidate["condition_json"],
        adjustment=0.0,
        confidence=0.0,
        sample_size=20,
        updated_at=_NOW,
    )
    # 2. A promotion pass interleaves here.
    assert promote_validated_heuristic_candidates(repo, _NOW) == 1
    # 3. ...and only now is the row marked demoted.
    assert repo.mark_guardian_authority_heuristic_candidate_demoted(
        "cand-1", _NOW, "forward performance degraded"
    )

    # The damage: a demoted heuristic left live at a nonzero adjustment.
    resurrected = _heuristics_by_id(repo)[heuristic_id]
    assert resurrected["adjustment"] != 0.0
    # Its own full 0.4: it is the only live member of its OWN (TIGHTEN_SL)
    # family, and the interleaved promotion is PRE_ENTRY_VETO-targeted, which
    # since review finding I1 (2026-09-17) no longer shares its divisor.
    assert resurrected["adjustment"] == pytest.approx(0.4)

    # And it is now permanently stuck there: every later promotion pass skips
    # it, because it IS demoted.
    _seed_second_validated_candidate(repo, "cand-3")
    promote_validated_heuristic_candidates(repo, _NOW + timedelta(days=1))
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)


# --------------------------------------------------------------------------
# I2 (final whole-branch review, 2026-09-17): the time-based "no forward
# evidence" demotion path.
#
# A PRE_ENTRY_VETO heuristic strong enough to actually veto DESTROYS its own
# forward evidence: every entry it correctly blocks is a position that never
# opens, never closes, and therefore never enters
# `_pre_entry_veto_evidence_pool`. Its forward sample size can never reach
# `_FORWARD_MIN_SAMPLE_SIZE`, so before this fix it could never be demoted at
# all - the mirror image of the TIGHTEN_SL track's own absorbing state, and
# the more dangerous of the two, since a veto rule acts on every real entry.
#
# The fix is symmetric in spirit to the canary sample-size floor but measured
# in TIME: a promoted heuristic with ZERO forward samples for longer than
# `_FORWARD_MAX_SILENT_DAYS` is demoted on a "no evidence of continued value"
# basis. It applies to BOTH tracks, and only when the forward sample is
# exactly zero - a heuristic with any forward evidence at all, however little,
# is governed by the existing sample-size/adverse-deviation rule instead.
# --------------------------------------------------------------------------
_PAST_THE_SILENCE_BAR = _PROMOTED_AT + timedelta(days=_FORWARD_MAX_SILENT_DAYS, minutes=1)
_JUST_INSIDE_THE_SILENCE_BAR = _PROMOTED_AT + timedelta(days=_FORWARD_MAX_SILENT_DAYS, minutes=-1)


def test_a_promoted_veto_heuristic_with_zero_forward_samples_is_demoted_after_the_silence_bar(
    tmp_path,
):
    """The I2 case exactly: a veto rule that has been live past the bar with
    not one matching closed position to show for it. Whether that is because
    it is successfully blocking every such entry or because its condition
    describes nothing that happens any more is UNKNOWABLE from here - and
    that is the point. An un-measurable rule acting on real capital is
    retired, and can be re-earned by a fresh candidate if the pattern is
    real."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "veto-1", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )

    assert track_and_demote_underperforming_heuristics(repo, _PAST_THE_SILENCE_BAR) == 1

    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["confidence"] == 0.0
    assert row["sample_size"] == 0
    candidate = repo.get_guardian_authority_heuristic_candidate("veto-1")
    assert candidate["demoted_at"] == _PAST_THE_SILENCE_BAR.isoformat()
    assert "no forward evidence" in candidate["demotion_reason"]
    assert str(_FORWARD_MAX_SILENT_DAYS) in candidate["demotion_reason"]

    # Genuinely silent for the real decision core afterwards.
    score, matched_ids = evaluate_heuristics(
        {"trigger_reasons": ["momentum_breakout"]}, repo.find_guardian_authority_heuristics()
    )
    assert matched_ids == [heuristic_id]
    assert score == 0.0


def test_a_promoted_tighten_sl_heuristic_with_zero_forward_samples_is_demoted_too(tmp_path):
    """The same path on the other track - the TIGHTEN_SL member diluted below
    `authority_tighten_threshold` that can never fire again is the mirror
    case, and gets the same release valve."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)

    assert track_and_demote_underperforming_heuristics(repo, _PAST_THE_SILENCE_BAR) == 1
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == 0.0
    assert "TIGHTEN_SL" in (
        repo.get_guardian_authority_heuristic_candidate("cand-1")["demotion_reason"]
    )


def test_zero_forward_samples_within_the_silence_bar_is_untouched(tmp_path):
    """One minute on the safe side of the bar: nothing happens at all. The
    bar is deliberately generous - a young heuristic must be given a real
    chance to accumulate evidence before it is judged for not having any."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "veto-1", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )
    before = _heuristics_by_id(repo)[heuristic_id]

    assert track_and_demote_underperforming_heuristics(repo, _JUST_INSIDE_THE_SILENCE_BAR) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == before
    assert repo.get_guardian_authority_heuristic_candidate("veto-1")["demoted_at"] is None


def test_a_nonzero_forward_sample_is_never_subject_to_the_time_based_rule(tmp_path):
    """The precise boundary between the two rules, so they cannot silently
    overlap: ONE single forward sample - far below `_FORWARD_MIN_SAMPLE_SIZE`,
    and adverse (a veto that would have been wrong) - is enough to take this
    heuristic out of the time-based rule's scope entirely, no matter how long
    it has been live. It is then governed by the sample-size rule, which its
    n=1 record does not clear, so it survives. Without the `sample_size == 0`
    restriction this fixture would demote on a single trade."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "veto-1", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )
    before = _heuristics_by_id(repo)[heuristic_id]
    _seed_closed_position(repo, "fwd-0000", _PROMOTED_AT + timedelta(minutes=1), _WIN_EXIT)

    # Ten times the silence bar, and still untouched.
    much_later = _PROMOTED_AT + timedelta(days=_FORWARD_MAX_SILENT_DAYS * 10)
    assert track_and_demote_underperforming_heuristics(repo, much_later) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == before
    assert repo.get_guardian_authority_heuristic_candidate("veto-1")["demoted_at"] is None


def test_the_time_based_rule_reads_only_forward_samples(tmp_path):
    """Pre-promotion evidence does not count as "evidence of continued
    value": 20 matching positions closed BEFORE promotion leave the forward
    sample at zero, so the silence bar still fires."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(
        repo, "veto-1", condition=_VETO_CONDITION, target_decision_type="PRE_ENTRY_VETO"
    )
    for index in range(20):
        _seed_closed_position(
            repo, f"past-{index:04d}", _BEFORE + timedelta(minutes=index), _LOSS_EXIT
        )

    assert track_and_demote_underperforming_heuristics(repo, _PAST_THE_SILENCE_BAR) == 1
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == 0.0


def test_an_unparseable_promoted_at_never_triggers_the_time_based_rule(tmp_path):
    """Defensive: the silence bar is computed from `now - promoted_at`, and a
    promoted_at this function cannot parse gives no measurable silence at all.
    Demoting on an unmeasurable record is the one thing this step must not do
    (the same stance as the existing `not promoted_at` skip)."""
    assert _days_since_promotion("not a timestamp", _PAST_THE_SILENCE_BAR) is None
    assert _days_since_promotion("", _PAST_THE_SILENCE_BAR) is None
    # ...and a real one is measured, in days, both timestamps being the
    # tz-aware ISO-8601 strings this pipeline actually writes.
    measured = _days_since_promotion(_PROMOTED_AT.isoformat(), _PAST_THE_SILENCE_BAR)
    assert measured == pytest.approx(_FORWARD_MAX_SILENT_DAYS + 1 / (24 * 60))


# --------------------------------------------------------------------------
# I4 (final whole-branch review, 2026-09-17): orphan reconciliation.
#
# `promote_validated_heuristic_candidates` writes the real heuristic row
# BEFORE it marks the candidate PROMOTED (the opposite order to demotion's
# own binding mark-then-zero, and for this table the only possible order -
# the candidate row has to record the heuristic id the write produced). The
# two writes are separately committed, so a crash between them leaves a LIVE
# `ga-llm:*` heuristic in the real table whose candidate is still VALIDATED:
# invisible to `_live_promoted_llm_candidates`, therefore excluded from Cap
# B's divisor AND from every demotion sweep - a heuristic acting on real
# capital that no part of this pipeline can see or retire.
#
# Reconciliation zeroes such a row (adjustment 0.0, confidence 0.0) and logs
# a distinctly-named event. It is self-healing in the benign interleaving: if
# the promotion that produced the orphan does eventually mark its candidate
# PROMOTED, the next promotion pass rescales the row back to its earned
# value, because the row is live and not demoted.
# --------------------------------------------------------------------------
_ORPHAN_EVENT = "ga_llm_orphan_heuristic_zeroed"


def _seed_orphan_llm_heuristic(repo, heuristic_id="ga-llm:orphan-1", adjustment=0.4):
    """Exactly what a crash between promotion's two writes leaves behind: the
    real heuristic row, with no PROMOTED candidate referencing it."""
    repo.upsert_guardian_authority_heuristic(
        heuristic_id=heuristic_id,
        description="an orphaned promotion",
        condition_json=json.dumps(_STATE_CONDITION),
        adjustment=adjustment,
        confidence=0.8,
        sample_size=40,
        updated_at=_BEFORE,
    )
    return heuristic_id


def test_an_orphaned_llm_heuristic_is_zeroed_and_logged(tmp_path, caplog):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_orphan_llm_heuristic(repo)
    assert repo.find_promoted_guardian_authority_heuristic_candidates() == []

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        # Not counted as a demotion: nothing was demoted, an unowned row was
        # silenced.
        assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0

    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["confidence"] == 0.0
    assert row["updated_at"] == _NOW.isoformat()
    assert _ORPHAN_EVENT in caplog.text
    assert heuristic_id in caplog.text

    # And it is genuinely a no-op for the real decision core.
    score, matched_ids = evaluate_heuristics(
        _CO_FIRING_FACTORS, repo.find_guardian_authority_heuristics()
    )
    assert matched_ids == [heuristic_id]
    assert score == 0.0


def test_a_normal_promoted_heuristic_is_never_touched_by_reconciliation(tmp_path):
    """The whole point: a row whose candidate really is PROMOTED is owned,
    measurable and must keep its earned adjustment."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)
    before = _heuristics_by_id(repo)[heuristic_id]

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == before


def test_reconciliation_runs_even_when_no_candidate_is_currently_live(tmp_path):
    """The orphan case's defining feature is that there IS no live promoted
    candidate to iterate over - so the reconciliation must run before (not
    inside) the loop over live candidates."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_orphan_llm_heuristic(repo)

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == 0.0


def test_reconciliation_never_touches_the_self_critique_family(tmp_path):
    """`ga-hc:state:*` rows belong to authority.py's own self-critique pass
    and have no candidate row by design - they are not orphans, and this
    module must never write to them."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.upsert_guardian_authority_heuristic(
        heuristic_id="ga-hc:state:PROTECT",
        description="TIGHTEN_SL outcomes while guardian_state=PROTECT",
        condition_json=json.dumps(_STATE_CONDITION),
        adjustment=0.25,
        confidence=0.5,
        sample_size=40,
        updated_at=_BEFORE,
    )
    before = _heuristics_by_id(repo)["ga-hc:state:PROTECT"]

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)["ga-hc:state:PROTECT"] == before


def test_an_already_silent_orphan_is_not_rewritten_on_every_pass(tmp_path, caplog):
    """Idempotence, and log hygiene: once an orphan is at 0.0/0.0 there is
    nothing acting on capital any more, so later passes leave it completely
    alone rather than re-writing (and re-logging) it every tick forever."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_orphan_llm_heuristic(repo)
    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    after_first = _heuristics_by_id(repo)[heuristic_id]

    later = _NOW + timedelta(days=1)
    caplog.clear()  # only the SECOND pass's own log output is in scope here
    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        assert track_and_demote_underperforming_heuristics(repo, later) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == after_first
    assert _ORPHAN_EVENT not in caplog.text


def test_a_demoted_candidates_heuristic_is_not_an_orphan(tmp_path):
    """A demoted candidate keeps `status='PROMOTED'` (audit trail) and still
    references its heuristic id, so its row is owned - it is already at 0.0
    anyway, but the reconciliation must recognize it as accounted-for rather
    than as an unowned row."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_heuristic(repo, "cand-1", condition=_STATE_CONDITION)
    _seed_forward_tighten_sl_record(
        repo, heuristic_id, count=20, correct_count=4, first_decided_at=_FORWARD_START
    )
    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1
    after_demotion = _heuristics_by_id(repo)[heuristic_id]

    later = _NOW + timedelta(days=1)
    assert track_and_demote_underperforming_heuristics(repo, later) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == after_demotion
