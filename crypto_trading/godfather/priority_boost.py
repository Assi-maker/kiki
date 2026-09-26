"""GODFATHER priority-boost scoring/ranking overlay (2026-09-18 GODFATHER
expansion, beyond Guardian Authority - see
docs (memory) "crypto-trading-godfather-expansion" for the full gap analysis
this closes).

--------------------------------------------------------------------------
Why this package is separate from crypto_trading/guardian/
--------------------------------------------------------------------------
`crypto_trading/guardian/` is the structurally isolated safety kernel: its
pure decision core (`authority.py::evaluate_heuristics`/`decide_pre_entry`/
`decide_open_position`/`heuristic_condition_matches`/
`_compute_proposed_new_sl`/`_groups_for_factors`) is frozen, and its own
self-improvement pipeline (`self_improvement.py`) only ever writes to
`guardian_authority_heuristics` - the ONE table that decision core reads.

This module is GODFATHER's STRATEGY layer, deliberately placed OUTSIDE
`guardian/` to make that boundary visible in the directory structure, not
just in comments. It runs its OWN, completely separate propose -> validate
-> promote -> track/demote pipeline (mirroring Guardian Authority's own
proven shape, reusing its generic pure helpers by import rather than
duplicating them) for exactly ONE new decision surface: an additive
RANKING nudge applied only inside
`crypto_trading/screening/candidate_engine.py::prioritize_and_apply_budget`.

--------------------------------------------------------------------------
The critical safety property: table separation, not vocabulary disjointness
--------------------------------------------------------------------------
Priority-boost heuristics share their factor vocabulary
(`instrument`/`candidate_score`/`trigger_reasons`, via the UNMODIFIED
`guardian/authority.py::_pre_entry_factors`) with Guardian Authority's own
PRE_ENTRY_VETO heuristics - on purpose, since this asks the mirror-image
question ("which patterns predict WINNING trades worth ranking up" vs.
PRE_ENTRY_VETO's "which patterns predict LOSING trades worth vetoing").
Because the vocabularies are NOT disjoint, vocabulary-based fail-closed
matching (the mechanism that keeps Guardian Authority's own TIGHTEN_SL/
CLOSE_EARLY/TAKE_PROFIT/PRE_ENTRY_VETO families from cross-firing on each
other despite sharing ONE live table) cannot be what keeps this family safe.

Instead, the safety property here is TABLE SEPARATION: `godfather_priority_
heuristics` is a wholly separate table from `guardian_authority_heuristics`
(see storage/db.py's own comment on it), read by exactly one call site
(`candidate_engine.py::prioritize_and_apply_budget`) that Guardian
Authority's `decide_pre_entry`/`decide_open_position` never call, and never
read by `guardian/authority.py` or `guardian/tick.py` at all. Symmetrically,
nothing in this module or in `candidate_engine.py` ever reads
`guardian_authority_heuristics`. The two pipelines are mutually unreachable
by construction - grep-provable, exactly the same "separation IS the safety
property... structurally impossible via a shared table" pattern this
codebase already established for its shadow tables.

A priority-boost heuristic can therefore only ever nudge which
already-eligible candidates get analyzed first within the existing budget
caps. It can never veto an entry, tighten or remove a stop-loss, raise
leverage/sizing, or touch anything `eligibility_filter.py`/`quant_screener.py`
/`gate/`/`risk_limits.yaml` decide.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.detective.stats import _is_blocked_by_exposure
from crypto_trading.guardian.authority import _ADJUSTMENT_SCALE, _pre_entry_factors
from crypto_trading.guardian.self_improvement import (
    _EMPTY_CONDITION_REJECTION_REASON,
    _FORWARD_ADVERSE_DEVIATION,
    _FORWARD_MAX_SILENT_DAYS,
    _FORWARD_MIN_SAMPLE_SIZE,
    _closed_position_entry_outcomes,
    _days_since_promotion,
    _forward_pre_entry_veto_stats,
    _is_usable_condition,
    _most_recent_closed_positions,
    _most_recent_rows,
    _safe_get_candidate,
    _split_pool_chronologically,
    _split_stats,
    _validation_outcome,
)
from crypto_trading.guardian.tick import _budget_allows_one_more_call, _utc_day_start
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.execution import realized_pnl_for
from crypto_trading.schemas.assessments import GodfatherPriorityStrategistAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.storage.repository import Repository

_STRATEGIST_AGENT_FILE = "crypto-godfather-priority-strategist.md"

# Prefix for every heuristic_id this module ever writes to
# godfather_priority_heuristics - lets a reader (and the co-firing-divisor
# family lookup below) tell at a glance which pipeline wrote a given live
# row, mirroring self_improvement.py's own "ga-llm:*"/"ga-hc:*" convention.
_LIVE_HEURISTIC_ID_PREFIX = "godfather-priority:"

# Orphan reconciliation (see _reconcile_orphan_priority_heuristics below) has
# no run_id of its own to log under - it is triggered by the STATE of a
# table, not by a run. A fixed, greppable id is used instead of borrowing an
# unrelated one, mirroring self_improvement.py's own
# _ORPHAN_RECONCILIATION_RUN_ID for the identical Guardian Authority fix.
_ORPHAN_RECONCILIATION_RUN_ID = "godfather-priority-orphan-reconciliation"


def _priority_boost_evidence_pool(repo: Repository) -> list[tuple[str, dict, bool]]:
    """`(closed_at, pre_entry_factors, would_boosting_have_been_correct)` for
    every real closed position whose candidate record still exists -
    available at cold start, exactly like Guardian Authority's own
    `self_improvement.py::_pre_entry_veto_evidence_pool`, and built the same
    way (same skips, same `_pre_entry_factors`/`compute_pnl` reuse), with
    the outcome FLIPPED: `compute_pnl(position) > 0` ("this pattern actually
    won") instead of `<= 0` ("this pattern actually lost"). This is the
    mirror-image counterfactual: PRE_ENTRY_VETO asks "would blocking this
    entry have avoided a loss", priority-boost asks "would ranking this
    pattern up have prioritized a winner". Nothing is synthesized - every
    factor and every PnL input is the position's own real, persisted data.

    Skips (same reasoning as `_pre_entry_veto_evidence_pool`): a CLOSED
    position with no `closed_at` (defensive, no production path produces
    this), an exposure-blocked position (zero real market exposure - its
    `compute_pnl` is `0 - fees - funding`, which would bias this pool's `>`
    comparison for reasons unrelated to real market behavior), and a
    position whose candidate record is missing/corrupt."""
    pool: list[tuple[str, dict, bool]] = []

    for position in repo.find_closed_positions():
        if position.closed_at is None:
            continue
        if _is_blocked_by_exposure(position):
            continue
        candidate = _safe_get_candidate(repo, position.candidate_id)
        if candidate is None:
            continue
        realized = realized_pnl_for(repo, position)
        if not realized.verified:
            continue  # 2026-09-26: unknown outcome - never learned from
        pool.append(
            (
                position.closed_at.isoformat(),
                _pre_entry_factors(candidate),
                realized.paper_size_equivalent(position) > Decimal("0"),
            )
        )

    return pool


def _build_priority_context(repo: Repository, run_id: str) -> dict:
    """Real, windowed evidence for the priority-strategist role - reuses
    Guardian Authority's own `_closed_position_entry_outcomes`/
    `_most_recent_closed_positions`/`_safe_get_candidate` unmodified (the
    SAME real, bounded data view `self_improvement.py::_build_context`
    already builds for its own `closed_position_entry_outcomes`), so no
    second unbounded read is introduced here."""
    closed_positions = _most_recent_closed_positions(repo.find_closed_positions())
    candidates_by_id: dict[str, Candidate] = {}
    for position in closed_positions:
        candidate = _safe_get_candidate(repo, position.candidate_id)
        if candidate is not None:
            candidates_by_id[position.candidate_id] = candidate

    realized = {
        position.position_id: realized_pnl_for(repo, position) for position in closed_positions
    }
    entry_outcomes = _closed_position_entry_outcomes(closed_positions, candidates_by_id, realized)

    return {
        "run_id": run_id,
        "closed_position_entry_outcomes": entry_outcomes,
        "existing_live_priority_heuristics": [
            {
                "heuristic_id": row["heuristic_id"],
                "description": row["description"],
                "condition_json": row["condition_json"],
                "adjustment": row["adjustment"],
                "sample_size": row["sample_size"],
            }
            for row in repo.find_godfather_priority_heuristics()
        ],
        "already_proposed_priority_candidates": [
            {
                "candidate_id": row["candidate_id"],
                "description": row["description"],
                "condition_json": row["condition_json"],
                "proposed_adjustment": row["proposed_adjustment"],
            }
            for row in _most_recent_rows(
                repo.find_proposed_godfather_priority_heuristic_candidates(), "proposed_at"
            )
        ],
        "pre_entry_factor_names": sorted(
            {name for entry in entry_outcomes for name in entry["factors"]}
        ),
    }


def propose_priority_candidates(
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    now: datetime,
) -> int:
    """PROPOSE step. Mirrors `self_improvement.py::propose_candidate_
    heuristics`'s shape exactly (same daily-watermark-claimed-up-front
    discipline, same shared daily AI budget gate, same never-raises
    contract), routed to this pipeline's own, separate watermark and
    candidates table. An empty `proposed_heuristics` list is a SUCCESS, not
    a failure - "no confident winning pattern in the accumulated history" is
    a valid, expected answer."""
    day_key: str | None = None
    try:
        day_key = _utc_day_start(now).date().isoformat()

        if not _budget_allows_one_more_call(repo, settings, now):
            log_event(run_id, event="godfather_priority_strategist_deferred_budget")
            return 0

        last_proposed = repo.get_godfather_priority_strategist_last_proposed_date()
        if last_proposed is not None and last_proposed >= day_key:
            log_event(
                run_id,
                event="godfather_priority_strategist_already_proposed_today",
                last_proposed_date=last_proposed,
                proposal_date=day_key,
            )
            return 0

        repo.set_godfather_priority_strategist_last_proposed_date(day_key, now)

        context = _build_priority_context(repo, run_id)
        agent_def = load_agent_definition(_STRATEGIST_AGENT_FILE)
        assessment: GodfatherPriorityStrategistAssessment = runner.run(
            agent_def, context, GodfatherPriorityStrategistAssessment
        )

        billed = getattr(runner, "last_call_billed", True)
        cost = getattr(runner, "last_call_cost_usd", Decimal("0"))
        if billed:
            repo.record_ai_call_event(
                Event(
                    event_id=f"AI_CALL_MADE:godfather_priority_strategist:{run_id}:{day_key}",
                    event_type="AI_CALL_MADE",
                    aggregate_type="godfather_priority_strategist",
                    aggregate_id=run_id,
                    occurred_at=now,
                    run_id=run_id,
                    schema_version=1,
                    payload={
                        "role": "godfather_priority_strategist",
                        "status": assessment.status,
                        "cost_usd": str(cost),
                    },
                )
            )

        if assessment.status != "ok":
            log_event(
                run_id,
                event="godfather_priority_strategist_assessment_unusable",
                status=assessment.status,
            )
            return 0

        saved = 0
        for index, proposal in enumerate(assessment.proposed_heuristics):
            if repo.save_godfather_priority_heuristic_candidate(
                candidate_id=f"priority-llm:{run_id}:{index}",
                description=proposal.description,
                condition_json=json.dumps(proposal.condition),
                proposed_adjustment=proposal.adjustment,
                rationale=proposal.rationale,
                run_id=run_id,
                proposed_at=now,
            ):
                saved += 1

        log_event(
            run_id,
            event="godfather_priority_strategist_candidates_proposed",
            proposal_date=day_key,
            proposed_count=len(assessment.proposed_heuristics),
            saved_count=saved,
        )
        return saved
    except Exception as exc:
        log_event(
            run_id,
            event="godfather_priority_strategist_failed",
            proposal_date=day_key,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return 0


def validate_pending_priority_candidates(repo: Repository, now: datetime) -> int:
    """VALIDATE step. Single-pool (unlike Guardian Authority's dual-pool
    dispatch - this pipeline only ever has one target), using the SAME
    generic `_split_pool_chronologically`/`_split_stats`/`_validation_
    outcome` Guardian Authority's own validation reuses unmodified, against
    `_priority_boost_evidence_pool` above. Returns the count of candidate
    rows actually transitioned (VALIDATED + REJECTED) in THIS call."""
    candidates = repo.find_proposed_godfather_priority_heuristic_candidates()
    if not candidates:
        return 0

    train_rows, test_rows = _split_pool_chronologically(_priority_boost_evidence_pool(repo))

    processed = 0
    for candidate in candidates:
        condition = json.loads(candidate["condition_json"])

        if not _is_usable_condition(condition):
            if repo.record_godfather_priority_heuristic_candidate_validation(
                candidate_id=candidate["candidate_id"],
                status="REJECTED",
                train_sample_size=0,
                train_correct_rate=0.0,
                test_sample_size=0,
                test_correct_rate=0.0,
                validated_at=now,
                rejected_reason=_EMPTY_CONDITION_REJECTION_REASON,
            ):
                processed += 1
            continue

        train_n, train_rate = _split_stats(train_rows, condition)
        test_n, test_rate = _split_stats(test_rows, condition)
        status, rejected_reason = _validation_outcome(train_n, train_rate, test_n, test_rate)

        if repo.record_godfather_priority_heuristic_candidate_validation(
            candidate_id=candidate["candidate_id"],
            status=status,
            train_sample_size=train_n,
            train_correct_rate=train_rate,
            test_sample_size=test_n,
            test_correct_rate=test_rate,
            validated_at=now,
            rejected_reason=rejected_reason,
        ):
            processed += 1

    return processed


def _priority_heuristic_id(candidate_id: str) -> str:
    return f"{_LIVE_HEURISTIC_ID_PREFIX}{candidate_id}"


def _live_promoted_priority_candidates(repo: Repository) -> list[dict]:
    return [
        row
        for row in repo.find_promoted_godfather_priority_heuristic_candidates()
        if row["demoted_at"] is None
    ]


def _with_usable_priority_conditions(validated: list[dict]) -> list[dict]:
    """C1's promotion-time mirror (same defense-in-depth Guardian Authority's
    own `_with_usable_conditions` provides): a row that reached VALIDATED
    without passing today's validation guard is refused here rather than
    trusted."""
    usable: list[dict] = []
    for row in validated:
        try:
            condition = json.loads(row["condition_json"])
        except (ValueError, TypeError):
            condition = None
        if _is_usable_condition(condition):
            usable.append(row)
            continue
        log_event(
            row["run_id"],
            event="godfather_priority_promotion_refused_unusable_condition",
            candidate_id=row["candidate_id"],
            reason=_EMPTY_CONDITION_REJECTION_REASON,
            condition_json=row["condition_json"],
        )
    return usable


def _write_priority_heuristic(
    repo: Repository, candidate: dict, heuristic_id: str, family_size: int, now: datetime
) -> None:
    """The single call site this module ever uses to write
    `godfather_priority_heuristics` - shared by newly-promoted rows and the
    rescale of already-live ones, mirroring `self_improvement.py::_write_
    llm_heuristic`'s co-firing Cap B (average, never sum: every live row's
    adjustment is its own earned `deviation` divided by the CURRENT total
    family size, so promoting more can never make the family louder than its
    single loudest member). No per-target split is needed here (unlike
    Guardian Authority's dual-family divisor) - this table serves exactly
    one decision family."""
    deviation = float(candidate["test_correct_rate"]) - 0.5
    repo.upsert_godfather_priority_heuristic(
        heuristic_id=heuristic_id,
        description=candidate["description"],
        condition_json=candidate["condition_json"],
        adjustment=deviation * _ADJUSTMENT_SCALE / family_size,
        confidence=abs(deviation) * 2.0,
        sample_size=int(candidate["test_sample_size"]),
        updated_at=now,
    )


def promote_validated_priority_candidates(repo: Repository, now: datetime) -> int:
    """PROMOTE step. No cardinality cap analogous to Guardian Authority's
    `_MAX_LIVE_TIGHTEN_SL_HEURISTICS` is needed here: that cap exists only
    because Guardian's TIGHTEN_SL forward-tracking is firing-attribution-
    based and can reach an absorbing state (a diluted heuristic that can
    never fire again can never accumulate forward evidence, so can never be
    demoted). This family's forward-tracking (see `track_and_demote_
    underperforming_priority_heuristics` below) uses the SAME closed-
    position-counterfactual mechanism as Guardian's own PRE_ENTRY_VETO
    track, which accumulates evidence from every real closed position
    whether or not this heuristic ever actually influenced a ranking - no
    absorbing state, no cap needed, exactly like PRE_ENTRY_VETO."""
    validated = _with_usable_priority_conditions(
        repo.find_validated_godfather_priority_heuristic_candidates()
    )
    if not validated:
        return 0

    live = sorted(_live_promoted_priority_candidates(repo), key=lambda row: row["candidate_id"])
    incoming = sorted(validated, key=lambda row: row["candidate_id"])
    family_size = len(live) + len(incoming)

    for row in live:
        _write_priority_heuristic(repo, row, row["promoted_heuristic_id"], family_size, now)

    promoted = 0
    for row in incoming:
        heuristic_id = _priority_heuristic_id(row["candidate_id"])
        _write_priority_heuristic(repo, row, heuristic_id, family_size, now)
        if repo.promote_godfather_priority_heuristic_candidate(
            row["candidate_id"], heuristic_id, now
        ):
            promoted += 1

    return promoted


def _zero_orphan_priority_heuristic(repo: Repository, row: dict, now: datetime) -> None:
    """Silences one orphaned `godfather-priority:*` heuristic through the
    same `upsert_godfather_priority_heuristic` every other write in this
    pipeline uses. The row is preserved verbatim (same description,
    condition, sample size) - only adjustment/confidence are zeroed, exactly
    as a demotion does. Mirrors `self_improvement.py::_zero_orphan_llm_
    heuristic` (Guardian Authority's own I4 fix)."""
    repo.upsert_godfather_priority_heuristic(
        heuristic_id=row["heuristic_id"],
        description=row["description"],
        condition_json=row["condition_json"],
        adjustment=0.0,
        confidence=0.0,
        sample_size=int(row["sample_size"] or 0),
        updated_at=now,
    )


def _reconcile_orphan_priority_heuristics(repo: Repository, now: datetime) -> int:
    """Zeroes every live `godfather_priority_heuristics` row with no
    PROMOTED candidate referencing it, and returns how many were silenced.

    THE WINDOW this closes (identical in shape to Guardian Authority's own
    I4 fix, `self_improvement.py::_reconcile_orphan_llm_heuristics`):
    `promote_validated_priority_candidates` writes the real heuristic row
    (`upsert_godfather_priority_heuristic`) BEFORE marking its candidate
    PROMOTED - forced, since the candidate has to record the heuristic id
    the write produced - and the two writes are separately committed. A
    crash between them leaves a LIVE ranking heuristic whose candidate is
    still VALIDATED: invisible to `_live_promoted_priority_candidates`,
    therefore excluded from Cap B's divisor accounting AND from this
    function's own demotion sweep below (which iterates candidates, not
    heuristics) - it would keep nudging candidate ranking indefinitely,
    unowned and unmeasurable.

    Runs BEFORE this function's own `if not live: return 0` early return,
    since an orphan's defining feature is that no live candidate points to
    it - that early return would otherwise skip reconciliation entirely
    whenever the interrupted promotion was this family's only member.

    Every row in this table is written by this module's own
    `_LIVE_HEURISTIC_ID_PREFIX` prefix (unlike Guardian Authority's table,
    which also holds `ga-hc:state:*` self-critique rows with no candidate
    BY DESIGN) - so no prefix filter is needed here, every row in this
    table is candidate-owned or it is an orphan. An orphan already at
    0.0/0.0 is left alone rather than rewritten and re-logged forever."""
    owned = {
        str(row["promoted_heuristic_id"] or "")
        for row in repo.find_promoted_godfather_priority_heuristic_candidates()
    }

    zeroed = 0
    for row in repo.find_godfather_priority_heuristics():
        heuristic_id = str(row["heuristic_id"])
        if heuristic_id in owned:
            continue
        if float(row["adjustment"]) == 0.0 and float(row["confidence"]) == 0.0:
            continue
        _zero_orphan_priority_heuristic(repo, row, now)
        log_event(
            _ORPHAN_RECONCILIATION_RUN_ID,
            event="godfather_priority_orphan_heuristic_zeroed",
            heuristic_id=heuristic_id,
            previous_adjustment=float(row["adjustment"]),
            previous_confidence=float(row["confidence"]),
            reason=(
                "live godfather_priority_heuristics row with no PROMOTED "
                "candidate - a promotion that wrote the heuristic row but "
                "never marked its candidate PROMOTED (crash/failure between "
                "the two writes)"
            ),
        )
        zeroed += 1

    return zeroed


def track_and_demote_underperforming_priority_heuristics(repo: Repository, now: datetime) -> int:
    """TRACK/DEMOTE step. Mirrors the PRE_ENTRY_VETO branch of
    `self_improvement.py::track_and_demote_underperforming_heuristics`
    exactly (same sign-aware count-based canary + time-based silence bar,
    same `_forward_pre_entry_veto_stats`/`_days_since_promotion` reuse),
    against `_priority_boost_evidence_pool` above instead of Guardian's own
    `_pre_entry_veto_evidence_pool`. Returns the number of heuristics
    actually demoted by THIS call (orphan reconciliations are not counted -
    see `_reconcile_orphan_priority_heuristics`, a separate, distinctly
    logged mechanism)."""
    _reconcile_orphan_priority_heuristics(repo, now)

    live = _live_promoted_priority_candidates(repo)
    if not live:
        return 0

    live_adjustments = {
        row["heuristic_id"]: float(row["adjustment"])
        for row in repo.find_godfather_priority_heuristics()
    }
    pool: list[tuple[str, dict, bool]] | None = None

    demoted = 0
    for candidate in sorted(live, key=lambda row: row["candidate_id"]):
        promoted_at = candidate["promoted_at"]
        heuristic_id = candidate["promoted_heuristic_id"]
        if not promoted_at or not heuristic_id:
            continue
        if heuristic_id not in live_adjustments:
            continue

        if pool is None:
            pool = _priority_boost_evidence_pool(repo)
        promoted_row = repo.get_godfather_priority_heuristic_candidate(candidate["candidate_id"])
        sample_size, correct_rate = _forward_pre_entry_veto_stats(
            pool, json.loads(promoted_row["condition_json"]), promoted_at
        )

        promoted_adjustment = live_adjustments[heuristic_id]
        promoted_sign = 1 if promoted_adjustment > 0 else -1
        agreement = promoted_sign * (correct_rate - 0.5)

        reason: str | None = None
        if sample_size == 0:
            silent_days = _days_since_promotion(promoted_at, now)
            if silent_days is not None and silent_days >= _FORWARD_MAX_SILENT_DAYS:
                reason = (
                    f"no forward evidence: zero forward priority-boost samples in "
                    f"{silent_days:.1f} days since promoted_at={promoted_at} "
                    f"(bar: {_FORWARD_MAX_SILENT_DAYS} days)"
                )
        elif sample_size >= _FORWARD_MIN_SAMPLE_SIZE and agreement <= -_FORWARD_ADVERSE_DEVIATION:
            reason = (
                f"forward correct_rate {correct_rate:.4f} deviates {agreement:+.4f} "
                f"in the direction opposite this heuristic's own adjustment "
                f"{promoted_adjustment:+.4f} (bar: {-_FORWARD_ADVERSE_DEVIATION}) over "
                f"n={sample_size} forward priority-boost samples since "
                f"promoted_at={promoted_at}"
            )
        if reason is None:
            continue

        if not repo.mark_godfather_priority_heuristic_candidate_demoted(
            candidate["candidate_id"], now, reason
        ):
            continue
        repo.upsert_godfather_priority_heuristic(
            heuristic_id=heuristic_id,
            description=candidate["description"],
            condition_json=candidate["condition_json"],
            adjustment=0.0,
            confidence=0.0,
            sample_size=sample_size,
            updated_at=now,
        )
        log_event(
            candidate["run_id"],
            event="godfather_priority_heuristic_demoted",
            candidate_id=candidate["candidate_id"],
            heuristic_id=heuristic_id,
            forward_sample_size=sample_size,
            forward_correct_rate=correct_rate,
            promoted_adjustment=promoted_adjustment,
            forward_agreement=agreement,
            demotion_reason=reason,
        )
        demoted += 1

    return demoted


def run_priority_boost_self_improvement_tick(
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    now: datetime,
) -> None:
    """Top-level periodic-tick orchestrator, mirroring
    `self_improvement.py::run_godfather_self_improvement_tick`'s shape: each
    step in its own try/except with its own `log_event` name, so one step's
    failure never blocks the others or this pipeline's caller
    (`discovery_loop.py`, in its own independent try/except alongside
    Guardian's own self-improvement tick call). Gated internally on
    `settings.godfather.priority_boost_enabled`, read defensively and
    fail-closed - unreadable means off, and reading it can never raise into
    the hosting discovery tick. Never raises."""
    try:
        priority_boost_enabled = bool(settings.godfather.priority_boost_enabled)
    except Exception:
        priority_boost_enabled = False
    if not priority_boost_enabled:
        return

    try:
        propose_priority_candidates(repo, runner, settings, run_id, now)
    except Exception as exc:
        log_event(
            run_id,
            event="godfather_priority_propose_tick_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    try:
        validate_pending_priority_candidates(repo, now)
    except Exception as exc:
        log_event(
            run_id,
            event="godfather_priority_validate_tick_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    try:
        promote_validated_priority_candidates(repo, now)
    except Exception as exc:
        log_event(
            run_id,
            event="godfather_priority_promote_tick_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    try:
        track_and_demote_underperforming_priority_heuristics(repo, now)
    except Exception as exc:
        log_event(
            run_id,
            event="godfather_priority_demote_tick_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
