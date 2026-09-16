"""Tests for Task 4B of docs/superpowers/sdd/2026-09-15-guardian-authority-
live-autonomy/ - the SECOND, independent validation pool
(crypto_trading/guardian/self_improvement.py::_pre_entry_veto_evidence_pool)
and the cold-start proof that closes R2 of the design spec's
"Addendum (2026-09-16)".

Why this file exists at all (the addendum's own framing): Task 4's TIGHTEN_SL
pool is empty BY CONSTRUCTION at cold start - `decide_pre_entry`/
`decide_open_position` only ever return a non-default decision when
`evaluate_heuristics` scores nonzero, which requires a row in
`guardian_authority_heuristics`, so with that table empty no TIGHTEN_SL
decision (real or shadow) can ever exist to validate against. The pool tested
here is built entirely from REAL closed positions' REAL entry evidence and
REAL realized PnL - data that exists completely independent of whether any
Guardian Authority heuristic has ever existed.

File split (brief's own "your call, document which you chose and why"): the
pool builder's own tests, the out-of-sample proof for that pool, and the
cold-start proof live HERE, together with the closed-position seeding helper
they all need; the DUAL-POOL ROUTING tests live in
test_self_improvement_validation.py (Task 4's file), which already owns the
TIGHTEN_SL seeding helpers a cross-contamination proof needs on the other
side, and imports `_seed_closed_position` from here - the same cross-test-file
helper reuse test_self_improvement.py already does with test_tick.py's
`_seed_candidate_and_position`.

There is no AI call anywhere in this file: neither
`_pre_entry_veto_evidence_pool` nor `validate_pending_heuristic_candidates`
makes one.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.guardian.self_improvement import (
    _MIN_SAMPLE_SIZE,
    _pre_entry_veto_evidence_pool,
    _tighten_sl_evidence_pool,
    validate_pending_heuristic_candidates,
)
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_BASE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

# Entry/size/fee constants the PnL assertions below are derived from:
# compute_pnl = size * (exit - entry)/entry - fees - funding
#             = 1000 * (exit - 100)/100 - 0 - 0 = 10 * (exit - 100).
# So exit=99 -> -10 (a real loss), exit=101 -> +10 (a real win), and
# exit=100 -> exactly 0 (the boundary case the "<= 0 counts as NOT
# favorable" convention has to decide).
_ENTRY = Decimal("100")
_SIZE = Decimal("1000")
_LOSS_EXIT = Decimal("99")
_WIN_EXIT = Decimal("101")
_BREAKEVEN_EXIT = Decimal("100")


# --------------------------------------------------------------------------
# Seeding helpers - real candidate rows + real closed positions, written
# through the same repository methods the trading pipeline itself uses.
# --------------------------------------------------------------------------
def _evidence(instrument, candidate_score, trigger_reasons, at) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=["30m"],
        evaluated_at=at,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=candidate_score,
        trigger_reasons=list(trigger_reasons),
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _seed_closed_position(
    repo,
    position_id,
    closed_at,
    exit_price,
    instrument="BTCUSDT",
    candidate_score=0.9,
    trigger_reasons=("momentum_breakout",),
    with_candidate=True,
):
    """One REAL closed position plus (unless `with_candidate=False`) the real
    candidate row its pre-entry evidence lives in. Nothing here is Guardian
    Authority data: no heuristic, no decision, no shadow observation is ever
    written - which is exactly what makes this pool available at cold
    start."""
    opened_at = closed_at - timedelta(hours=2)
    if with_candidate:
        candidate = Candidate(
            candidate_id=position_id,
            idempotency_key=f"key-{position_id}",
            instrument=instrument,
            discovery_run_id="run-0",
            evidence_hash=f"hash-{position_id}",
            status="CONFIRMED",
            evidence_record=_evidence(instrument, candidate_score, trigger_reasons, opened_at),
            created_at=opened_at,
            updated_at=opened_at,
        )
        repo.create_candidate_with_event(
            candidate,
            Event(
                event_id=f"CANDIDATE_CREATED:{position_id}",
                event_type="CANDIDATE_CREATED",
                aggregate_type="candidate",
                aggregate_id=position_id,
                occurred_at=opened_at,
                run_id="run-0",
                schema_version=1,
                payload={},
            ),
        )
    repo.create_position_with_event(
        Position(
            position_id=position_id,
            candidate_id=position_id,
            instrument=instrument,
            direction="LONG",
            status="OPEN_POSITION",
            theoretical_entry=_ENTRY,
            simulated_fill_entry=_ENTRY,
            stop_loss=Decimal("90"),
            target=Decimal("120"),
            size=_SIZE,
            fill_model_version="v1",
            opened_at=opened_at,
        ),
        Event(
            event_id=f"POSITION_OPENED:{position_id}",
            event_type="POSITION_OPENED",
            aggregate_type="position",
            aggregate_id=position_id,
            occurred_at=opened_at,
            run_id="run-0",
            schema_version=1,
            payload={},
        ),
    )
    repo.close_position_with_event(
        position_id,
        exit_price,
        exit_price,
        "stop_loss" if exit_price < _ENTRY else "target",
        Decimal("0"),
        Decimal("0"),
        closed_at,
        Event(
            event_id=f"POSITION_CLOSED:{position_id}",
            event_type="POSITION_CLOSED",
            aggregate_type="position",
            aggregate_id=position_id,
            occurred_at=closed_at,
            run_id="run-0",
            schema_version=1,
            payload={},
        ),
    )


def _seed_veto_candidate(
    repo,
    candidate_id="veto-1",
    condition=None,
    adjustment=-0.2,
    target_decision_type="PRE_ENTRY_VETO",
):
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id=candidate_id,
        description="a PRE_ENTRY_VETO-shaped candidate under validation",
        condition_json=json.dumps(
            condition if condition is not None else {"trigger_reasons": ["momentum_breakout"]}
        ),
        proposed_adjustment=adjustment,
        rationale="seeded directly for a Task 4B test",
        run_id="run-llm",
        proposed_at=_NOW,
        target_decision_type=target_decision_type,
    )


# --------------------------------------------------------------------------
# The pool builder itself: real factors, real PnL, real skips.
# --------------------------------------------------------------------------
def test_pre_entry_veto_evidence_pool_builds_tuples_from_real_entry_evidence_and_real_pnl(
    tmp_path,
):
    """One losing, one winning and one exactly-breakeven closed position.
    `factors` must be exactly what the unmodified `_pre_entry_factors`
    produces from the candidate's own evidence record (instrument,
    candidate_score, trigger_reasons - nothing else, nothing invented), and
    the outcome flag is "would a veto have been correct", i.e. `compute_pnl
    <= 0`."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(
        repo,
        "loser",
        _BASE,
        _LOSS_EXIT,
        instrument="ETHUSDT",
        candidate_score=0.71,
        trigger_reasons=("volume_spike",),
    )
    _seed_closed_position(repo, "winner", _BASE + timedelta(minutes=1), _WIN_EXIT)
    _seed_closed_position(repo, "breakeven", _BASE + timedelta(minutes=2), _BREAKEVEN_EXIT)

    pool = _pre_entry_veto_evidence_pool(repo)

    by_timestamp = {row[0]: row for row in pool}
    assert len(pool) == 3
    assert by_timestamp[_BASE.isoformat()] == (
        _BASE.isoformat(),
        {
            "instrument": "ETHUSDT",
            "candidate_score": 0.71,
            "trigger_reasons": ["volume_spike"],
        },
        True,  # real loss (-10 USDT) -> a veto would have been correct
    )
    # A real win: vetoing it would have COST money, so the veto is wrong.
    assert by_timestamp[(_BASE + timedelta(minutes=1)).isoformat()][2] is False
    # Exactly zero PnL counts as NOT favorable - the same convention
    # resolve_pending_decisions already uses - so the veto is "correct".
    assert by_timestamp[(_BASE + timedelta(minutes=2)).isoformat()][2] is True


