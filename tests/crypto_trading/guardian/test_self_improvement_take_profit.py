"""Tests for the TAKE_PROFIT target_decision_type added to Guardian
Authority's self-improvement pipeline (propose -> validate -> promote ->
track/demote), alongside the existing, LIVE TIGHTEN_SL/PRE_ENTRY_VETO
target types.

Mirrors test_self_improvement_pre_entry_pool.py's/test_self_improvement_
promotion.py's/test_self_improvement_demotion.py's own conventions exactly:
every fixture is seeded through the real repository methods the trading
pipeline itself writes with, and every outcome is read back through the
existing, unmodified repository readers and `evaluate_heuristics` - never
by inspecting an internal value the production code happened to compute.

Like TAKE_PROFIT's sibling PRE_ENTRY_VETO pool, `_take_profit_evidence_pool`
is available at cold start (real, already-persisted `guardian_observations`
rows on real closed positions, independent of whether any TAKE_PROFIT
heuristic has ever existed) - the cold-start test below proves this the
same way test_self_improvement_pre_entry_pool.py's own cold-start test
proves it for PRE_ENTRY_VETO.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.guardian.authority import evaluate_heuristics
from crypto_trading.guardian.self_improvement import (
    _take_profit_evidence_pool,
    promote_validated_heuristic_candidates,
    track_and_demote_underperforming_heuristics,
    validate_pending_heuristic_candidates,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_BEFORE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
_PROMOTED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

# compute_pnl = size * (exit - entry) / entry - fees - funding
#             = 1000 * (exit - 100) / 100 (fees=funding=0)
_ENTRY = Decimal("100")
_SIZE = Decimal("1000")
_TAKE_PROFIT_CONDITION = {"progress_ratio_min": 0.5}


def _seed_closed_position_with_observations(
    repo,
    position_id,
    closed_at,
    exit_price,
    observations,  # list of (observed_at, progress_ratio, unrealized_pnl)
    size=_SIZE,
):
    """One REAL closed position plus its real guardian_observations rows -
    no candidate row is needed (unlike the PRE_ENTRY_VETO pool):
    `_take_profit_evidence_pool` reads only observations + the position's
    own realized PnL, never candidate evidence."""
    opened_at = closed_at - timedelta(hours=2)
    repo.create_position_with_event(
        Position(
            position_id=position_id, candidate_id=position_id, instrument="BTCUSDT",
            direction="LONG", status="OPEN_POSITION", theoretical_entry=_ENTRY,
            simulated_fill_entry=_ENTRY, stop_loss=Decimal("90"), target=Decimal("120"),
            size=size, fill_model_version="v1", opened_at=opened_at,
        ),
        Event(
            event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
            run_id="run-0", schema_version=1, payload={},
        ),
    )
    for index, (observed_at, progress_ratio, unrealized_pnl) in enumerate(observations):
        repo.save_guardian_observation(
            GuardianObservation(
                observation_id=f"obs-{position_id}-{index}", position_id=position_id,
                observed_at=observed_at, state="HOLD", decay_score=Decimal("0.1"),
                progress_ratio=Decimal(str(progress_ratio)),
                unrealized_pnl=Decimal(str(unrealized_pnl)),
                factors={"time_decay": 0.0, "momentum_decay": 0.0, "volume_decay": 0.0,
                         "funding_decay": 0.0, "secondary_confirmation_lost": 0.0,
                         "market_regime": 0.0},
                run_id="run-0",
            )
        )
    repo.close_position_with_event(
        position_id, exit_price, exit_price,
        "stop_loss" if exit_price < _ENTRY else "target",
        Decimal("0"), Decimal("0"), closed_at,
        Event(
            event_id=f"POSITION_CLOSED:{position_id}", event_type="POSITION_CLOSED",
            aggregate_type="position", aggregate_id=position_id, occurred_at=closed_at,
            run_id="run-0", schema_version=1, payload={},
        ),
    )


def _seed_take_profit_candidate(repo, candidate_id="tp-1", condition=None, adjustment=-0.2):
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id=candidate_id,
        description="a TAKE_PROFIT-shaped candidate under validation",
        condition_json=json.dumps(condition if condition is not None else _TAKE_PROFIT_CONDITION),
        proposed_adjustment=adjustment,
        rationale="seeded directly for a TAKE_PROFIT test",
        run_id="run-llm",
        proposed_at=_NOW,
        target_decision_type="TAKE_PROFIT",
    )


def _heuristics_by_id(repo) -> dict:
    return {row["heuristic_id"]: row for row in repo.find_guardian_authority_heuristics()}


# ---------------------------------------------------------------------------
# _take_profit_evidence_pool - the pool builder itself
# ---------------------------------------------------------------------------


def test_take_profit_pool_builds_tuples_from_real_observations_and_real_eventual_pnl(tmp_path):
    """One closed position, eventual pnl=+10 (exit=101). Two observations:
    one whose own unrealized_pnl (+15) exceeds the eventual +10 (closing
    THERE would have captured more -> correct=True), one whose own
    unrealized_pnl (+5) is below it (closing there would have captured LESS
    -> correct=False)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    t0 = _BEFORE
    t1 = _BEFORE + timedelta(minutes=30)
    _seed_closed_position_with_observations(
        repo, "pos-1", _BEFORE + timedelta(hours=1), Decimal("101"),
        observations=[(t0, 0.8, "15"), (t1, 0.4, "5")],
    )

    pool = _take_profit_evidence_pool(repo)

    by_timestamp = {row[0]: row for row in pool}
    assert len(pool) == 2
    assert by_timestamp[t0.isoformat()] == (
        t0.isoformat(), {"progress_ratio": 0.8, "unrealized_pnl_positive": True}, True,
    )
    assert by_timestamp[t1.isoformat()] == (
        t1.isoformat(), {"progress_ratio": 0.4, "unrealized_pnl_positive": True}, False,
    )


