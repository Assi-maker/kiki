"""Tests for crypto_trading/godfather/priority_boost.py - the GODFATHER
priority-boost scoring/ranking overlay's own, separate propose -> validate
-> promote -> track/demote pipeline (2026-09-18 GODFATHER expansion, closing
the second gap identified alongside the TAKE_PROFIT decision type).

Reuses the SAME closed-position seeding helper
(`_seed_closed_position`/`_evidence`) test_self_improvement_pre_entry_pool.py
already established for Guardian Authority's own PRE_ENTRY_VETO pool tests -
this pipeline's evidence pool is built from the exact same real data (closed
positions + their real candidate records), just with the outcome comparison
flipped (`pnl > 0` instead of `pnl <= 0`)."""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.godfather.priority_boost import (
    _priority_boost_evidence_pool,
    promote_validated_priority_candidates,
    propose_priority_candidates,
    track_and_demote_underperforming_priority_heuristics,
    validate_pending_priority_candidates,
)
from crypto_trading.guardian.self_improvement import _pre_entry_veto_evidence_pool
from crypto_trading.schemas.assessments import (
    GodfatherPriorityStrategistAssessment,
    ProposedPriorityHeuristic,
)
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.guardian.test_self_improvement_pre_entry_pool import (
    _BREAKEVEN_EXIT,
    _LOSS_EXIT,
    _WIN_EXIT,
    _seed_closed_position,
)
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_BASE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
_AGENT_NAME = "crypto-godfather-priority-strategist"


def _assessment(*heuristics, status="ok", run_id="run-1"):
    return GodfatherPriorityStrategistAssessment(
        agent_name=_AGENT_NAME,
        run_id=run_id,
        created_at=_NOW,
        status=status,
        proposed_heuristics=list(heuristics),
    )


def _heuristic(
    description="winners cluster on momentum_breakout above 0.7",
    condition=None,
    adjustment=0.3,
    rationale="18 of 22 such rows closed with pnl > 0.",
):
    return ProposedPriorityHeuristic(
        description=description,
        condition=condition
        or {"trigger_reasons": ["momentum_breakout"], "candidate_score_min": 0.7},
        adjustment=adjustment,
        rationale=rationale,
    )


# --------------------------------------------------------------------------
# Evidence pool: outcome-flip correctness vs. _pre_entry_veto_evidence_pool
# --------------------------------------------------------------------------
def test_priority_boost_pool_flips_the_pre_entry_veto_pools_outcome(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "p-win", _BASE + timedelta(hours=1), _WIN_EXIT)
    _seed_closed_position(repo, "p-loss", _BASE + timedelta(hours=2), _LOSS_EXIT)
    _seed_closed_position(repo, "p-breakeven", _BASE + timedelta(hours=3), _BREAKEVEN_EXIT)

    veto_pool = {row[0]: row[2] for row in _pre_entry_veto_evidence_pool(repo)}
    priority_pool = {row[0]: row[2] for row in _priority_boost_evidence_pool(repo)}

    assert set(veto_pool) == set(priority_pool)
    for closed_at, veto_correct in veto_pool.items():
        assert priority_pool[closed_at] == (not veto_correct)


def test_priority_boost_pool_breakeven_position_is_not_a_win(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "p-breakeven", _BASE, _BREAKEVEN_EXIT)

    pool = _priority_boost_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][2] is False


def test_priority_boost_pool_uses_same_factors_as_pre_entry_factors(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(
        repo, "p-1", _BASE, _WIN_EXIT, instrument="ETHUSDT",
        candidate_score=0.8, trigger_reasons=("funding_extreme",),
    )

    pool = _priority_boost_evidence_pool(repo)

    assert pool[0][1] == {
        "instrument": "ETHUSDT",
        "candidate_score": 0.8,
        "trigger_reasons": ["funding_extreme"],
    }


def test_priority_boost_pool_skips_positions_without_a_candidate_record(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "p-orphan", _BASE, _WIN_EXIT, with_candidate=False)

    assert _priority_boost_evidence_pool(repo) == []


def test_priority_boost_pool_skips_exposure_blocked_zero_size_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "p-blocked", _BASE, _WIN_EXIT, size=Decimal("0"))

    assert _priority_boost_evidence_pool(repo) == []