def test_pre_entry_veto_evidence_pool_skips_a_position_whose_candidate_record_is_missing(
    tmp_path,
):
    """A closed position with no candidate row to reconstruct pre-entry
    evidence from is SKIPPED, never guessed at - the same discipline
    `_reconstruct_tighten_sl_factors` applies on the other pool."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "orphan", _BASE, _LOSS_EXIT, with_candidate=False)
    _seed_closed_position(repo, "intact", _BASE + timedelta(minutes=1), _LOSS_EXIT)

    pool = _pre_entry_veto_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][0] == (_BASE + timedelta(minutes=1)).isoformat()


def test_pre_entry_veto_evidence_pool_skips_a_closed_row_without_a_closed_at(tmp_path):
    """Defensive: `closed_at` is nullable on the positions table, and a row
    without one cannot carry a position in the chronological split. Written
    through raw SQL because no production code path can produce this state -
    that is the point of the guard."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "undated", _BASE, _LOSS_EXIT)
    _seed_closed_position(repo, "dated", _BASE + timedelta(minutes=1), _LOSS_EXIT)
    repo._conn.execute("UPDATE positions SET closed_at = NULL WHERE position_id = 'undated'")
    repo._conn.commit()

    pool = _pre_entry_veto_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][0] == (_BASE + timedelta(minutes=1)).isoformat()


