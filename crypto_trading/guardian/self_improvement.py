"""Guardian Authority self-improvement, steps 1-2 of 4: PROPOSE + VALIDATE
(design spec:
docs/superpowers/specs/2026-09-15-guardian-authority-live-autonomy-design.md,
"What 'self-improvement' concretely means here").

The full pipeline is propose -> validate -> promote -> track/demote. THIS
module proposes (`propose_candidate_heuristics`, Task 3) and validates
out-of-sample (`validate_pending_heuristic_candidates`, Task 4); promotion
into the real table and forward-performance tracking/demotion are later
tasks. Everything Task 3 writes lands in one place -
`guardian_authority_heuristic_candidates`, status `PROPOSED` - a table the
real decision engine never reads, and Task 4 only ever moves a row from
`PROPOSED` to `VALIDATED` or `REJECTED` in that SAME table, via the
existing, unmodified `record_guardian_authority_heuristic_candidate_
validation`. `evaluate_heuristics` reads `guardian_authority_heuristics` (a
different table), and nothing in this module can write there: the only
write path into that table anywhere in this plan is the existing, unmodified
`upsert_guardian_authority_heuristic`, called by the later promotion step
after an independent out-of-sample validation clears a candidate. So an LLM
that hallucinates a confident-sounding rule cannot influence a single real
trade from here - it can only queue a hypothesis for a statistical test it
has no way to reach.

Isolation (design spec, Global Constraints): this module never imports
`position_sizing.py`, never references `set_leverage`, and never imports
`gate/`, `screening/` or any AI role loading code beyond the shared,
unmodified `agents/loader.py` every role already uses. Its only reach into
the trading pipeline is READ-ONLY reuse of code the brief names explicitly:
`guardian/tick.py`'s budget gate, `guardian/authority.py`'s factor
reconstruction, and `detective/stats.py`'s post-trade statistics - all three
imported unmodified, none of them called in a way that writes.

--------------------------------------------------------------------------
Why two gates, and why the watermark is claimed BEFORE the work
--------------------------------------------------------------------------
Gate 1 is the shared daily AI budget (`_budget_allows_one_more_call`,
imported from guardian/tick.py, not forked). Gate 2 is a once-per-UTC-day
watermark: proposing heuristics is a slow-changing, expensive, whole-history
analysis, not a per-tick action - re-running it every tick would burn the
budget the rest of the system needs and produce near-identical proposals.

The two gates are deliberately asymmetric about consuming the day's slot:

- Budget exhausted -> nothing happens at all, and the slot is explicitly NOT
  consumed. Budget frees up over the day (and resets at the UTC boundary the
  budget window itself uses), so a later tick today must still be allowed to
  propose. Consuming the slot here would let a transient budget squeeze
  silently cost a whole day of learning.
- Both gates passed -> the slot is claimed IMMEDIATELY, before the context
  is built and before the model is called. Every outcome after that point -
  a good proposal, an empty proposal, a `status="failed"` assessment, a
  transport explosion, a failing database read - has already spent real
  time and (usually) real money, so it must not be retried every tick for
  the remainder of the day. Claiming up front also means a crash between
  the call and the save cannot produce a second paid call today.

An empty `proposed_heuristics` list is a SUCCESS, not a failure: "the
accumulated history does not support a confident pattern" is the correct
answer most days, and the agent role says so explicitly. It returns 0 and
consumes the day's slot exactly like a productive call.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.detective.stats import (
    _is_blocked_by_exposure,
    compute_breakdown_by_signal_type,
    compute_guardian_exit_effectiveness,
)
from crypto_trading.guardian.authority import (
    _MIN_MISCALIBRATION,
    _MIN_SAMPLE_SIZE,
    _pre_entry_factors,
    _reconstruct_tighten_sl_factors,
    heuristic_condition_matches,
)
from crypto_trading.guardian.tick import _budget_allows_one_more_call, _utc_day_start
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.assessments import GodfatherStrategistAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import Repository

_STRATEGIST_AGENT_FILE = "crypto-godfather-strategist.md"

# Evidence window (review fix, round 1, Finding 2). The three history reads
# below are full-table SELECTs that grow forever. Unbounded, two things go
# wrong - and the second is a SILENT TERMINAL STATE, which is what makes
# this a correctness bound and not a tuning knob:
#   (a) cost per call rises linearly with history, for a call whose value
#       does not - the model does not get better at spotting a pattern by
#       being handed every row since the system's first day;
#   (b) once the serialized prompt crosses the model's context limit the
#       call fails outright - and because the day's slot is claimed up
#       front (deliberately, see the module docstring), the system would
#       then burn exactly one failed PAID attempt per day, every day,
#       forever, visible only as a `godfather_strategist_failed` log line.
#
# 150 rows per source, most-recent-first. The number is chosen against the
# budget gate's own worst-case assumption rather than picked round:
# `tick.py::_WORST_CASE_COST_PER_CALL_USD` reserves $0.20 for one AI call,
# and 150 shadow + 150 real rows at roughly 600 bytes of JSON each is about
# 45k input tokens, i.e. ~$0.09 at the Sonnet input price `runner.py`
# already records - leaving comfortable room for the response inside the
# $0.20 this call is budgeted for. It is also 5x `authority.py`'s own
# `_MIN_SAMPLE_SIZE = 30`, so a bounded window still carries several times
# the evidence a pattern needs to clear validation, and the freshest rows
# are the relevant ones anyway: a heuristic is being proposed about how the
# system behaves NOW, not in its first month.
#
# Bounding happens here rather than as a new SQL `LIMIT` repository method
# (the `find_closed_positions_pending_detective_analysis(limit)` shape)
# deliberately: the harm above is entirely prompt-side, the three reads are
# existing plan-sanctioned methods this task must reuse UNMODIFIED, and
# this task already carries one flagged repository.py addition. Slicing
# before the per-position candidate lookups also bounds what was an
# unbounded N+1. If the read cost itself ever matters, converting these to
# ORDER BY ... DESC LIMIT ? methods is a mechanical follow-up.
_MAX_EVIDENCE_ROWS = 150


def _safe_json_loads(raw: str | None) -> dict | None:
    """A factors_json column this module cannot parse is context, not
    control flow - it is dropped from the prompt rather than allowed to
    abort the whole proposal run. (The real decision engine deliberately
    does the opposite and fails loudly on malformed condition_json; that
    asymmetry is intended - this is a read-only prompt builder.)"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _safe_json_list(raw: str | None) -> list | None:
    """List counterpart of `_safe_json_loads`, for
    `matched_heuristic_ids_json` - same drop-rather-than-abort rationale."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _optional_bool(value: object) -> bool | None:
    """SQLite has no native boolean - a nullable BOOLEAN column comes back
    as `None`/`0`/`1`. The same `None`-check-then-`bool()` normalization
    authority.py::update_heuristics_from_resolved_decisions already applies
    to `expectation_correct`, applied here so the JSON handed to the model
    says `false`, not `0` (an integer the model could plausibly read as a
    count or a score rather than an outcome)."""
    return None if value is None else bool(value)


def _safe_get_candidate(repo: Repository, candidate_id: str) -> Candidate | None:
    # Same "one corrupt row is skipped, the rest continues" principle as
    # detective/batch.py::_safe_get_candidate and repository.py's own
    # find_candidates_by_status()/find_all_candidates().
    try:
        return repo.get_candidate(candidate_id)
    except CorruptCandidateStateError:
        return None


def _shadow_context(row: dict) -> dict:
    return {
        "shadow_id": row["shadow_id"],
        "instrument": row["instrument"],
        "shadow_decision": row["shadow_decision"],
        "decided_at": row["decided_at"],
        "expected_direction": row["expected_direction"],
        "confidence": row["confidence"],
        "factors": _safe_json_loads(row["factors_json"]),
        "mfe": row["mfe"],
        "mae": row["mae"],
        "actual_exit_reason": row["actual_exit_reason"],
        "actual_pnl_usdt": row["actual_pnl_usdt"],
        "expectation_correct": _optional_bool(row["expectation_correct"]),
        "prediction_error": row["prediction_error"],
    }


def _real_decision_context(repo: Repository, row: dict) -> dict:
    # Factors are reconstructed through the UNMODIFIED
    # authority.py::_reconstruct_tighten_sl_factors exact-join (decision's
    # decided_at == the same tick's guardian_observations.observed_at).
    # It returns None rather than guessing when no such row exists, and
    # None is carried into the prompt as None - never silently replaced by
    # a plausible-looking factors dict the decision was not actually made
    # from.
    return {
        "decision_id": row["decision_id"],
        "decision_type": row["decision_type"],
        "decided_at": row["decided_at"],
        "expected_direction": row["expected_direction"],
        "confidence": row["confidence"],
        "factors": _reconstruct_tighten_sl_factors(repo, row),
        "intervention_applied": _optional_bool(row["intervention_applied"]),
        "matched_heuristic_ids": _safe_json_list(row["matched_heuristic_ids_json"]),
        "actual_exit_reason": row["actual_exit_reason"],
        "actual_pnl_usdt": row["actual_pnl_usdt"],
        "expectation_correct": _optional_bool(row["expectation_correct"]),
    }


def _observed_factor_names(shadows: list[dict], reals: list[dict]) -> list[str]:
    """The exact factor vocabulary present in the evidence. The agent role
    is told to use ONLY these names: `heuristic_condition_matches` is
    fail-closed on a missing key, so a condition built on an invented
    factor name matches nothing at all and would make the candidate
    silently unusable rather than visibly wrong."""
    names: set[str] = set()
    for entry in (*shadows, *reals):
        factors = entry.get("factors")
        if isinstance(factors, dict):
            names.update(factors.keys())
    return sorted(names)


def _most_recent_rows(rows: list[dict], timestamp_key: str) -> list[dict]:
    """The newest `_MAX_EVIDENCE_ROWS` rows, most-recent-first. Timestamps
    on these tables are stored as ISO-8601 strings, so lexicographic order
    IS chronological order - no parsing, and a row with a missing/NULL
    timestamp sorts last instead of raising."""
    ordered = sorted(rows, key=lambda row: row.get(timestamp_key) or "", reverse=True)
    return ordered[:_MAX_EVIDENCE_ROWS]


def _closed_position_entry_outcomes(
    positions: list[Position], candidates_by_id: dict[str, Candidate]
) -> list[dict]:
    """Task 4B: real pre-entry evidence paired with the real outcome it led
    to, for the already-windowed closed positions.

    Why this exists: a `PRE_ENTRY_VETO` proposal may only condition on
    `_pre_entry_factors`' own three fields, and before this task the context
    evidenced exactly ONE of them - `trigger_reasons`, indirectly, through
    Detective's `historical_signal_type_breakdown` grouping key. A model
    told "never invent a factor name, never cite evidence you were not
    given" therefore had no legitimate basis for a condition on
    `candidate_score` or `instrument` at all. This is the minimal fix: the
    SAME rows, the SAME candidate lookups and the SAME
    `_MAX_EVIDENCE_ROWS` window `_build_context` already performed - no
    additional database read of any kind - reshaped through the same
    unmodified `_pre_entry_factors`/`compute_pnl` the validation pool itself
    uses, so what the model reasons about and what it is later graded
    against are the same view of the same data. A position whose candidate
    record is missing is omitted (the same skip the pool applies), never
    emitted with guessed factors.

    Review fix (round 1): exposure-blocked (`size == 0`) positions are
    excluded here too, via the SAME unmodified `_is_blocked_by_exposure`.
    Without it this list would contradict the very statistics sitting next
    to it in the same prompt - `historical_signal_type_breakdown` and
    `historical_guardian_exit_effectiveness` are built by detective/stats.py,
    which already excludes those rows on the explicit 2026-09-03 user ruling
    - and it would show the model a `pnl_usdt` of roughly zero for a trade
    that never had any market exposure at all."""
    outcomes: list[dict] = []
    for position in positions:
        if _is_blocked_by_exposure(position):
            continue
        candidate = candidates_by_id.get(position.candidate_id)
        if candidate is None:
            continue
        outcomes.append(
            {
                "closed_at": position.closed_at.isoformat() if position.closed_at else None,
                "factors": _pre_entry_factors(candidate),
                # str(), not float(): the same Decimal-preserving discipline
                # detective/stats.py already applies to money in a prompt.
                "pnl_usdt": str(compute_pnl(position)),
                "exit_reason": position.exit_reason,
            }
        )
    return outcomes


def _most_recent_closed_positions(positions: list[Position]) -> list[Position]:
    """Positions counterpart of `_most_recent_rows`. `closed_at` is a
    nullable datetime here rather than a string, and sorting a mix of
    `None` and `datetime` raises `TypeError`, so undated rows are dropped
    up front - a closed position without a close time cannot contribute to
    a time-windowed view of recent behaviour anyway."""
    dated = [position for position in positions if position.closed_at is not None]
    ordered = sorted(dated, key=lambda position: position.closed_at, reverse=True)
    return ordered[:_MAX_EVIDENCE_ROWS]


def _build_context(repo: Repository, settings: Settings, run_id: str) -> dict:
    # Every read here is windowed to _MAX_EVIDENCE_ROWS before anything is
    # built from it - see that constant's rationale.
    shadows = [
        _shadow_context(row)
        for row in _most_recent_rows(
            repo.find_resolved_guardian_authority_shadows(), "decided_at"
        )
    ]
    reals = [
        _real_decision_context(repo, row)
        for row in _most_recent_rows(
            repo.find_resolved_guardian_authority_decisions(), "decided_at"
        )
    ]

    # Sliced BEFORE the per-position candidate lookups, so the N+1 below is
    # bounded by the window too.
    closed_positions = _most_recent_closed_positions(repo.find_closed_positions())
    candidates_by_id: dict[str, Candidate] = {}
    for position in closed_positions:
        candidate = _safe_get_candidate(repo, position.candidate_id)
        if candidate is not None:
            candidates_by_id[position.candidate_id] = candidate

    entry_outcomes = _closed_position_entry_outcomes(closed_positions, candidates_by_id)

    return {
        "run_id": run_id,
        "resolved_shadow_decisions": shadows,
        "resolved_real_decisions": reals,
        "closed_position_entry_outcomes": entry_outcomes,
        "historical_signal_type_breakdown": compute_breakdown_by_signal_type(
            closed_positions, candidates_by_id
        ),
        "historical_guardian_exit_effectiveness": compute_guardian_exit_effectiveness(
            closed_positions, settings.risk_limits.max_position_hold_hours
        ),
        "existing_live_heuristics": [
            {
                "heuristic_id": row["heuristic_id"],
                "description": row["description"],
                "condition_json": row["condition_json"],
                "adjustment": row["adjustment"],
                "sample_size": row["sample_size"],
            }
            for row in repo.find_guardian_authority_heuristics()
        ],
        "already_proposed_candidates": [
            {
                "candidate_id": row["candidate_id"],
                "description": row["description"],
                "condition_json": row["condition_json"],
                "proposed_adjustment": row["proposed_adjustment"],
            }
            for row in _most_recent_rows(
                repo.find_proposed_guardian_authority_heuristic_candidates(), "proposed_at"
            )
        ],
        # Two vocabularies, kept deliberately separate (Task 4B): the
        # TIGHTEN_SL factor names that actually occur in Guardian
        # Authority's own decision history, and the pre-entry factor names
        # that actually occur in real entry evidence. A condition written in
        # the other type's vocabulary is fail-closed against its own pool
        # (it matches nothing), so the role prompt names each list
        # separately rather than handing the model one merged set it could
        # mix freely.
        "observed_factor_names": _observed_factor_names(shadows, reals),
        "pre_entry_factor_names": sorted(
            {name for entry in entry_outcomes for name in entry["factors"]}
        ),
    }


def propose_candidate_heuristics(
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    now: datetime,
) -> int:
    """Runs at most one GODFATHER Strategist proposal per UTC day. Returns
    the number of candidate rows actually written by THIS call (a candidate
    whose deterministic id already exists is an INSERT OR IGNORE no-op and
    is not counted). Never raises - every AI call site in this codebase is
    fail-safe, and a failing self-improvement step must never be able to
    disturb the trading tick that hosts it.

    Review fix (round 1, Finding 1): the try/except covers the ENTIRE body,
    including both gates and the watermark claim. Those are SQLite reads
    and a SQLite WRITE - under concurrent tick/write contention a
    `sqlite3.OperationalError: database is locked` from any of them would
    otherwise propagate straight out of this function into the hosting
    trading tick, which is precisely the failure this contract exists to
    prevent. The claim itself has NOT moved: it still happens after both
    gates and before the AI call, so every slot-consumption semantic
    documented in the module docstring is unchanged."""
    # Assigning None cannot raise, and it keeps `proposal_date` safe to log
    # even if the very first statement inside the try is what failed.
    day_key: str | None = None
    try:
        day_key = _utc_day_start(now).date().isoformat()

        if not _budget_allows_one_more_call(repo, settings, now):
            log_event(run_id, event="godfather_strategist_deferred_budget")
            return 0

        last_proposed = repo.get_guardian_authority_strategist_last_proposed_date()
        # `>=`, not `==`: a clock that jumps backwards must not be able to
        # buy a second proposal for a day that already had one.
        if last_proposed is not None and last_proposed >= day_key:
            log_event(
                run_id,
                event="godfather_strategist_already_proposed_today",
                last_proposed_date=last_proposed,
                proposal_date=day_key,
            )
            return 0

        # Claim the day's slot before any work - see module docstring.
        repo.set_guardian_authority_strategist_last_proposed_date(day_key, now)

        context = _build_context(repo, settings, run_id)
        agent_def = load_agent_definition(_STRATEGIST_AGENT_FILE)
        assessment: GodfatherStrategistAssessment = runner.run(
            agent_def, context, GodfatherStrategistAssessment
        )

        billed = getattr(runner, "last_call_billed", True)
        cost = getattr(runner, "last_call_cost_usd", Decimal("0"))
        if billed:
            repo.record_ai_call_event(
                Event(
                    event_id=f"AI_CALL_MADE:godfather_strategist:{run_id}:{day_key}",
                    event_type="AI_CALL_MADE",
                    # Same shape as detective/batch.py's own AI_CALL_MADE row
                    # (role name as aggregate_type, run_id as aggregate_id) -
                    # this is a whole-run analysis, not a per-position one.
                    aggregate_type="godfather_strategist",
                    aggregate_id=run_id,
                    occurred_at=now,
                    run_id=run_id,
                    schema_version=1,
                    payload={
                        "role": "godfather_strategist",
                        "status": assessment.status,
                        "cost_usd": str(cost),
                    },
                )
            )

        if assessment.status != "ok":
            log_event(
                run_id,
                event="godfather_strategist_assessment_unusable",
                status=assessment.status,
            )
            return 0

        saved = 0
        for index, proposal in enumerate(assessment.proposed_heuristics):
            if repo.save_guardian_authority_heuristic_candidate(
                candidate_id=f"llm:{run_id}:{index}",
                description=proposal.description,
                condition_json=json.dumps(proposal.condition),
                proposed_adjustment=proposal.adjustment,
                rationale=proposal.rationale,
                run_id=run_id,
                proposed_at=now,
                # Persisted verbatim (Task 4B): the model's own declaration
                # of which decision type this proposal is for, which is what
                # routes it to its own evidence pool in
                # `validate_pending_heuristic_candidates` below. Never
                # inferred here from the condition's shape.
                target_decision_type=proposal.target_decision_type,
            ):
                saved += 1

        log_event(
            run_id,
            event="godfather_strategist_candidates_proposed",
            proposal_date=day_key,
            proposed_count=len(assessment.proposed_heuristics),
            saved_count=saved,
        )
        return saved
    except Exception as exc:
        # Same fail-safe discipline as every other AI call site here: the
        # day's slot stays claimed (the attempt already cost time/money),
        # and the caller gets 0 instead of an exception.
        log_event(
            run_id,
            event="godfather_strategist_failed",
            proposal_date=day_key,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return 0


# ---------------------------------------------------------------------------
# Task 4 (2026-09-15, Guardian Authority Live Autonomy): out-of-sample
# validation gate - step 2 of 4 (propose -> VALIDATE -> promote ->
# track/demote). Every row `propose_candidate_heuristics` above writes lands
# here next: PROPOSED -> VALIDATED | REJECTED, via the ONE write path Task 1
# built for exactly this transition, the existing, unmodified
# `record_guardian_authority_heuristic_candidate_validation` - never a new
# write path, and never `upsert_guardian_authority_heuristic` (that is the
# LATER promotion step's job, reusing an already-VALIDATED row).
#
# --------------------------------------------------------------------------
# Where the evidence pool comes from - "the SAME pool", not a third reading
# --------------------------------------------------------------------------
# The SAME pool Task 8's shadow self-critique
# (paper_trading/guardian_authority_shadow.py::
# update_shadow_heuristics_from_resolved_shadow_observations) and the real
# Task 9 self-critique (authority.py::update_heuristics_from_resolved_
# decisions) already learn from - not a third, independently-invented
# reading of the same two tables:
#
# - Shadow rows: `repo.find_resolved_guardian_authority_shadows()`, filtered
#   to `shadow_decision == "TIGHTEN_SL"` with a non-null
#   `expectation_correct` (same scope note both sibling functions document -
#   CLOSE_EARLY/NO_ACTION never carry a real expectation to learn from).
#   Factors come straight off the row's own immutable `factors_json` - no
#   reconstruction needed, exactly as Task 8's own function does it.
# - Real rows: `repo.find_resolved_guardian_authority_decisions()`, filtered
#   to `decision_type == "TIGHTEN_SL"` with a non-null `expectation_correct`
#   AND `intervention_applied` true (Task 9's own I2 hardening fix - without
#   it, a single position sitting above the tighten threshold for many
#   consecutive ticks could inflate a sample with ticks that were never a
#   genuine, distinct intervention; see authority.py's Task 9 docstring
#   section for the full rationale). Factors are recovered via the existing,
#   unmodified `_reconstruct_tighten_sl_factors` exact-join - never a
#   second, hand-rolled reconstruction.
#
# Both sibling functions apply exactly these filters before a row may
# contribute to ANY group's tally; this function applies them once, up
# front, before either the chronological split or the later per-candidate
# condition filter - the pool this function draws its train/test split from
# is, sample for sample, the intersection of the two pools those two
# already-trusted functions use, unmodified.
#
# --------------------------------------------------------------------------
# The train/test split: why a row-level index split, not run_tier1_
# backtest's DB-level split
# --------------------------------------------------------------------------
# `run_tier1_backtest` physically separates train/test into two SEPARATE
# SQLite database files before any evaluation runs, "so out-of-sample cannot
# leak into training results by construction" (its own module docstring).
# This function honors that SAME principle - genuine out-of-sample means the
# test split's statistics can never have been visible to whatever decided
# the train split's statistics were good enough - but the mechanism here is
# simpler: this is a one-shot, single-process computation over an in-memory
# list of `(decided_at, factors, expectation_correct)` tuples, not a replay
# across two database files, so a plain chronological INDEX split is
# sufficient; there is no reason to build the heavier DB-level machinery
# `run_tier1_backtest` needs for its own, structurally different (multi-day
# replay) use case.
#
# `_TRAIN_FRACTION = 0.7`: sort the WHOLE pool by `decided_at` ascending: the
# first 70% of rows (by COUNT, not by calendar time) become train, the
# remaining 30% become test - deterministic, reproducible with no RNG/seed,
# and computed ONCE per call, shared by every candidate processed in that
# call (the pool and its chronological boundary do not depend on which
# candidate is being validated - only the per-condition filter below does).
#
# --------------------------------------------------------------------------
# Per-candidate condition filter, and the promotion bar itself
# --------------------------------------------------------------------------
# For each split, further filter to rows whose factors satisfy the
# candidate's own, unmodified `condition_json` (parsed once per candidate),
# via the real, unmodified `heuristic_condition_matches` - never a
# simplified restatement of its matching semantics. `sample_size`/
# `correct_rate` are then computed exactly as `update_heuristics_from_
# resolved_decisions`'s own per-group tally does (a plain count and a plain
# correct/n ratio); `deviation = correct_rate - 0.5` follows immediately,
# same formula, computed inline rather than stored (the DB row only ever
# stores `correct_rate`, per Task 1's own schema - `deviation`'s sign is
# re-derived from it wherever needed, here and at promotion time).
#
# A candidate is VALIDATED only if BOTH splits clear `_MIN_SAMPLE_SIZE`/
# `_MIN_MISCALIBRATION` (imported, unmodified, from authority.py - the exact
# thresholds the real heuristics table itself is held to) AND their
# deviations agree in sign - train agreeing with itself is not evidence of
# anything; only train AND an independently-computed, chronologically LATER
# test split agreeing is the genuine out-of-sample bar Acceptance Criterion
# 4 requires (a candidate whose pattern looks real on train alone but
# reverses on test must be rejected, not promoted). Every other outcome is
# REJECTED, with `rejected_reason` naming the FIRST check that failed,
# checked in this fixed order (mirrors the brief's own listed order): too
# few train samples, too few test samples, train miscalibration below
# floor, test miscalibration below floor, sign disagreement between splits.
#
# --------------------------------------------------------------------------
# Error handling: deliberately NOT wrapped in a blanket try/except
# --------------------------------------------------------------------------
# Unlike `propose_candidate_heuristics` above (an AI call site, wrapped for
# the reasons documented at length in that function's own docstring), this
# function has no `run_id` parameter and performs no AI call, no budget
# gate, no once-per-day watermark - it is a pure statistical batch
# computation over already-resolved DB rows, structurally identical in kind
# to `update_heuristics_from_resolved_decisions`/`update_shadow_heuristics_
# from_resolved_shadow_observations`, NEITHER of which catches exceptions
# either (both let a DB read failure propagate straight to their own
# caller). A malformed `condition_json` on a candidate row is exactly the
# case `heuristic_condition_matches`'s own module docstring says should
# fail loudly rather than be hidden behind a silent no-match (heuristics
# rows - and candidate rows - are Guardian Authority's own internal data,
# not third-party input); every candidate's `condition_json` was itself
# produced by `json.dumps` inside `propose_candidate_heuristics` above, so
# it is always syntactically valid JSON in practice. This function follows
# its two direct siblings' own precedent, not `propose_candidate_
# heuristics`'s AI-call-specific one.
# ---------------------------------------------------------------------------

_TRAIN_FRACTION = 0.7


def _tighten_sl_evidence_pool(repo: Repository) -> list[tuple[str, dict, bool]]:
    """`(decided_at, factors, expectation_correct)` for every resolved
    TIGHTEN_SL row - shadow AND real - with a non-null `expectation_correct`.
    See the Task 4 module section above for exactly which pool this is and
    why."""
    pool: list[tuple[str, dict, bool]] = []

    for shadow in repo.find_resolved_guardian_authority_shadows():
        if shadow["shadow_decision"] != "TIGHTEN_SL":
            continue
        if shadow["expectation_correct"] is None:
            continue
        pool.append(
            (
                shadow["decided_at"],
                json.loads(shadow["factors_json"]),
                bool(shadow["expectation_correct"]),
            )
        )

    for decision in repo.find_resolved_guardian_authority_decisions():
        if decision["decision_type"] != "TIGHTEN_SL":
            continue
        if decision["expectation_correct"] is None:
            continue
        if not bool(decision.get("intervention_applied")):
            continue  # Task 9's own I2 filter - see module section above
        factors = _reconstruct_tighten_sl_factors(repo, decision)
        if factors is None:
            continue  # no matching observation to reconstruct from - skip, don't guess
        pool.append(
            (
                decision["decided_at"],
                factors,
                bool(decision["expectation_correct"]),
            )
        )

    return pool


# ---------------------------------------------------------------------------
# Task 4B (2026-09-16 addendum, R2): the SECOND evidence pool - real closed
# positions, not Guardian Authority's own decision history.
#
# --------------------------------------------------------------------------
# Why a second pool had to exist at all
# --------------------------------------------------------------------------
# The pool above is empty BY CONSTRUCTION at cold start, and a post-Task-4
# audit re-derived that straight from authority.py's own source:
# `decide_pre_entry`/`decide_open_position` only ever return a non-default
# decision when `evaluate_heuristics(...)` scores nonzero, which requires at
# least one row in `guardian_authority_heuristics`. With that table empty -
# the state every new deployment starts in, and the state this whole
# self-improvement pipeline exists to get OUT of - every real and shadow
# decision is APPROVE/NO_ACTION forever, so no TIGHTEN_SL row can ever exist
# to validate against and every proposed candidate would reject forever on
# `too few train samples`. Widening to real/shadow PRE_ENTRY_VETO decisions
# does not help either: `decide_pre_entry` has the same nonzero-score
# precondition, AND `resolve_pending_decisions` permanently skips resolving a
# PRE_ENTRY_VETO row's counterfactual by original design (it has no
# market-data infrastructure to evaluate a position that was never opened).
#
# --------------------------------------------------------------------------
# What this pool is, and why it is neither circular nor fabricated
# --------------------------------------------------------------------------
# Every position that was ever actually opened and closed is real,
# deterministic ground truth that exists completely independent of whether a
# Guardian Authority heuristic has ever existed - Gate approval and position
# opening/closing never read the heuristics table at all. For a candidate
# aimed at PRE_ENTRY_VETO, validation therefore asks a genuine
# counterfactual: *if this condition had been an active pre-entry veto rule,
# would it have matched this position's real entry evidence, and would
# blocking that entry actually have avoided a loss?*
#
# - The evidence is the position's own candidate record, reshaped by the
#   UNMODIFIED `_pre_entry_factors` - literally the same function, and
#   therefore the same factor vocabulary, `decide_pre_entry` itself is
#   evaluated against. Never a second, hand-rolled reconstruction.
# - The outcome is `compute_pnl(position) <= 0` via the UNMODIFIED
#   `compute_pnl` (the only PnL computation in this codebase), using the same
#   "a P/L of exactly zero counts as NOT favorable" convention
#   `resolve_pending_decisions` already applies - so a would-be veto is
#   scored "correct" iff the real position actually lost money or exactly
#   broke even.
# - Nothing is synthesized: every factor and every PnL input is the row's
#   own real, persisted data. The only counterfactual thing is the VETO
#   ITSELF (which never fired) - the same kind of counterfactual
#   `run_tier1_backtest` and Detective's post-trade analysis already treat
#   as legitimate evidence here, not a new methodology invented for this
#   task.
#
# Three rows are skipped rather than guessed at: a CLOSED row with no
# `closed_at` (defensive - it has no place in a chronological split; no
# production path produces one, since close_position_with_event sets status
# and closed_at in the same UPDATE), a position whose candidate record is
# missing or corrupt (`_safe_get_candidate` returning None) - the exact
# mirror of the TIGHTEN_SL pool skipping a decision whose observation row
# cannot be reconstructed - and an exposure-blocked, zero-size position,
# which has no real realized outcome at all (see the skip's own comment in
# the function below for why that one matters most here).
#
# Statistical machinery: NONE is added or forked here. `_split_pool_
# chronologically`, `_split_stats` and `_validation_outcome` are already
# generic over `list[tuple[str, dict, bool]]` and contain zero TIGHTEN_SL-
# specific logic, so this pool is a second BUILDER of the identical shape and
# every threshold, split rule and rejection reason is reused byte-for-byte.
# The two pools are split independently and never merged: their timestamps
# live in different domains (a decision's `decided_at` vs. a position's
# `closed_at`) and a shared split boundary would mean one pool's volume could
# move the other's train/test cutoff.
# ---------------------------------------------------------------------------

# The candidate-declared routing key, and the legacy default. A row written
# before Task 4B existed has target_decision_type NULL; it is read as
# TIGHTEN_SL because that was the only pool that existed when it was
# proposed - backward compatibility for a handful of rows, not a valid
# ongoing state (every row written from Task 4B onward carries the proposing
# model's own explicit declaration).
_TARGET_TIGHTEN_SL = "TIGHTEN_SL"
_TARGET_PRE_ENTRY_VETO = "PRE_ENTRY_VETO"


def _pre_entry_veto_evidence_pool(repo: Repository) -> list[tuple[str, dict, bool]]:
    """`(closed_at, pre_entry_factors, would_veto_have_been_correct)` for
    every real closed position whose candidate record still exists. See the
    Task 4B module section above for why this pool is available at cold
    start and why it is neither circular nor fabricated."""
    pool: list[tuple[str, dict, bool]] = []

    # Deliberately unwindowed, unlike `_build_context`'s own bounded loop
    # above: this is an aggregate statistical read (like every other
    # consumer of `find_closed_positions`), and truncating it to the most
    # recent N rows would silently move the train/test boundary and change
    # the very statistics the promotion bar is computed from. The prompt
    # side is bounded because a prompt has a cost and a context limit; a
    # validation pool has neither.
    for position in repo.find_closed_positions():
        if position.closed_at is None:
            continue  # defensive - nothing to place in a chronological split
        if _is_blocked_by_exposure(position):
            # Review fix (round 1). A position whose `size` was pushed to 0
            # by the max_total_exposure_pct cap never had real market
            # exposure, so it has no real realized outcome to
            # counterfactual against - and `compute_pnl` on it returns
            # `0 - fees - funding`, which this pool's own `<= 0` rule would
            # score as "a veto would have been correct". Every such row
            # would land on that SAME side of the boolean, and they cluster
            # non-randomly (whatever the cap happened to block), so they
            # would push any condition matching them toward VALIDATED on an
            # outcome that never happened - the one thing the design
            # addendum's "every factor and every PnL value is the
            # position's own real, persisted data" forbids. Excluded via
            # the SAME unmodified helper detective/stats.py,
            # paper_track_report.py, profit_protection_report.py and
            # repository.py already apply to outcome statistics (explicit
            # 2026-09-03 user ruling: such rows "would incorrectly be
            # counted as break-even trades").
            continue
        candidate = _safe_get_candidate(repo, position.candidate_id)
        if candidate is None:
            continue  # missing/corrupt entry evidence - skip, never guess
        pool.append(
            (
                position.closed_at.isoformat(),
                _pre_entry_factors(candidate),
                compute_pnl(position) <= Decimal("0"),
            )
        )

    return pool


def _split_pool_chronologically(
    pool: list[tuple[str, dict, bool]],
) -> tuple[list[tuple[str, dict, bool]], list[tuple[str, dict, bool]]]:
    """Sorts `pool` by `decided_at` ascending and returns `(train, test)`:
    the first `_TRAIN_FRACTION` of rows by COUNT, and the rest. See the
    Task 4 module section above for why a row-level index split is the
    right mechanism here (not `run_tier1_backtest`'s DB-level split).
    Timestamps here are the same ISO-8601 strings `_most_recent_rows` above
    already relies on sorting lexicographically - lexicographic order IS
    chronological order for this format, no parsing needed."""
    ordered = sorted(pool, key=lambda row: row[0])
    split_index = int(len(ordered) * _TRAIN_FRACTION)
    return ordered[:split_index], ordered[split_index:]


def _split_stats(rows: list[tuple[str, dict, bool]], condition: dict) -> tuple[int, float]:
    """`(sample_size, correct_rate)` for the subset of `rows` whose factors
    satisfy `condition` via the real, unmodified `heuristic_condition_
    matches` - exactly the tally `update_heuristics_from_resolved_
    decisions` computes per guardian_state group, computed here per
    CANDIDATE CONDITION instead. `correct_rate` is `0.0` (a placeholder,
    never read as meaningful) when nothing matched - an n=0 split always
    fails the `_MIN_SAMPLE_SIZE` check before any caller looks at its
    correct_rate."""
    outcomes = [
        correct for _, factors, correct in rows if heuristic_condition_matches(condition, factors)
    ]
    n = len(outcomes)
    if n == 0:
        return 0, 0.0
    return n, sum(1 for correct in outcomes if correct) / n


def _validation_outcome(
    train_n: int, train_rate: float, test_n: int, test_rate: float
) -> tuple[str, str | None]:
    """`(status, rejected_reason)` per the promotion bar documented in the
    Task 4 module section above. Checks run in the brief's own listed
    order; `rejected_reason` names the FIRST one that failed. `status` is
    always `"VALIDATED"` or `"REJECTED"` - the two outcomes `record_
    guardian_authority_heuristic_candidate_validation` accepts."""
    if train_n < _MIN_SAMPLE_SIZE:
        return "REJECTED", f"too few train samples (n={train_n} < {_MIN_SAMPLE_SIZE})"
    if test_n < _MIN_SAMPLE_SIZE:
        return "REJECTED", f"too few test samples (n={test_n} < {_MIN_SAMPLE_SIZE})"

    train_deviation = train_rate - 0.5
    if abs(train_deviation) < _MIN_MISCALIBRATION:
        return (
            "REJECTED",
            f"train miscalibration below floor (|deviation|={abs(train_deviation):.4f} "
            f"< {_MIN_MISCALIBRATION})",
        )

    test_deviation = test_rate - 0.5
    if abs(test_deviation) < _MIN_MISCALIBRATION:
        return (
            "REJECTED",
            f"test miscalibration below floor (|deviation|={abs(test_deviation):.4f} "
            f"< {_MIN_MISCALIBRATION})",
        )

    if (train_deviation > 0) != (test_deviation > 0):
        return (
            "REJECTED",
            f"sign disagreement between splits (train_deviation={train_deviation:+.4f}, "
            f"test_deviation={test_deviation:+.4f})",
        )

    return "VALIDATED", None


def validate_pending_heuristic_candidates(repo: Repository, now: datetime) -> int:
    """Out-of-sample validation gate (Task 4, dual-pool since Task 4B). For
    every row currently `PROPOSED` (`repo.find_proposed_guardian_authority_
    heuristic_candidates()`), computes train/test `sample_size`/
    `correct_rate` for that candidate's own `condition_json` against the
    chronologically-split evidence pool matching that candidate's own
    declared `target_decision_type` - the TIGHTEN_SL decision pool (Task 4
    module section) or the closed-position counterfactual pool (Task 4B
    module section), a NULL/legacy value reading as TIGHTEN_SL - and calls
    the existing, unmodified `record_guardian_authority_heuristic_candidate_
    validation` with the outcome: `VALIDATED` if both splits clear the
    sample-size/miscalibration bar and agree in sign, `REJECTED` with a
    specific reason otherwise.

    Returns the count of candidate rows actually transitioned (VALIDATED +
    REJECTED) in THIS call - `record_guardian_authority_heuristic_
    candidate_validation`'s own `WHERE status = 'PROPOSED'` guard makes a
    row some concurrent/earlier call already transitioned a structural
    no-op, not counted here (same idempotency discipline as `propose_
    candidate_heuristics`'s own `saved` count above)."""
    candidates = repo.find_proposed_guardian_authority_heuristic_candidates()
    if not candidates:
        return 0

    # Both pools are built and split ONCE per call, independently of each
    # other and of which candidates are pending (Task 4B). Each split is its
    # own chronological 70/30 over its own timestamp domain - the two are
    # never merged and never share a boundary; see the Task 4B module
    # section above.
    splits_by_target = {
        _TARGET_TIGHTEN_SL: _split_pool_chronologically(_tighten_sl_evidence_pool(repo)),
        _TARGET_PRE_ENTRY_VETO: _split_pool_chronologically(_pre_entry_veto_evidence_pool(repo)),
    }

    processed = 0
    for candidate in candidates:
        # The candidate's own, unmodified condition_json - never a
        # hand-modified copy (see this module's self-review discipline).
        condition = json.loads(candidate["condition_json"])
        # The candidate's own declared target decides its pool, and nothing
        # else: a TIGHTEN_SL candidate is never measured against closed-
        # position PnL, and a PRE_ENTRY_VETO candidate is never measured
        # against Guardian Authority's own decision history. A value that
        # names no pool at all (unreachable through the schema's own
        # Literal, so only a future writer bypassing it could produce one)
        # gets an EMPTY pool and is therefore REJECTED on sample size -
        # "untested stays untested, never evidence" is the conservative
        # outcome here, not a silent re-route into whichever pool happens to
        # be richest.
        target = candidate.get("target_decision_type") or _TARGET_TIGHTEN_SL
        train_rows, test_rows = splits_by_target.get(target, ([], []))
        train_n, train_rate = _split_stats(train_rows, condition)
        test_n, test_rate = _split_stats(test_rows, condition)
        status, rejected_reason = _validation_outcome(train_n, train_rate, test_n, test_rate)

        if repo.record_guardian_authority_heuristic_candidate_validation(
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