def test_priority_boost_pool_is_available_at_cold_start_with_zero_heuristics(tmp_path):
    """Same cold-start property Guardian Authority's own PRE_ENTRY_VETO pool
    has (Task 4B): real closed positions exist independent of whether any
    priority-boost heuristic has ever been proposed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(40):
        _seed_closed_position(repo, f"p-{i}", _BASE + timedelta(hours=i), _WIN_EXIT)

    assert repo.find_godfather_priority_heuristics() == []
    assert len(_priority_boost_evidence_pool(repo)) == 40


# --------------------------------------------------------------------------
# PROPOSE
# --------------------------------------------------------------------------
def test_propose_priority_candidates_saves_every_proposed_heuristic(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures={_AGENT_NAME: _assessment(_heuristic(), _heuristic(
        description="second pattern", condition={"instrument": "ETHUSDT"}, adjustment=-0.2,
    ))})

    saved = propose_priority_candidates(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 2
    first = repo.get_godfather_priority_heuristic_candidate("priority-llm:run-1:0")
    assert first["status"] == "PROPOSED"
    assert json.loads(first["condition_json"]) == {
        "trigger_reasons": ["momentum_breakout"], "candidate_score_min": 0.7,
    }
    assert first["proposed_adjustment"] == 0.3


def test_propose_priority_candidates_never_writes_to_guardian_authority_table(tmp_path):
    """The one thing this whole design exists to guarantee: a priority-boost
    proposal never lands in Guardian Authority's own candidates table."""
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    propose_priority_candidates(repo, runner, _settings(), "run-1", _NOW)

    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    assert len(repo.find_proposed_godfather_priority_heuristic_candidates()) == 1