def test_pre_entry_veto_evidence_pool_ignores_still_open_positions(tmp_path):
    """`find_closed_positions` is the only source - an open position has no
    realized PnL and therefore no ground truth to counterfactual against."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "closed", _BASE, _LOSS_EXIT)
    repo.create_position_with_event(
        Position(
            position_id="still-open",
            candidate_id="closed",
            instrument="BTCUSDT",
            direction="LONG",
            status="OPEN_POSITION",
            theoretical_entry=_ENTRY,
            simulated_fill_entry=_ENTRY,
            stop_loss=Decimal("90"),
            target=Decimal("120"),
            size=_SIZE,
            fill_model_version="v1",
            opened_at=_BASE,
        ),
        Event(
            event_id="POSITION_OPENED:still-open",
            event_type="POSITION_OPENED",
            aggregate_type="position",
            aggregate_id="still-open",
            occurred_at=_BASE,
            run_id="run-0",
            schema_version=1,
            payload={},
        ),
    )

    pool = _pre_entry_veto_evidence_pool(repo)

    assert len(pool) == 1
    assert pool[0][0] == _BASE.isoformat()


# --------------------------------------------------------------------------
# THE test that matters for this task: R2's cold start, closed for real.
# --------------------------------------------------------------------------
def test_a_pre_entry_veto_candidate_validates_at_cold_start_with_zero_guardian_authority_history(
    tmp_path,
):
    """Zero real decisions, zero shadow observations, zero heuristics - the
    exact state every new deployment starts in, and the state that makes
    Task 4's TIGHTEN_SL pool permanently empty (asserted below, not assumed).
    100 real closed positions exist, 80 of which lost money. A
    PRE_ENTRY_VETO-targeted candidate therefore has a real, out-of-sample
    testable pattern and reaches VALIDATED - which is precisely what could
    never happen before this task."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_veto_candidate(repo, condition={"trigger_reasons": ["momentum_breakout"]})

    for i in range(100):
        # 80% real losses, uniformly spread, so train and test see the same
        # 0.8 "a veto would have been correct" rate.
        lost = i % 5 != 0
        _seed_closed_position(
            repo,
            f"pos-{i:04d}",
            _BASE + timedelta(minutes=i),
            _LOSS_EXIT if lost else _WIN_EXIT,
        )

    # The cold start is REAL, not accidental: nothing Guardian Authority has
    # ever decided, observed or promoted exists in this database.
    assert repo.find_guardian_authority_heuristics() == []
    assert repo.find_resolved_guardian_authority_decisions() == []
    assert repo.find_resolved_guardian_authority_shadows() == []
    assert _tighten_sl_evidence_pool(repo) == []

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("veto-1")
    assert row["status"] == "VALIDATED"
    assert row["rejected_reason"] is None
    assert row["train_sample_size"] == 70
    assert row["train_correct_rate"] == 56 / 70
    assert row["test_sample_size"] == 30
    assert row["test_correct_rate"] == 24 / 30
    assert row["validated_at"] == _NOW.isoformat()