def test_take_profit_pool_unrealized_pnl_positive_is_false_for_a_negative_observation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    t0 = _BEFORE
    _seed_closed_position_with_observations(
        repo, "pos-neg", _BEFORE + timedelta(hours=1), Decimal("99"),  # eventual pnl = -10
        observations=[(t0, 0.1, "-5")],
    )

    pool = _take_profit_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][1] == {"progress_ratio": 0.1, "unrealized_pnl_positive": False}
    # -5 > -10 (the eventual, worse loss) -> closing there would have been better
    assert pool[0][2] is True


def test_take_profit_pool_skips_a_closed_row_without_a_closed_at(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position_with_observations(
        repo, "undated", _BEFORE, Decimal("101"), observations=[(_BEFORE, 0.5, "5")],
    )
    _seed_closed_position_with_observations(
        repo, "dated", _BEFORE + timedelta(minutes=1), Decimal("101"),
        observations=[(_BEFORE + timedelta(minutes=1), 0.5, "5")],
    )
    repo._conn.execute("UPDATE positions SET closed_at = NULL WHERE position_id = 'undated'")
    repo._conn.commit()

    pool = _take_profit_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][0] == (_BEFORE + timedelta(minutes=1)).isoformat()


def test_take_profit_pool_excludes_exposure_blocked_zero_size_positions(tmp_path):
    """Same 2026-09-03 ruling _pre_entry_veto_evidence_pool already applies:
    a zero-size position's compute_pnl is 0 - fees - funding, which would
    systematically bias this pool's own comparison for reasons unrelated to
    real market behavior."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position_with_observations(
        repo, "blocked", _BEFORE, Decimal("101"), observations=[(_BEFORE, 0.9, "50")],
        size=Decimal("0"),
    )
    _seed_closed_position_with_observations(
        repo, "real", _BEFORE + timedelta(minutes=1), Decimal("101"),
        observations=[(_BEFORE + timedelta(minutes=1), 0.5, "5")],
    )

    pool = _take_profit_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][0] == (_BEFORE + timedelta(minutes=1)).isoformat()


def test_take_profit_pool_ignores_still_open_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position_with_observations(
        repo, "closed", _BEFORE, Decimal("101"), observations=[(_BEFORE, 0.5, "5")],
    )
    repo.create_position_with_event(
        Position(
            position_id="still-open", candidate_id="closed", instrument="BTCUSDT",
            direction="LONG", status="OPEN_POSITION", theoretical_entry=_ENTRY,
            simulated_fill_entry=_ENTRY, stop_loss=Decimal("90"), target=Decimal("120"),
            size=_SIZE, fill_model_version="v1", opened_at=_BEFORE,
        ),
        Event(
            event_id="POSITION_OPENED:still-open", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id="still-open", occurred_at=_BEFORE,
            run_id="run-0", schema_version=1, payload={},
        ),
    )
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id="obs-still-open", position_id="still-open", observed_at=_BEFORE,
            state="HOLD", decay_score=Decimal("0.1"), progress_ratio=Decimal("0.9"),
            unrealized_pnl=Decimal("50"), factors={}, run_id="run-0",
        )
    )

    pool = _take_profit_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][0] == _BEFORE.isoformat()


# ---------------------------------------------------------------------------
# Cold-start validation - the same proof test_self_improvement_pre_entry_
# pool.py's own cold-start test provides for PRE_ENTRY_VETO
# ---------------------------------------------------------------------------


def test_a_take_profit_candidate_validates_at_cold_start_with_zero_guardian_authority_history(
    tmp_path,
):
    """Zero real decisions, zero shadow observations, zero heuristics. 100
    real closed positions each with one observation; 80 of those
    observations' own unrealized_pnl exceeds the position's real eventual
    PnL (i.e. closing there would genuinely have captured more) - a
    TAKE_PROFIT-targeted candidate therefore has a real, out-of-sample
    testable pattern and reaches VALIDATED, exactly like PRE_ENTRY_VETO's own
    cold-start proof."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_take_profit_candidate(repo, condition=_TAKE_PROFIT_CONDITION)

    for i in range(100):
        would_have_been_better = i % 5 != 0  # 80% "closing there was better"
        eventual_exit = Decimal("101")  # eventual pnl = +10
        observed_pnl = "15" if would_have_been_better else "5"
        _seed_closed_position_with_observations(
            repo, f"pos-{i:04d}", _BEFORE + timedelta(minutes=i), eventual_exit,
            observations=[(_BEFORE + timedelta(minutes=i), 0.7, observed_pnl)],
        )

    assert repo.find_guardian_authority_heuristics() == []
    assert repo.find_resolved_guardian_authority_decisions() == []
    assert repo.find_resolved_guardian_authority_shadows() == []

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("tp-1")
    assert row["status"] == "VALIDATED"
    assert row["rejected_reason"] is None
    assert row["train_sample_size"] == 70
    assert row["train_correct_rate"] == 56 / 70
    assert row["test_sample_size"] == 30
    assert row["test_correct_rate"] == 24 / 30