def test_propose_priority_candidates_empty_list_is_success_not_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures={_AGENT_NAME: _assessment()})

    saved = propose_priority_candidates(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert repo.get_godfather_priority_strategist_last_proposed_date() == "2026-09-20"


def test_propose_priority_candidates_at_most_once_per_utc_day(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    first = propose_priority_candidates(repo, runner, _settings(), "run-1", _NOW)
    second = propose_priority_candidates(
        repo, runner, _settings(), "run-2", _NOW + timedelta(hours=1)
    )

    assert first == 1
    assert second == 0


def test_propose_priority_candidates_never_raises_on_a_transport_explosion(tmp_path):
    class _RaisingRunner(MockAgentRunner):
        def __init__(self):
            super().__init__(fixtures={})

        def run(self, agent_def, context, output_schema):
            raise RuntimeError("simulated transport explosion")

    repo = SQLiteRepository(tmp_path / "t.db")

    saved = propose_priority_candidates(repo, _RaisingRunner(), _settings(), "run-1", _NOW)

    assert saved == 0


# --------------------------------------------------------------------------
# VALIDATE
# --------------------------------------------------------------------------
def _seed_winning_pattern(repo, n=30, start=_BASE):
    """n closed positions all matching {"trigger_reasons": ["momentum_breakout"]}
    and all real wins - a genuine, strongly-supported winning pattern."""
    for i in range(n):
        _seed_closed_position(
            repo, f"win-{i}", start + timedelta(hours=i), _WIN_EXIT,
            trigger_reasons=("momentum_breakout",),
        )


def test_validate_pending_priority_candidates_validates_a_genuine_winning_pattern(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    # _MIN_SAMPLE_SIZE=30 must clear on BOTH the 70% train and 30% test
    # splits, so the total pool needs >= ~100 rows (30 / 0.3), not merely
    # >= 30 overall.
    _seed_winning_pattern(repo, n=110)
    repo.save_godfather_priority_heuristic_candidate(
        candidate_id="c-1",
        description="momentum_breakout wins",
        condition_json=json.dumps({"trigger_reasons": ["momentum_breakout"]}),
        proposed_adjustment=0.3,
        rationale="test",
        run_id="run-1",
        proposed_at=_NOW,
    )

    processed = validate_pending_priority_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_godfather_priority_heuristic_candidate("c-1")
    assert row["status"] == "VALIDATED"
    assert row["test_correct_rate"] == 1.0


def test_validate_pending_priority_candidates_rejects_empty_condition(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_winning_pattern(repo, n=40)
    repo.save_godfather_priority_heuristic_candidate(
        candidate_id="c-empty",
        description="always",
        condition_json=json.dumps({}),
        proposed_adjustment=0.3,
        rationale="test",
        run_id="run-1",
        proposed_at=_NOW,
    )

    validate_pending_priority_candidates(repo, _NOW)

    row = repo.get_godfather_priority_heuristic_candidate("c-empty")
    assert row["status"] == "REJECTED"
    assert row["train_sample_size"] == 0


def test_validate_pending_priority_candidates_rejects_too_few_samples(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_winning_pattern(repo, n=4)
    repo.save_godfather_priority_heuristic_candidate(
        candidate_id="c-thin",
        description="thin",
        condition_json=json.dumps({"trigger_reasons": ["momentum_breakout"]}),
        proposed_adjustment=0.3,
        rationale="test",
        run_id="run-1",
        proposed_at=_NOW,
    )

    validate_pending_priority_candidates(repo, _NOW)

    assert repo.get_godfather_priority_heuristic_candidate("c-thin")["status"] == "REJECTED"


def test_validate_pending_priority_candidates_wrong_vocabulary_never_matches(tmp_path):
    """A condition using Guardian Authority's own TIGHTEN_SL vocabulary
    (guardian_state) has no place in this pool at all (its factors are
    instrument/candidate_score/trigger_reasons only) - fail-closed, rejected
    on sample size, exactly like Guardian Authority's own cross-vocabulary
    guard."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_winning_pattern(repo, n=40)
    repo.save_godfather_priority_heuristic_candidate(
        candidate_id="c-wrong-vocab",
        description="wrong vocabulary",
        condition_json=json.dumps({"guardian_state": "PROTECT"}),
        proposed_adjustment=0.3,
        rationale="test",
        run_id="run-1",
        proposed_at=_NOW,
    )

    validate_pending_priority_candidates(repo, _NOW)

    row = repo.get_godfather_priority_heuristic_candidate("c-wrong-vocab")
    assert row["status"] == "REJECTED"
    assert row["train_sample_size"] == 0
    assert row["test_sample_size"] == 0


# --------------------------------------------------------------------------
# PROMOTE + co-firing Cap B (average, never sum)
# --------------------------------------------------------------------------
def _validated_candidate(repo, candidate_id, condition, test_correct_rate=0.85, test_n=40):
    repo.save_godfather_priority_heuristic_candidate(
        candidate_id=candidate_id,
        description=candidate_id,
        condition_json=json.dumps(condition),
        proposed_adjustment=0.3,
        rationale="test",
        run_id="run-1",
        proposed_at=_NOW,
    )
    repo.record_godfather_priority_heuristic_candidate_validation(
        candidate_id=candidate_id,
        status="VALIDATED",
        train_sample_size=test_n,
        train_correct_rate=test_correct_rate,
        test_sample_size=test_n,
        test_correct_rate=test_correct_rate,
        validated_at=_NOW,
    )


def test_promote_validated_priority_candidates_writes_the_live_heuristic(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _validated_candidate(repo, "c-1", {"trigger_reasons": ["momentum_breakout"]})

    promoted = promote_validated_priority_candidates(repo, _NOW)

    assert promoted == 1
    live = repo.find_godfather_priority_heuristics()
    assert len(live) == 1
    assert live[0]["heuristic_id"] == "godfather-priority:c-1"
    assert abs(live[0]["adjustment"] - (0.85 - 0.5)) < 1e-9  # single-member family, divisor 1


def test_promote_validated_priority_candidates_never_writes_guardian_authority_table(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _validated_candidate(repo, "c-1", {"trigger_reasons": ["momentum_breakout"]})

    promote_validated_priority_candidates(repo, _NOW)

    assert repo.find_guardian_authority_heuristics() == []


def test_promote_validated_priority_candidates_averages_not_sums_across_the_family(tmp_path):
    """Cap B: adding a second live member HALVES the first member's own
    stored adjustment (rewritten in the same pass) - the family speaks at
    its mean, never its sum."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _validated_candidate(
        repo, "c-1", {"trigger_reasons": ["momentum_breakout"]}, test_correct_rate=0.9
    )
    promote_validated_priority_candidates(repo, _NOW)
    first_adjustment = repo.find_godfather_priority_heuristics()[0]["adjustment"]

    _validated_candidate(repo, "c-2", {"instrument": "ETHUSDT"}, test_correct_rate=0.9)
    promote_validated_priority_candidates(repo, _NOW + timedelta(hours=1))

    live = {
        row["heuristic_id"]: row["adjustment"]
        for row in repo.find_godfather_priority_heuristics()
    }
    assert len(live) == 2
    for adjustment in live.values():
        assert abs(adjustment - first_adjustment / 2) < 1e-9


def test_promote_validated_priority_candidates_refuses_unusable_condition(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_godfather_priority_heuristic_candidate(
        candidate_id="c-empty",
        description="empty",
        condition_json=json.dumps({}),
        proposed_adjustment=0.3,
        rationale="test",
        run_id="run-1",
        proposed_at=_NOW,
    )
    repo.record_godfather_priority_heuristic_candidate_validation(
        candidate_id="c-empty",
        status="VALIDATED",  # simulates a legacy row that bypassed today's guard
        train_sample_size=40,
        train_correct_rate=0.9,
        test_sample_size=40,
        test_correct_rate=0.9,
        validated_at=_NOW,
    )

    promoted = promote_validated_priority_candidates(repo, _NOW)

    assert promoted == 0
    assert repo.find_godfather_priority_heuristics() == []
    assert repo.get_godfather_priority_heuristic_candidate("c-empty")["status"] == "VALIDATED"


# --------------------------------------------------------------------------
# TRACK / DEMOTE
# --------------------------------------------------------------------------
def test_track_and_demote_demotes_a_heuristic_whose_forward_record_disagrees(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    # Promote a strong-looking candidate at t0...
    _validated_candidate(
        repo, "c-1", {"trigger_reasons": ["momentum_breakout"]}, test_correct_rate=0.9
    )
    promote_validated_priority_candidates(repo, _BASE)

    # ...then seed >= _FORWARD_MIN_SAMPLE_SIZE forward LOSSES matching the
    # SAME condition, all closed AFTER promoted_at.
    for i in range(20):
        _seed_closed_position(
            repo, f"forward-loss-{i}", _BASE + timedelta(hours=i + 1), _LOSS_EXIT,
            trigger_reasons=("momentum_breakout",),
        )

    demoted = track_and_demote_underperforming_priority_heuristics(
        repo, _BASE + timedelta(days=1)
    )

    assert demoted == 1
    row = repo.get_godfather_priority_heuristic_candidate("c-1")
    assert row["demoted_at"] is not None
    live = repo.find_godfather_priority_heuristics()
    assert live[0]["adjustment"] == 0.0
    assert live[0]["confidence"] == 0.0


def test_track_and_demote_leaves_a_well_performing_heuristic_alone(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _validated_candidate(
        repo, "c-1", {"trigger_reasons": ["momentum_breakout"]}, test_correct_rate=0.9
    )
    promote_validated_priority_candidates(repo, _BASE)

    for i in range(20):
        _seed_closed_position(
            repo, f"forward-win-{i}", _BASE + timedelta(hours=i + 1), _WIN_EXIT,
            trigger_reasons=("momentum_breakout",),
        )

    demoted = track_and_demote_underperforming_priority_heuristics(
        repo, _BASE + timedelta(days=1)
    )

    assert demoted == 0
    assert repo.get_godfather_priority_heuristic_candidate("c-1")["demoted_at"] is None


def test_track_and_demote_demotes_on_zero_forward_evidence_after_the_silent_window(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _validated_candidate(repo, "c-1", {"instrument": "ZZZUSDT"}, test_correct_rate=0.9)
    promote_validated_priority_candidates(repo, _BASE)

    demoted = track_and_demote_underperforming_priority_heuristics(
        repo, _BASE + timedelta(days=15)
    )

    assert demoted == 1
    assert repo.get_godfather_priority_heuristic_candidate("c-1")["demoted_at"] is not None


# --------------------------------------------------------------------------
# Full round trip
# --------------------------------------------------------------------------
def test_full_propose_validate_promote_demote_round_trip(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_winning_pattern(repo, n=110)
    runner = MockAgentRunner(fixtures={_AGENT_NAME: _assessment(_heuristic(
        condition={"trigger_reasons": ["momentum_breakout"]}, adjustment=0.3,
    ))})

    saved = propose_priority_candidates(repo, runner, _settings(), "run-1", _NOW)
    validated = validate_pending_priority_candidates(repo, _NOW)
    promoted = promote_validated_priority_candidates(repo, _NOW)

    assert saved == 1
    assert validated == 1
    assert promoted == 1
    live = repo.find_godfather_priority_heuristics()
    assert len(live) == 1
    assert live[0]["adjustment"] > 0  # a genuine winning pattern promotes a positive nudge


# --------------------------------------------------------------------------
# Orphan reconciliation (mirrors Guardian Authority's own I4 fix -
# self_improvement.py::_reconcile_orphan_llm_heuristics /
# test_self_improvement_demotion.py's equivalent tests exactly, for the
# identical crash window: promote_validated_priority_candidates writes the
# real heuristic row BEFORE marking its candidate PROMOTED, so a crash
# between the two writes leaves a live, unowned heuristic row).
# --------------------------------------------------------------------------
_ORPHAN_EVENT = "godfather_priority_orphan_heuristic_zeroed"
_ORPHAN_CONDITION = {"trigger_reasons": ["momentum_breakout"]}


def _seed_orphan_priority_heuristic(
    repo, heuristic_id="godfather-priority:orphan-1", adjustment=0.4
):
    """Exactly what a crash between promotion's two writes leaves behind:
    the real heuristic row, with no PROMOTED candidate referencing it."""
    repo.upsert_godfather_priority_heuristic(
        heuristic_id=heuristic_id,
        description="an orphaned promotion",
        condition_json=json.dumps(_ORPHAN_CONDITION),
        adjustment=adjustment,
        confidence=0.8,
        sample_size=40,
        updated_at=_BASE,
    )
    return heuristic_id


def test_an_orphaned_priority_heuristic_is_zeroed_and_logged(tmp_path, caplog):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_orphan_priority_heuristic(repo)
    assert repo.find_promoted_godfather_priority_heuristic_candidates() == []

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        # Not counted as a demotion: nothing was demoted, an unowned row was
        # silenced.
        assert track_and_demote_underperforming_priority_heuristics(repo, _NOW) == 0

    row = {r["heuristic_id"]: r for r in repo.find_godfather_priority_heuristics()}[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["confidence"] == 0.0
    assert row["updated_at"] == _NOW.isoformat()
    assert _ORPHAN_EVENT in caplog.text
    assert heuristic_id in caplog.text


def test_a_normal_promoted_priority_heuristic_is_never_touched_by_reconciliation(tmp_path):
    """The whole point: a row whose candidate really is PROMOTED is owned,
    measurable and must keep its earned adjustment."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _validated_candidate(repo, "c-1", _ORPHAN_CONDITION, test_correct_rate=0.9)
    promote_validated_priority_candidates(repo, _BASE)
    before = repo.find_godfather_priority_heuristics()[0]

    assert track_and_demote_underperforming_priority_heuristics(repo, _BASE) == 0

    after = repo.find_godfather_priority_heuristics()[0]
    assert after == before


def test_reconciliation_runs_even_with_zero_live_candidates(tmp_path):
    """Orphan reconciliation must run BEFORE the early return for an empty
    live-candidate list - an orphan's defining feature is precisely that no
    candidate references it, so it would never be reachable otherwise."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_orphan_priority_heuristic(repo)

    assert repo.find_promoted_godfather_priority_heuristic_candidates() == []
    assert track_and_demote_underperforming_priority_heuristics(repo, _NOW) == 0

    row = {r["heuristic_id"]: r for r in repo.find_godfather_priority_heuristics()}[heuristic_id]
    assert row["adjustment"] == 0.0


def test_an_already_silent_orphan_priority_heuristic_is_not_rewritten_on_every_pass(
    tmp_path, caplog
):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_orphan_priority_heuristic(repo)
    assert track_and_demote_underperforming_priority_heuristics(repo, _NOW) == 0
    after_first = {r["heuristic_id"]: r for r in repo.find_godfather_priority_heuristics()}[
        heuristic_id
    ]

    later = _NOW + timedelta(days=1)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        assert track_and_demote_underperforming_priority_heuristics(repo, later) == 0
    after_second = {r["heuristic_id"]: r for r in repo.find_godfather_priority_heuristics()}[
        heuristic_id
    ]
    assert after_second == after_first
    assert _ORPHAN_EVENT not in caplog.text


def test_a_demoted_priority_candidates_heuristic_is_not_an_orphan(tmp_path):
    """A demoted candidate keeps `status='PROMOTED'` (audit trail) and still
    references its heuristic id, so its row is owned - already at 0.0
    anyway, but reconciliation must recognize it as accounted-for rather
    than as an unowned row."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _validated_candidate(repo, "c-1", {"instrument": "ZZZUSDT"}, test_correct_rate=0.9)
    promote_validated_priority_candidates(repo, _BASE)
    demoted = track_and_demote_underperforming_priority_heuristics(
        repo, _BASE + timedelta(days=15)
    )
    assert demoted == 1
    after_demotion = repo.find_godfather_priority_heuristics()[0]

    later = _BASE + timedelta(days=16)
    assert track_and_demote_underperforming_priority_heuristics(repo, later) == 0
    assert repo.find_godfather_priority_heuristics()[0] == after_demotion
