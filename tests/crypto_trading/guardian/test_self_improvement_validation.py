"""Tests for Task 4 of docs/superpowers/sdd/2026-09-15-guardian-authority-
live-autonomy/ - the VALIDATE step of the propose -> validate -> promote ->
track/demote pipeline
(crypto_trading/guardian/self_improvement.py::validate_pending_heuristic_
candidates).

Every fixture here builds the evidence pool directly through the repository
(`seed_guardian_authority_shadow`/`decide_guardian_authority_shadow`/
`resolve_guardian_authority_shadow_decided` for shadow rows;
`save_guardian_observation`/`save_guardian_authority_decision`/
`resolve_guardian_authority_decision` for real rows) - there is no AI call
anywhere in this file, `validate_pending_heuristic_candidates` makes none.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.guardian.self_improvement import (
    _MIN_MISCALIBRATION,
    _MIN_SAMPLE_SIZE,
    _split_pool_chronologically,
    _tighten_sl_evidence_pool,
    validate_pending_heuristic_candidates,
)
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_BASE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------
def _seed_candidate(repo, candidate_id="cand-1", condition=None, adjustment=0.2):
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id=candidate_id,
        description="a candidate heuristic under validation",
        condition_json=json.dumps(condition if condition is not None else {"guardian_state": "PROTECT"}),
        proposed_adjustment=adjustment,
        rationale="seeded directly for a Task 4 test",
        run_id="run-llm",
        proposed_at=_NOW,
    )


def _seed_real_tighten_sl(
    repo, position_id, decided_at, expectation_correct, factors, intervention_applied=True
):
    """A resolved real TIGHTEN_SL decision PLUS the guardian_observations row
    whose observed_at is exactly the decision's decided_at - the exact-join
    authority.py::_reconstruct_tighten_sl_factors relies on."""
    guardian_state = factors.get("guardian_state", "PROTECT")
    non_state_factors = {k: v for k, v in factors.items() if k != "guardian_state"}
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id=f"{position_id}:{decided_at.isoformat()}",
            position_id=position_id,
            observed_at=decided_at,
            state=guardian_state,
            decay_score=Decimal("0.5"),
            progress_ratio=Decimal("0.2"),
            unrealized_pnl=Decimal("-1"),
            factors=non_state_factors,
            run_id="run-0",
        )
    )
    decision_id = f"ga:tighten:{position_id}:{decided_at.isoformat()}"
    repo.save_guardian_authority_decision(
        decision_id=decision_id,
        position_id=position_id,
        candidate_id=position_id,
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
        matched_heuristic_ids_json=json.dumps([]),
    )
    repo.resolve_guardian_authority_decision(
        decision_id,
        "stop_loss",
        "-1",
        expectation_correct,
        decided_at + timedelta(minutes=5),
    )
    return decision_id


def _seed_shadow_tighten_sl(repo, shadow_id, decided_at, expectation_correct, factors):
    repo.seed_guardian_authority_shadow(
        shadow_id=shadow_id,
        position_id=shadow_id,
        candidate_id=shadow_id,
        instrument="BTCUSDT",
        opened_at=decided_at - timedelta(hours=1),
        created_at=decided_at - timedelta(hours=1),
        run_id="run-0",
    )
    repo.decide_guardian_authority_shadow(
        shadow_id=shadow_id,
        decision="TIGHTEN_SL",
        decided_at=decided_at,
        expected_outcome="seeded",
        expected_direction="favorable",
        confidence=0.6,
        factors_json=json.dumps(factors),
        proposed_new_sl=Decimal("95"),
        updated_at=decided_at,
    )
    repo.resolve_guardian_authority_shadow_decided(
        shadow_id=shadow_id,
        actual_exit_reason="stop_loss",
        actual_pnl_usdt=Decimal("-1"),
        actual_closed_at=decided_at + timedelta(minutes=5),
        expectation_correct=expectation_correct,
        prediction_error=0.5,
        updated_at=decided_at + timedelta(minutes=5),
    )


# --------------------------------------------------------------------------
# Nothing to do
# --------------------------------------------------------------------------
def test_validate_pending_heuristic_candidates_returns_zero_when_nothing_proposed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert validate_pending_heuristic_candidates(repo, _NOW) == 0


# --------------------------------------------------------------------------
# Happy path: a genuine, consistent pattern in BOTH splits validates
# --------------------------------------------------------------------------
def test_validate_pending_heuristic_candidates_validates_a_consistent_pattern_in_both_splits(
    tmp_path,
):
    """100 rows, all matching the candidate's condition, chronologically
    ordered. 80% correct throughout (uniform pattern), so the 70/30 split
    (70 train, 30 test) sees the SAME 0.8 correct_rate on both sides -
    genuine out-of-sample agreement."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, condition={"guardian_state": "PROTECT"})

    for i in range(100):
        decided_at = _BASE + timedelta(minutes=i)
        correct = i % 5 != 0  # wrong on i=0,5,10,... -> 20 wrong, 80 correct = 0.8
        _seed_real_tighten_sl(
            repo,
            position_id=f"real-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "VALIDATED"
    assert row["rejected_reason"] is None
    assert row["train_sample_size"] == 70
    assert row["train_correct_rate"] == 56 / 70
    assert row["test_sample_size"] == 30
    assert row["test_correct_rate"] == 24 / 30
    assert row["validated_at"] == _NOW.isoformat()


# --------------------------------------------------------------------------
# Acceptance Criterion 4's own required proof: a pattern that looks real on
# train alone but reverses on the genuinely held-out test split must be
# REJECTED, not promoted.
# --------------------------------------------------------------------------
def test_validate_pending_heuristic_candidates_rejects_when_the_pattern_reverses_on_test(
    tmp_path,
):
    """Two differently-signed clusters, split unevenly by the 70/30 cutoff:
    the first (chronologically oldest) 70 rows - which land entirely in
    TRAIN - are 90% correct (a strong positive pattern); the last
    (chronologically newest) 30 rows - which land entirely in TEST - are
    90% WRONG (a strong negative pattern). A train-only validation would
    wrongly promote this as a well-calibrated, positively-adjusted rule;
    the genuinely held-out test split correctly rejects it instead."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, condition={"guardian_state": "PROTECT"})

    for i in range(70):
        decided_at = _BASE + timedelta(minutes=i)
        correct = i % 10 != 0  # wrong on 0,10,...,60 -> 7 wrong, 63 correct = 0.9
        _seed_real_tighten_sl(
            repo,
            position_id=f"train-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )
    for i in range(30):
        decided_at = _BASE + timedelta(minutes=70 + i)
        correct = i in (0, 10, 20)  # 3 correct, 27 wrong = 0.1
        _seed_real_tighten_sl(
            repo,
            position_id=f"test-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "REJECTED"
    assert row["train_sample_size"] == 70
    assert row["train_correct_rate"] == 63 / 70
    assert row["test_sample_size"] == 30
    assert row["test_correct_rate"] == 3 / 30
    assert "sign disagreement" in row["rejected_reason"]


# --------------------------------------------------------------------------
# Insufficient total sample size (even before splitting) - rejects with the
# right reason.
# --------------------------------------------------------------------------
def test_validate_pending_heuristic_candidates_rejects_for_insufficient_total_sample_size(
    tmp_path,
):
    """Only 20 rows total, all matching, all correct. 70% of 20 is 14, which
    is below _MIN_SAMPLE_SIZE (30) even though the pattern would otherwise
    be perfectly calibrated - the reason must name the train-sample-size
    check, checked first."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, condition={"guardian_state": "PROTECT"})

    for i in range(20):
        decided_at = _BASE + timedelta(minutes=i)
        _seed_real_tighten_sl(
            repo,
            position_id=f"small-{i:04d}",
            decided_at=decided_at,
            expectation_correct=True,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "REJECTED"
    assert row["train_sample_size"] == 14
    assert "too few train samples" in row["rejected_reason"]
    assert f"n=14 < {_MIN_SAMPLE_SIZE}" in row["rejected_reason"]


def test_validate_pending_heuristic_candidates_rejects_for_insufficient_test_sample_size(
    tmp_path,
):
    """Enough total rows for train (>=30) but the candidate's own condition
    only matches a handful of them in the TEST half - proves the test-count
    check is independently enforced, not just the train one."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, condition={"momentum_decay_min": 0.8})

    # 70 rows in what will become the train window, all matching.
    for i in range(70):
        decided_at = _BASE + timedelta(minutes=i)
        _seed_real_tighten_sl(
            repo,
            position_id=f"train-{i:04d}",
            decided_at=decided_at,
            expectation_correct=True,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )
    # 30 rows in what will become the test window, only 5 of which match
    # the candidate's momentum_decay_min condition.
    for i in range(30):
        decided_at = _BASE + timedelta(minutes=70 + i)
        matching = i < 5
        _seed_real_tighten_sl(
            repo,
            position_id=f"test-{i:04d}",
            decided_at=decided_at,
            expectation_correct=True,
            factors={
                "guardian_state": "PROTECT",
                "momentum_decay": 0.9 if matching else 0.3,
            },
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["status"] == "REJECTED"
    assert row["train_sample_size"] == 70
    assert row["test_sample_size"] == 5
    assert "too few test samples" in row["rejected_reason"]
    assert f"n=5 < {_MIN_SAMPLE_SIZE}" in row["rejected_reason"]


# --------------------------------------------------------------------------
# heuristic_condition_matches is called with the candidate's own,
# unmodified condition_json - proved via a real numeric _min bound, not a
# simplified stand-in.
# --------------------------------------------------------------------------
def test_validate_pending_heuristic_candidates_reuses_real_min_max_matching_semantics(tmp_path):
    """The candidate's condition uses the `_min` suffix
    (`momentum_decay_min`) - a numeric lower-bound requirement per
    authority.py's own documented matching semantics, NOT a literal
    dict-key lookup for a factor literally named "momentum_decay_min"
    (which does not exist in any seeded factors dict). If this function
    used anything other than the real, unmodified `heuristic_condition_
    matches`, one of two wrong things would happen: either the _min
    semantics would be ignored entirely (a naive equality/membership check
    against a nonexistent key -> zero rows would ever match -> both splits
    would fail on sample size, never reaching VALIDATED), or the numeric
    bound would be applied incorrectly. Here, 40 rows have momentum_decay
    below the 0.8 bound (must be EXCLUDED) and 60 have momentum_decay at or
    above it (must be INCLUDED) - the assertion on the exact resulting
    sample sizes (30 train / 30 test, not 0/0 and not 70/30) is the proof."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, condition={"momentum_decay_min": 0.8})

    # Chronologically oldest 40 rows: below the bound, excluded from the
    # matched sample entirely (their expectation_correct is deliberately
    # the OPPOSITE of the matched rows' pattern below - if they were
    # wrongly included, the computed correct_rate would be very different
    # from the asserted 0.8).
    for i in range(40):
        decided_at = _BASE + timedelta(minutes=i)
        _seed_real_tighten_sl(
            repo,
            position_id=f"below-{i:04d}",
            decided_at=decided_at,
            expectation_correct=False,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.3},
        )
    # Next 60 rows (chronologically newest): at/above the bound, must be
    # included. 80% correct throughout -> consistent in both splits.
    for i in range(60):
        decided_at = _BASE + timedelta(minutes=40 + i)
        correct = i % 5 != 0  # 12 wrong, 48 correct = 0.8
        _seed_real_tighten_sl(
            repo,
            position_id=f"above-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    # Total pool = 100 rows -> train = rows[0:70] (40 below-bound + 30
    # above-bound, of which only the 30 above-bound rows match) -> matched
    # train sample_size = 30. test = rows[70:100], all above-bound -> 30.
    assert row["train_sample_size"] == 30
    assert row["test_sample_size"] == 30
    assert row["status"] == "VALIDATED"
    assert row["train_correct_rate"] == row["test_correct_rate"] == 0.8


# --------------------------------------------------------------------------
# The pool genuinely combines shadow + real resolved TIGHTEN_SL rows.
# --------------------------------------------------------------------------
def test_tighten_sl_evidence_pool_combines_resolved_shadow_and_real_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_tighten_sl(
        repo,
        position_id="real-1",
        decided_at=_BASE,
        expectation_correct=True,
        factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
    )
    _seed_shadow_tighten_sl(
        repo,
        shadow_id="shadow-1",
        decided_at=_BASE + timedelta(minutes=1),
        expectation_correct=False,
        factors={"guardian_state": "WATCH", "momentum_decay": 0.4},
    )

    pool = _tighten_sl_evidence_pool(repo)

    assert len(pool) == 2
    factors_by_state = {row[1]["guardian_state"]: row for row in pool}
    assert factors_by_state["PROTECT"][2] is True
    assert factors_by_state["WATCH"][2] is False


def test_validate_pending_heuristic_candidates_test_split_counts_shadow_rows(tmp_path):
    """A candidate condition matches everything; the pool's chronologically
    NEWEST 30 rows (which become the test split) are ALL shadow rows. If
    shadow rows were not included in the pool, test_sample_size would be 0,
    not 30."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, condition={"guardian_state": "PROTECT"})

    for i in range(50):
        decided_at = _BASE + timedelta(minutes=i)
        correct = i % 5 != 0
        _seed_real_tighten_sl(
            repo,
            position_id=f"real-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )
    for i in range(50):
        decided_at = _BASE + timedelta(minutes=50 + i)
        correct = i % 5 != 0
        _seed_shadow_tighten_sl(
            repo,
            shadow_id=f"shadow-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert row["train_sample_size"] == 70
    assert row["test_sample_size"] == 30
    assert row["status"] == "VALIDATED"


# --------------------------------------------------------------------------
# Exclusion rules on the pool itself (matches Task 8/9's own scope notes).
# --------------------------------------------------------------------------
def test_tighten_sl_evidence_pool_excludes_non_tighten_sl_and_unresolved_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    # Real TIGHTEN_SL, resolved, included.
    _seed_real_tighten_sl(
        repo,
        position_id="included",
        decided_at=_BASE,
        expectation_correct=True,
        factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
    )
    # Real TIGHTEN_SL without a genuine intervention - excluded (Task 9's
    # own I2 filter).
    _seed_real_tighten_sl(
        repo,
        position_id="no-intervention",
        decided_at=_BASE + timedelta(minutes=1),
        expectation_correct=True,
        factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        intervention_applied=False,
    )
    # A CLOSE_EARLY decision always resolves with expectation_correct=None
    # (Task 8's own ruling) - simulate that directly rather than going
    # through the whole close-early code path.
    repo.save_guardian_authority_decision(
        decision_id="ga:close:excluded",
        position_id="excluded-close",
        candidate_id="excluded-close",
        decision_type="CLOSE_EARLY",
        decided_at=_BASE + timedelta(minutes=2),
        reasoning="seeded",
        expected_outcome="seeded",
        expected_direction="unfavorable",
        confidence=0.6,
        run_id="run-0",
        intervention_applied=True,
        matched_heuristic_ids_json=json.dumps([]),
    )
    repo.resolve_guardian_authority_decision(
        "ga:close:excluded", "guardian_exit", "5", None, _BASE + timedelta(minutes=10)
    )
    # A shadow row still OBSERVING (never resolved) - excluded by the
    # status='RESOLVED' filter on find_resolved_guardian_authority_shadows.
    repo.seed_guardian_authority_shadow(
        shadow_id="observing",
        position_id="observing",
        candidate_id="observing",
        instrument="BTCUSDT",
        opened_at=_BASE,
        created_at=_BASE,
        run_id="run-0",
    )

    pool = _tighten_sl_evidence_pool(repo)

    assert len(pool) == 1
    decided_at, factors, correct = pool[0]
    assert factors["guardian_state"] == "PROTECT"
    assert correct is True


# --------------------------------------------------------------------------
# The chronological split helper, in isolation.
# --------------------------------------------------------------------------
def test_split_pool_chronologically_sorts_and_splits_70_30():
    pool = [
        (f"2026-09-01T00:{i:02d}:00+00:00", {"i": i}, True) for i in range(10)
    ]
    # shuffled input order - the function must sort before splitting.
    shuffled = [pool[5], pool[0], pool[9], pool[2], pool[7], pool[1], pool[8], pool[3], pool[6], pool[4]]

    train, test = _split_pool_chronologically(shuffled)

    assert len(train) == 7
    assert len(test) == 3
    assert [row[1]["i"] for row in train] == [0, 1, 2, 3, 4, 5, 6]
    assert [row[1]["i"] for row in test] == [7, 8, 9]


# --------------------------------------------------------------------------
# Idempotency: a row already transitioned is never reprocessed.
# --------------------------------------------------------------------------
def test_validate_pending_heuristic_candidates_does_not_reprocess_an_already_transitioned_row(
    tmp_path,
):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, condition={"guardian_state": "PROTECT"})
    for i in range(100):
        decided_at = _BASE + timedelta(minutes=i)
        correct = i % 5 != 0
        _seed_real_tighten_sl(
            repo,
            position_id=f"real-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )
    assert validate_pending_heuristic_candidates(repo, _NOW) == 1
    before = repo.get_guardian_authority_heuristic_candidate("cand-1")

    later = _NOW + timedelta(hours=1)
    processed_again = validate_pending_heuristic_candidates(repo, later)

    assert processed_again == 0
    after = repo.get_guardian_authority_heuristic_candidate("cand-1")
    assert after == before  # untouched by the second call


# --------------------------------------------------------------------------
# Multiple independent candidates in one call.
# --------------------------------------------------------------------------
def test_validate_pending_heuristic_candidates_processes_each_candidate_independently(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate(repo, candidate_id="matches-nothing", condition={"guardian_state": "EXIT"})
    _seed_candidate(repo, candidate_id="matches-everything", condition={})

    for i in range(100):
        decided_at = _BASE + timedelta(minutes=i)
        correct = i % 5 != 0
        _seed_real_tighten_sl(
            repo,
            position_id=f"real-{i:04d}",
            decided_at=decided_at,
            expectation_correct=correct,
            factors={"guardian_state": "PROTECT", "momentum_decay": 0.9},
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 2
    nothing_row = repo.get_guardian_authority_heuristic_candidate("matches-nothing")
    everything_row = repo.get_guardian_authority_heuristic_candidate("matches-everything")
    assert nothing_row["status"] == "REJECTED"
    assert nothing_row["train_sample_size"] == 0
    assert "too few train samples" in nothing_row["rejected_reason"]
    assert everything_row["status"] == "VALIDATED"
    assert everything_row["train_sample_size"] == 70