# --------------------------------------------------------------------------
# The out-of-sample bar is genuinely enforced for THIS pool too (the same
# proof Acceptance Criterion 4 required of Task 4's own pool).
# --------------------------------------------------------------------------
def test_a_pre_entry_veto_candidate_is_rejected_when_the_pattern_reverses_on_test(tmp_path):
    """Two differently-signed clusters of real closed positions, split
    unevenly by the 70/30 cutoff: the 70 chronologically oldest (all TRAIN)
    lost money 90% of the time, the 30 newest (all TEST) lost money only 10%
    of the time. Train alone would promote a confident "veto this pattern"
    rule; the genuinely held-out test split reverses it, so the candidate is
    REJECTED."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_veto_candidate(repo, condition={"trigger_reasons": ["momentum_breakout"]})

    for i in range(70):
        lost = i % 10 != 0  # 63 losses / 70 = 0.9
        _seed_closed_position(
            repo,
            f"train-{i:04d}",
            _BASE + timedelta(minutes=i),
            _LOSS_EXIT if lost else _WIN_EXIT,
        )
    for i in range(30):
        lost = i in (0, 10, 20)  # 3 losses / 30 = 0.1
        _seed_closed_position(
            repo,
            f"test-{i:04d}",
            _BASE + timedelta(minutes=70 + i),
            _LOSS_EXIT if lost else _WIN_EXIT,
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("veto-1")
    assert row["status"] == "REJECTED"
    assert row["train_sample_size"] == 70
    assert row["train_correct_rate"] == 63 / 70
    assert row["test_sample_size"] == 30
    assert row["test_correct_rate"] == 3 / 30
    assert "sign disagreement" in row["rejected_reason"]


def test_a_pre_entry_veto_candidate_with_too_little_real_history_stays_rejected(tmp_path):
    """Untested stays untested, never evidence (the addendum's own wording):
    a genuinely small real history is REJECTED on the standard sample-size
    reason - the bar is never lowered just because this pool is new."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_veto_candidate(repo, condition={"trigger_reasons": ["momentum_breakout"]})

    for i in range(20):
        _seed_closed_position(repo, f"pos-{i:04d}", _BASE + timedelta(minutes=i), _LOSS_EXIT)

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("veto-1")
    assert row["status"] == "REJECTED"
    assert row["train_sample_size"] == 14
    assert "too few train samples" in row["rejected_reason"]
    assert f"n=14 < {_MIN_SAMPLE_SIZE}" in row["rejected_reason"]


def test_a_pre_entry_veto_candidate_uses_the_real_min_max_matching_semantics(tmp_path):
    """The candidate conditions on `candidate_score_min` - a numeric lower
    bound per authority.py's own documented semantics, not a literal factor
    named "candidate_score_min" (no such key exists in any
    `_pre_entry_factors` dict). 40 chronologically oldest positions score
    below the bound and must be EXCLUDED; the 60 above it must be included -
    the exact 30/30 sample sizes are the proof the real, unmodified
    `heuristic_condition_matches` did the filtering."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_veto_candidate(repo, condition={"candidate_score_min": 0.8})

    for i in range(40):
        # Below the bound, and deliberately the OPPOSITE outcome pattern -
        # if these leaked in, the asserted rates could not come out at 0.8.
        _seed_closed_position(
            repo,
            f"below-{i:04d}",
            _BASE + timedelta(minutes=i),
            _WIN_EXIT,
            candidate_score=0.3,
        )
    for i in range(60):
        lost = i % 5 != 0  # 48 losses / 60 = 0.8
        _seed_closed_position(
            repo,
            f"above-{i:04d}",
            _BASE + timedelta(minutes=40 + i),
            _LOSS_EXIT if lost else _WIN_EXIT,
            candidate_score=0.9,
        )

    processed = validate_pending_heuristic_candidates(repo, _NOW)

    assert processed == 1
    row = repo.get_guardian_authority_heuristic_candidate("veto-1")
    assert row["train_sample_size"] == 30
    assert row["test_sample_size"] == 30
    assert row["status"] == "VALIDATED"
    assert row["train_correct_rate"] == row["test_correct_rate"] == 0.8