def test_a_take_profit_candidate_with_too_little_real_history_stays_rejected(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_take_profit_candidate(repo, condition=_TAKE_PROFIT_CONDITION)

    for i in range(20):
        _seed_closed_position_with_observations(
            repo, f"pos-{i:04d}", _BEFORE + timedelta(minutes=i), Decimal("101"),
            observations=[(_BEFORE + timedelta(minutes=i), 0.7, "15")],
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("tp-1")
    assert row["status"] == "REJECTED"
    assert "too few train samples" in row["rejected_reason"]


def test_a_tighten_sl_or_pre_entry_veto_candidate_is_never_measured_against_the_take_profit_pool(
    tmp_path,
):
    """Routing proof: a TIGHTEN_SL-targeted candidate's condition
    (progress_ratio-shaped, on purpose) would trivially validate against a
    rich take-profit pool if it were ever routed there by mistake - seeding
    a rich take-profit pool but ZERO tighten_sl evidence proves the routing
    genuinely sends it to the (empty) TIGHTEN_SL pool instead, where it is
    REJECTED on sample size."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id="mis-targeted",
        description="a progress_ratio condition wrongly declared TIGHTEN_SL",
        condition_json=json.dumps(_TAKE_PROFIT_CONDITION),
        proposed_adjustment=0.3,
        rationale="seeded for a routing test",
        run_id="run-llm",
        proposed_at=_NOW,
        target_decision_type="TIGHTEN_SL",
    )
    for i in range(100):
        _seed_closed_position_with_observations(
            repo, f"pos-{i:04d}", _BEFORE + timedelta(minutes=i), Decimal("101"),
            observations=[(_BEFORE + timedelta(minutes=i), 0.7, "15")],
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("mis-targeted")
    assert row["status"] == "REJECTED"
    assert "too few train samples" in row["rejected_reason"]


# ---------------------------------------------------------------------------
# Promotion round-trip
# ---------------------------------------------------------------------------


def test_a_validated_take_profit_candidate_is_promoted_with_values_from_its_test_split(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_take_profit_candidate(repo, "tp-1", adjustment=0.95)  # must be ignored
    repo.record_guardian_authority_heuristic_candidate_validation(
        candidate_id="tp-1", status="VALIDATED",
        train_sample_size=90, train_correct_rate=0.85,
        test_sample_size=40, test_correct_rate=0.9,
        validated_at=_NOW, rejected_reason=None,
    )

    assert promote_validated_heuristic_candidates(repo, _NOW) == 1

    row = _heuristics_by_id(repo)["ga-llm:tp-1"]
    assert row["adjustment"] == pytest.approx(0.4)  # (0.9-0.5)*1.0/family_size(1)
    assert row["confidence"] == pytest.approx(0.8)
    assert row["sample_size"] == 40
    assert json.loads(row["condition_json"]) == _TAKE_PROFIT_CONDITION


def test_a_promoted_take_profit_heuristic_genuinely_fires_only_under_its_own_vocabulary(tmp_path):
    """End-to-end proof through the real, unmodified evaluate_heuristics:
    the promoted row matches TAKE_PROFIT factors and NOTHING else."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_take_profit_candidate(repo, "tp-1")
    repo.record_guardian_authority_heuristic_candidate_validation(
        candidate_id="tp-1", status="VALIDATED",
        train_sample_size=90, train_correct_rate=0.85,
        test_sample_size=40, test_correct_rate=0.9,
        validated_at=_NOW, rejected_reason=None,
    )
    promote_validated_heuristic_candidates(repo, _NOW)
    heuristics = repo.find_guardian_authority_heuristics()

    tp_score, tp_matched = evaluate_heuristics(
        {"progress_ratio": 0.9, "unrealized_pnl_positive": True}, heuristics
    )
    assert tp_matched == ["ga-llm:tp-1"]
    assert tp_score == pytest.approx(0.4)

    tighten_score, tighten_matched = evaluate_heuristics(
        {"time_decay": 0.9, "momentum_decay": 0.9, "volume_decay": 0.9,
         "funding_decay": 0.9, "secondary_confirmation_lost": 0.9, "market_regime": 0.9,
         "guardian_state": "PROTECT"},
        heuristics,
    )
    assert tighten_matched == []
    assert tighten_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Forward-tracking / demotion - mirrors the PRE_ENTRY_VETO track exactly,
# since both reuse the SAME generic, closed-position-counterfactual
# mechanism (_forward_pre_entry_veto_stats applied to a different pool).
# ---------------------------------------------------------------------------


def _seed_promoted_take_profit_heuristic(
    repo, candidate_id="tp-1", condition=None, test_correct_rate=0.9, test_sample_size=40,
    promoted_at=_PROMOTED_AT,
):
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id=candidate_id,
        description=f"{candidate_id} description",
        condition_json=json.dumps(condition if condition is not None else _TAKE_PROFIT_CONDITION),
        proposed_adjustment=0.95,
        rationale=f"{candidate_id} rationale",
        run_id="run-llm",
        proposed_at=_BEFORE,
        target_decision_type="TAKE_PROFIT",
    )
    repo.record_guardian_authority_heuristic_candidate_validation(
        candidate_id=candidate_id, status="VALIDATED",
        train_sample_size=90, train_correct_rate=0.85,
        test_sample_size=test_sample_size, test_correct_rate=test_correct_rate,
        validated_at=_BEFORE + timedelta(hours=1), rejected_reason=None,
    )
    promote_validated_heuristic_candidates(repo, promoted_at)
    return f"ga-llm:{candidate_id}"


def test_a_promoted_take_profit_heuristic_with_a_poor_forward_record_is_demoted(tmp_path):
    """20 forward observations matching this heuristic's own condition; only
    4/20 = 0.2 genuinely had closing-there capture more than the position's
    real eventual PnL - well under the heuristic's own promoted-positive
    direction. Demoted."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_take_profit_heuristic(repo, "tp-1", test_correct_rate=0.8)
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.3)

    for index in range(20):
        would_have_been_better = index < 4
        observed_pnl = "15" if would_have_been_better else "5"
        _seed_closed_position_with_observations(
            repo, f"fwd-{index:04d}", _PROMOTED_AT + timedelta(minutes=index + 1), Decimal("101"),
            observations=[(_PROMOTED_AT + timedelta(minutes=index + 1), 0.7, observed_pnl)],
        )

    assert repo.find_resolved_guardian_authority_decisions() == []

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 1

    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    assert row["confidence"] == 0.0
    assert row["sample_size"] == 20
    candidate = repo.get_guardian_authority_heuristic_candidate("tp-1")
    assert candidate["status"] == "PROMOTED"
    assert candidate["demoted_at"] == _NOW.isoformat()
    assert "TAKE_PROFIT" in candidate["demotion_reason"]


def test_observations_before_promotion_are_never_counted_on_the_take_profit_track(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_take_profit_heuristic(repo, "tp-1")

    for index in range(25):
        _seed_closed_position_with_observations(
            repo, f"past-{index:04d}", _BEFORE + timedelta(minutes=index), Decimal("101"),
            observations=[(_BEFORE + timedelta(minutes=index), 0.7, "5")],  # all "worse"
        )
    for index in range(15):
        would_have_been_better = index < 9  # 9/15 = 0.6, above the demotion bar
        observed_pnl = "15" if would_have_been_better else "5"
        _seed_closed_position_with_observations(
            repo, f"fwd-{index:04d}", _PROMOTED_AT + timedelta(minutes=index + 1), Decimal("101"),
            observations=[(_PROMOTED_AT + timedelta(minutes=index + 1), 0.7, observed_pnl)],
        )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id]["adjustment"] == pytest.approx(0.4)
    assert repo.get_guardian_authority_heuristic_candidate("tp-1")["demoted_at"] is None


def test_a_promoted_take_profit_heuristic_with_a_good_forward_record_is_untouched(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_take_profit_heuristic(repo, "tp-1")
    before = _heuristics_by_id(repo)[heuristic_id]

    for index in range(20):
        _seed_closed_position_with_observations(
            repo, f"fwd-{index:04d}", _PROMOTED_AT + timedelta(minutes=index + 1), Decimal("101"),
            observations=[(_PROMOTED_AT + timedelta(minutes=index + 1), 0.7, "15")],  # all "better"
        )

    assert track_and_demote_underperforming_heuristics(repo, _NOW) == 0
    assert _heuristics_by_id(repo)[heuristic_id] == before
    assert repo.get_guardian_authority_heuristic_candidate("tp-1")["demoted_at"] is None


def test_a_promoted_take_profit_heuristic_with_zero_forward_evidence_is_demoted_after_silence(
    tmp_path,
):
    """The I2 time-based path (mirrors PRE_ENTRY_VETO's own equivalent
    absorbing-state fix): zero forward observations at all since promotion,
    _FORWARD_MAX_SILENT_DAYS later -> demoted on 'no evidence of continued
    value', not on a poor forward correct_rate."""
    repo = SQLiteRepository(tmp_path / "t.db")
    heuristic_id = _seed_promoted_take_profit_heuristic(repo, "tp-1")

    from crypto_trading.guardian.self_improvement import _FORWARD_MAX_SILENT_DAYS

    now = _PROMOTED_AT + timedelta(days=_FORWARD_MAX_SILENT_DAYS)

    assert track_and_demote_underperforming_heuristics(repo, now) == 1

    row = _heuristics_by_id(repo)[heuristic_id]
    assert row["adjustment"] == 0.0
    candidate = repo.get_guardian_authority_heuristic_candidate("tp-1")
    assert "no forward evidence" in candidate["demotion_reason"]
