"""Guardian Authority self-improvement, all four steps: PROPOSE + VALIDATE +
PROMOTE + TRACK/DEMOTE (design spec:
docs/superpowers/specs/2026-09-15-guardian-authority-live-autonomy-design.md,
"What 'self-improvement' concretely means here").

The full pipeline is propose -> validate -> promote -> track/demote. THIS
module proposes (`propose_candidate_heuristics`, Task 3), validates
out-of-sample (`validate_pending_heuristic_candidates`, Task 4), promotes
(`promote_validated_heuristic_candidates`, Task 5) and tracks each promoted
heuristic's own forward record, retiring the ones that degrade
(`track_and_demote_underperforming_heuristics`, Task 6). Everything Task 3
writes lands in one place - `guardian_authority_heuristic_candidates`, status
`PROPOSED` - a table the real decision engine never reads, and Task 4 only
ever moves a row from `PROPOSED` to `VALIDATED` or `REJECTED` in that SAME
table, via the existing, unmodified `record_guardian_authority_heuristic_
candidate_validation`. `evaluate_heuristics` reads
`guardian_authority_heuristics` (a different table), and the only ways
anything from this module reaches it are Task 5's single call to the
existing, unmodified `upsert_guardian_authority_heuristic` - made only for a
row an independent out-of-sample validation has already moved to `VALIDATED`
- and Task 6's single call to the SAME unmodified method, which only ever
writes `adjustment=0.0` (a demotion can silence a rule, never strengthen
one). So an LLM that hallucinates a confident-sounding rule cannot influence
a single real trade from here - it can only queue a hypothesis for a
statistical test it has no way to reach, and its own proposed numbers are
discarded even if that test clears (see Task 5's section below).

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
from decimal import Decimal, InvalidOperation

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.detective.stats import (
    _is_blocked_by_exposure,
    compute_breakdown_by_signal_type,
    compute_guardian_exit_effectiveness,
)
from crypto_trading.guardian.authority import (
    _ADJUSTMENT_SCALE,
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
        # A third, deliberately separate vocabulary (see _TAKE_PROFIT_
        # FACTOR_NAMES's own comment): TAKE_PROFIT candidates may use ONLY
        # these two names, never mixed with either vocabulary above.
        "take_profit_factor_names": _TAKE_PROFIT_FACTOR_NAMES,
        "take_profit_observations": _take_profit_observation_context(repo, closed_positions),
    }


def _release_day_slot(repo: Repository, previous: str | None, now: datetime) -> None:
    """A strategist AI call that failed/timed out/raised produced no answer,
    so it must not burn the once-per-UTC-day slot claimed just before it:
    put back exactly what was there before the claim (2026-09-19: a
    persistent failure used to lock proposals out for the whole day)."""
    if previous is not None:
        repo.set_guardian_authority_strategist_last_proposed_date(previous, now)
    else:
        repo.clear_guardian_authority_strategist_last_proposed_date()


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
        try:
            assessment: GodfatherStrategistAssessment = runner.run(
                agent_def, context, GodfatherStrategistAssessment
            )
        except Exception:
            _release_day_slot(repo, last_proposed, now)
            raise

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
            _release_day_slot(repo, last_proposed, now)
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
        # caller gets 0 instead of an exception. A failure while BUILDING
        # the context still leaves the day's slot claimed; a failed AI call
        # itself already released it above.
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

# ---------------------------------------------------------------------------
# C1 (final whole-branch review, 2026-09-17): the empty-condition guard.
#
# `heuristic_condition_matches` (frozen, unmodifiable by this plan) is an
# `all(...)` over the condition's own keys, so an EMPTY condition `{}` is
# vacuously true for EVERY factors dict - its own module docstring calls that
# an "always-on" heuristic. That is a defensible thing for a hand-written,
# human-reviewed rule to be; it is NOT a defensible thing for an LLM-proposed
# candidate to be, for three compounding reasons the final review proved
# end-to-end against the real repository:
#
# 1. It clears out-of-sample validation TRIVIALLY. `_split_stats` filters each
#    split by the condition, so an empty condition filters nothing: train and
#    test are the WHOLE pool, and the bar degenerates to "is the bot's own
#    base rate outside [0.35, 0.65]". Any consistently losing strategy passes
#    that - the candidate would be certified by a test that measured nothing
#    about the candidate itself.
# 2. It escapes Cap B's dilution. The rescale divides by the live family
#    size, and the FIRST promotion lands at family_size == 1 - i.e. at full
#    earned strength, with no dilution at all.
# 3. Promoted, it is structurally un-demotable on the veto track. The real
#    `guardian_authority_heuristics` table has no `target_decision_type`
#    column and `evaluate_heuristics` reads every row in it, so an
#    always-true row matches every pre-entry decision AND every tick-time
#    decision; every entry it successfully vetoes is a position that never
#    opens, never closes, and therefore never appears in the forward
#    closed-position pool that would have to accumulate the evidence to
#    retire it. It silences itself into permanence.
#
# The guard is therefore placed at the EARLIEST point the pipeline can see
# such a row - before any pool is consulted - and routed through the exact
# same `record_guardian_authority_heuristic_candidate_validation` call every
# other outcome uses (no second rejection path). `promote_validated_heuristic_
# candidates` carries a mirror of it for defense in depth, against a row that
# was already `VALIDATED` before this guard existed.
#
# Non-dict conditions (a JSON list, a bare `null`, a string) are refused by
# the SAME check, for a stronger reason still: `heuristic_condition_matches`
# raises `AttributeError` on them, so such a row is not merely over-broad but
# structurally unusable.
# ---------------------------------------------------------------------------
_EMPTY_CONDITION_REJECTION_REASON = "empty or non-object condition (vacuously always-true)"


def _is_usable_condition(condition: object) -> bool:
    """A condition that can genuinely discriminate: a dict with at least one
    key. See the C1 section above for why `{}` specifically must never reach
    `VALIDATED` or the real heuristics table."""
    return isinstance(condition, dict) and bool(condition)


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
_TARGET_TAKE_PROFIT = "TAKE_PROFIT"


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


def _take_profit_evidence_pool(repo: Repository) -> list[tuple[str, dict, bool]]:
    """`(observed_at, profit_factors, would_take_profit_have_been_correct)`
    for every real `guardian_observations` row belonging to a real CLOSED
    position - available at cold start exactly like
    `_pre_entry_veto_evidence_pool` above, and for the same structural
    reason: a TAKE_PROFIT heuristic strong enough to actually fire prevents
    a position from generating any FURTHER observations, but every
    observation recorded on any closed position - including one where no
    TAKE_PROFIT heuristic has ever existed - is real, already-persisted
    ground truth, independent of whether the pipeline has ever proposed a
    TAKE_PROFIT candidate at all.

    `profit_factors` is exactly `{"progress_ratio": ..., "unrealized_pnl_
    positive": ...}` - the SAME two-field vocabulary `decide_take_profit`
    itself is evaluated against (see that function's own docstring), read
    straight off the observation's own persisted `progress_ratio`/
    `unrealized_pnl` columns, never recomputed. The outcome is `this
    observation's own unrealized_pnl > the position's own real, eventual
    realized PnL` (via the UNMODIFIED `compute_pnl`) - i.e. "would closing
    right here have captured more than what the position's real, eventual
    close actually captured?" Nothing is synthesized: every factor and
    every PnL input is the row's own real, persisted data - the only
    counterfactual thing is the TAKE_PROFIT ACTION ITSELF (which never
    fired for these observations), the same kind of counterfactual
    `_pre_entry_veto_evidence_pool` above already treats as legitimate
    evidence.

    Skips: a CLOSED position with no `closed_at` (defensive - mirrors the
    veto pool's own skip; no production path produces this), an exposure-
    blocked position (its `compute_pnl` is `0 - fees - funding`, which
    would systematically bias this pool's `>` comparison toward "TAKE_
    PROFIT would have been correct" for reasons that have nothing to do
    with the position's own real market behavior - same reasoning
    `_pre_entry_veto_evidence_pool` documents for its own identical skip),
    and an observation row whose `unrealized_pnl`/`progress_ratio` cannot
    be parsed (defensive - no production path produces this; skip, never
    guess)."""
    pool: list[tuple[str, dict, bool]] = []

    for position in repo.find_closed_positions():
        if position.closed_at is None:
            continue
        if _is_blocked_by_exposure(position):
            continue
        eventual_pnl = compute_pnl(position)
        for observation in repo.find_guardian_observations_for_position(position.position_id):
            try:
                observed_pnl = Decimal(observation["unrealized_pnl"])
                progress_ratio = float(observation["progress_ratio"])
            except (TypeError, ValueError, InvalidOperation):
                continue
            pool.append(
                (
                    observation["observed_at"],
                    {
                        "progress_ratio": progress_ratio,
                        "unrealized_pnl_positive": observed_pnl > Decimal("0"),
                    },
                    observed_pnl > eventual_pnl,
                )
            )

    return pool


def _take_profit_observation_context(
    repo: Repository, positions: list[Position]
) -> list[dict]:
    """Real per-observation evidence for the LLM to reason about TAKE_PROFIT
    patterns from, for the SAME already-windowed `positions` list
    `_build_context` uses everywhere else (no additional unbounded read) -
    exposure-blocked positions excluded via the SAME unmodified `_is_
    blocked_by_exposure`, for the SAME reason `_closed_position_entry_
    outcomes` excludes them. Each entry is one real `guardian_observations`
    row: `progress_ratio`/`unrealized_pnl` read straight off that row's own
    persisted columns (the SAME two values `decide_take_profit`'s own
    factors are built from), paired with the position's own real, eventual
    `pnl_usdt`/`exit_reason` - the exact same shape `_take_profit_evidence_
    pool` validates against, so what the model reasons about and what it is
    later graded against are the same view of the same data."""
    observations: list[dict] = []
    for position in positions:
        if _is_blocked_by_exposure(position):
            continue
        eventual_pnl = compute_pnl(position)
        for observation in repo.find_guardian_observations_for_position(position.position_id):
            observations.append(
                {
                    "progress_ratio": observation["progress_ratio"],
                    "unrealized_pnl": observation["unrealized_pnl"],
                    "observed_at": observation["observed_at"],
                    "eventual_pnl_usdt": str(eventual_pnl),
                    "eventual_exit_reason": position.exit_reason,
                }
            )
    # Bounded to _MAX_EVIDENCE_ROWS TOTAL (2026-09-18 production fix): the
    # caller's own `positions` list is already windowed to at most
    # _MAX_EVIDENCE_ROWS POSITIONS, but a position accumulates one
    # guardian_observations row per tick for its entire open lifetime
    # (guardian.check_interval_seconds, ~60s) - a handful of positions held
    # for hours each already produces thousands of rows, and real
    # production history hit over a MILLION prompt tokens this way (a live
    # godfather_strategist_failed/"prompt is too long" 400 error, not a
    # theoretical concern), permanently blocking every GODFATHER Strategist
    # proposal call from ever succeeding again. Reuses the same
    # _most_recent_rows this module's other context lists already apply -
    # most-recent-150 BY OBSERVATION, not by position, so the LLM still sees
    # the freshest real behavior regardless of how unevenly it's
    # distributed across positions.
    return _most_recent_rows(observations, "observed_at")


# The TAKE_PROFIT vocabulary is fixed, unlike the other two lists above
# (which are derived from whatever has actually occurred): progress_ratio
# and unrealized_pnl are computed every single tick regardless of whether
# any TAKE_PROFIT heuristic has ever existed, so the vocabulary a proposal
# may use is always exactly these two names, cold start or not.
_TAKE_PROFIT_FACTOR_NAMES = ["progress_ratio", "unrealized_pnl_positive"]


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

    An empty or non-object `condition_json` is REJECTED up front, before any
    pool is consulted (C1, final whole-branch review - see that section
    above): such a condition is vacuously true for every factors dict, so it
    would otherwise be "validated" by a test that filtered nothing.

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
        _TARGET_TAKE_PROFIT: _split_pool_chronologically(_take_profit_evidence_pool(repo)),
    }

    processed = 0
    for candidate in candidates:
        # The candidate's own, unmodified condition_json - never a
        # hand-modified copy (see this module's self-review discipline).
        condition = json.loads(candidate["condition_json"])

        # C1: an empty/non-object condition is rejected HERE, before any pool
        # is consulted, so it can never reach VALIDATED - see the C1 section
        # above for the full reasoning. Every sample size and rate is written
        # as 0: nothing was measured, and the row must never read as if
        # something had been.
        if not _is_usable_condition(condition):
            if repo.record_guardian_authority_heuristic_candidate_validation(
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


# ---------------------------------------------------------------------------
# Task 5 (2026-09-15, Guardian Authority Live Autonomy): PROMOTION - step 3 of
# 4 (propose -> validate -> PROMOTE -> track/demote), and the one and only
# point in this whole plan where anything crosses from the isolated candidates
# table into the REAL `guardian_authority_heuristics` table the live decision
# core reads on every single decision.
#
# --------------------------------------------------------------------------
# What is written, and where every number comes from
# --------------------------------------------------------------------------
# The write itself is the existing, unmodified, already-reviewed
# `repo.upsert_guardian_authority_heuristic` - the same narrow path
# authority.py's own self-critique pass has always used, called here from
# exactly ONE new call site (the plan's own Global Constraint: a candidate can
# reach that table by no other route, and this task adds no second route).
#
# - `heuristic_id`: `f"ga-llm:{candidate_id}"` - deterministic, so a repeated
#   promotion of the same candidate can only ever REPLACE its own row, never
#   accumulate near-duplicates, and so a reader can tell at a glance which
#   family a live heuristic belongs to (`ga-hc:state:*` = authority.py's
#   self-critique pass, `ga-llm:*` = this pipeline). The prefix is also what
#   makes the co-firing cap below grep/count-provable.
# - `adjustment`/`confidence`: derived from the candidate's own TEST-split
#   numbers with the SAME formulas `update_heuristics_from_resolved_decisions`
#   uses (`deviation = test_correct_rate - 0.5`, `adjustment = deviation *
#   _ADJUSTMENT_SCALE`, `confidence = abs(deviation) * 2.0` - the constant
#   imported from authority.py, never re-derived), then scaled by the
#   co-firing cap below. TEST, never train: the held-out numbers are what
#   earned the promotion and are the only ones that describe the rule's real,
#   out-of-sample strength.
# - NEVER `proposed_adjustment`. That column is the proposing model's own
#   informational rationale, kept for audit only - the live table's numbers
#   are measured from real outcomes, not asserted by the proposer. This is
#   the single most important property of this function: an LLM cannot set
#   the strength of its own rule, only nominate a condition for measurement.
# - `condition_json`: the candidate's own string, verbatim - not re-parsed
#   and re-serialized, so what the live table matches on is byte-identical to
#   what validation measured.
# - `sample_size`: `test_sample_size`, matching the split the adjustment and
#   confidence come from (the row would otherwise advertise a sample size
#   that never produced its own numbers).
#
# --------------------------------------------------------------------------
# R3 - the co-firing cap (design spec "Addendum (2026-09-16)", R3)
# --------------------------------------------------------------------------
# `evaluate_heuristics` (frozen, unmodifiable by this plan) SUMS the
# adjustment of every matching heuristic. The self-critique family is safe
# from that by construction - `_groups_for_factors`' own I1 hardening makes
# `ga-hc:state:*` ids 1:1 with `guardian_state`, so at most one can ever match
# one decision. This family has no such structure: conditions are written by
# a model, over a shared factor vocabulary, and nothing stops N of them from
# matching the same decision and summing to N times a single rule's worth -
# the exact multi-heuristic escalation I1 closed.
#
# Chosen mechanism (Cap B in the brief's taxonomy - an evaluation-time
# contribution cap achieved entirely by what gets WRITTEN, with zero changes
# to `evaluate_heuristics`): **every live `ga-llm:*` heuristic's stored
# `adjustment` is its own earned adjustment divided by the number of live
# `ga-llm:*` heuristics OF ITS OWN `target_decision_type`.** Each promotion
# pass rewrites the whole live family with the new divisors, so the invariant
# holds continuously, not just at the moment of a promotion.
#
# THE DIVISOR IS PER TARGET TYPE (review finding I1, 2026-09-17 - this
# replaced a whole-family divisor; see `_per_target_family_sizes`). The
# original whole-family divisor was chosen because a PRE_ENTRY_VETO-targeted
# condition and a TIGHTEN_SL-targeted one "can still co-fire on one factors
# dict (trivially so for an empty/permissive condition)". Two things have
# changed since, and together they make cross-type co-firing provably
# impossible rather than merely unlikely:
#   (a) C1's guard means a promoted condition can never be empty - it is a
#       dict with at least one key;
#   (b) a candidate only reaches VALIDATED by matching at least
#       `_MIN_SAMPLE_SIZE` rows of ITS OWN pool, which requires every key in
#       its condition to be present in that pool's factor vocabulary (matching
#       is fail-closed on a missing key). The two vocabularies share no field
#       name at all - `_pre_entry_factors` is exactly `{instrument,
#       candidate_score, trigger_reasons}`, while a reconstructed TIGHTEN_SL
#       factors dict is exactly the configured decay factors (`time_decay`,
#       `momentum_decay`, `volume_decay`, `funding_decay`,
#       `secondary_confirmation_lost`, `market_regime`) plus `guardian_state`.
# So a promoted rule of one type carries at least one key the other type's
# factors dict never contains, and therefore can never match it. Each family
# only ever co-fires with itself, and Cap B's bound ("the family contributes
# an AVERAGE, never a sum") holds per family - which is what makes the
# TIGHTEN_SL cardinality cap's own arithmetic actually true (see that
# constant's section below).
#
# The honest residual: (b) rests on the two vocabularies staying disjoint. If
# a future change ever gave `_pre_entry_factors` a field name that also
# appears in a guardian observation's factors (or vice versa), a cross-type
# match would become possible again and this divisor would need revisiting.
# That is a grep-checkable property of two small, explicit field lists, not a
# statement about what the model might propose.
#
# The bound this buys, stated exactly: with M live rows, each storing
# `raw_i / M`, the sum over ANY subset that co-fires on ANY factors dict is at
# most `sum_i |raw_i| / M`, which is the family's MEAN magnitude and therefore
# `<= max_i |raw_i|` - the strongest single member's own full worth. In other
# words the family contributes an AVERAGE, never a sum: promoting more
# heuristics can never make the family louder than its single loudest member,
# which is precisely "multiple heuristics can never escalate a decision merely
# by co-firing". It is provable from this diff alone by counting: every
# `ga-llm:*` row in the table is written by the one loop below, and that loop
# divides by the number of rows it writes.
#
# Why this and not Cap A (a hard cap on the promoted count):
# - A per-`target_decision_type` count cap would NOT be provable. The real
#   heuristics table has no `target_decision_type` column and
#   `evaluate_heuristics` reads every row in it, so a PRE_ENTRY_VETO-targeted
#   condition and a TIGHTEN_SL-targeted one can still co-fire on one factors
#   dict (trivially so for an empty/permissive condition); proving they cannot
#   would require reasoning about the model's conditions staying disjoint -
#   the one thing the brief explicitly forbids relying on. (SUPERSEDED in
#   part by the I1 note above: with C1's guard in place the proof rests on the
#   two FACTOR VOCABULARIES being disjoint - two short, explicit field lists
#   in this codebase - not on the model's conditions being disjoint. That is
#   what made the per-target divisor sound; a per-target COUNT cap is still
#   not the mechanism chosen here.)
# - A GLOBAL count cap of 1 would be provable, but it freezes the pipeline at
#   a single live LLM-authored rule forever (a second one can only ever exist
#   after the first degrades enough for Task 6 to demote it), which is a much
#   larger amputation of the self-improvement the spec is built around.
# (The first bullet is also what originally made the divisor whole-family
# rather than per-target; the I1 note above is where that reasoning was
# re-derived and superseded.)
#
# The cost, stated honestly: an individual heuristic gets quieter as the
# family grows (with 4 live rules, a lone matching rule contributes a quarter
# of its earned strength, below `authority_tighten_threshold`'s 0.15 default
# for any realistic deviation). The family speaks at full strength only when
# its members AGREE on a decision. That is the conservative direction the
# user asked for - a quiet heuristic can only fail to intervene, never
# over-intervene.
#
# And one asymmetry in that cost that must NOT be glossed over (review
# finding, Task 5): Task 6's demotion frees a divisor slot only for a
# heuristic that has actually FIRED and resolved. Its TIGHTEN_SL
# forward-tracking counts only resolved decisions with
# `intervention_applied` true, so a TIGHTEN_SL-targeted member that this
# rescale has diluted below `authority_tighten_threshold` can never fire
# again, can therefore never accumulate a single forward sample, and can
# therefore never be demoted through that path - an absorbing state. It is a
# SAFE absorbing state (a silent heuristic cannot over-intervene), but it
# means the TIGHTEN_SL half of this family can only ever grow, never shrink,
# via demotion. PRE_ENTRY_VETO-targeted members do not have this problem:
# their forward-tracking uses the closed-position counterfactual pool
# (Task 4B's `_pre_entry_veto_evidence_pool`), which measures a condition
# against real closed positions whether or not the heuristic ever fired. The
# accepted resolution is a TIGHTEN_SL-specific cardinality cap in Task 6's
# own scope, not a change here.
#
# Two ordering details the cap depends on:
# - Already-live rows are rewritten (downward, to the new, larger divisor)
#   BEFORE the newly promoted ones are added, so even the transient state in
#   the middle of the loop is never louder than the bound.
# - A DEMOTED candidate (Task 6 sets `demoted_at` and upserts its heuristic to
#   `adjustment = 0.0`) is skipped entirely: never counted in the divisor (it
#   contributes nothing to any sum) and, more importantly, never rewritten -
#   rescaling it would resurrect a rule that forward performance already
#   retired.
#
# Error handling follows `validate_pending_heuristic_candidates`' precedent
# directly above (no blanket try/except): this is a pure DB-in/DB-out batch
# step with no AI call, no budget gate and no watermark, structurally the same
# kind of function as `update_heuristics_from_resolved_decisions`, which
# likewise lets a failure propagate to its caller rather than silently
# half-promoting.
# ---------------------------------------------------------------------------

# The live-heuristic id prefix that IS this family - see the R3 section above.
_LLM_HEURISTIC_ID_PREFIX = "ga-llm:"

# --------------------------------------------------------------------------
# The TIGHTEN_SL cardinality cap (added by Task 6 - see the design spec's
# "Addendum (2026-09-16)", R3, consequence 1, and the note at the end of the
# Task 5 section above that predicted this exact follow-up).
# --------------------------------------------------------------------------
# WHY IT LIVES HERE AND NOT IN TASK 6's OWN FUNCTION: it is a decision about
# whether a promotion may happen, made from the state that exists at the
# instant of promotion. Task 6 demotes; it has no say over what gets written
# in the first place, and a cap enforced anywhere else could only ever notice
# a violation after the over-cap row was already live for the real decision
# core. The brief's own wording ("checked at promotion time... naturally Task
# 5's own promotion function") and the mechanics agree.
#
# WHAT IT FIXES: Cap B above divides every live `ga-llm:*` heuristic's stored
# adjustment by the size of the whole live family. A TIGHTEN_SL-targeted
# member diluted below `authority_tighten_threshold` can never fire again;
# Task 6's TIGHTEN_SL forward-tracking counts only resolved decisions with
# `intervention_applied` true, so it can never accumulate a forward sample,
# so it can never be demoted - an absorbing state that only grows as more
# TIGHTEN_SL candidates are promoted. PRE_ENTRY_VETO members are structurally
# immune (Task 6 tracks them against real closed positions, whether or not
# they ever fired), which is why the cap is TIGHTEN_SL-only.
#
# WHY 3, specifically:
# - The binding number is what a TIGHTEN_SL heuristic needs in order to still
#   be ABLE to fire: its stored adjustment is `raw / tighten_sl_family_size`
#   and `decide_open_position` requires that to STRICTLY exceed `authority_
#   tighten_threshold` (0.15 by default). The largest `raw` the validation bar
#   can ever produce is `(1.0 - 0.5) * _ADJUSTMENT_SCALE = 0.5` (a perfect
#   test split). At a family of 3 that stores 0.1667 > 0.15 and can still
#   fire; at a family of 4 it stores 0.125 and NO TIGHTEN_SL heuristic,
#   however strong, can ever fire again. So 3 is the largest cap at which a
#   way out of the absorbing state described above still exists at all - the
#   whole point of having a cap at all. (Stated precisely rather than
#   loosely: at a family of 3 only a near-perfect rule - test_correct_rate
#   above ~0.95 - clears the threshold; the cap guarantees the possibility,
#   not that every member will use it.)
# - It is deliberately conservative in the direction the brief names: "too
#   few TIGHTEN_SL heuristics can ever be promoted" is a missed opportunity
#   (the candidate stays VALIDATED and is promoted the moment a slot frees),
#   while unbounded accumulation of stuck ones permanently degrades every
#   other member's signal through the shared divisor. Same conservative
#   framing as `_MIN_SAMPLE_SIZE = 30`'s own.
# - It is not 1: that would freeze the TIGHTEN_SL track at a single live rule
#   forever, the same amputation the Task 5 section above rejected when it
#   chose Cap B over a global count cap of 1.
#
# WHAT THIS CAP DOES AND DOES NOT BOUND (review finding I1, 2026-09-17 -
# this section used to concede that it bounded neither):
# - Since I1, Cap B's divisor is PER target decision type
#   (`_per_target_family_sizes`), so a TIGHTEN_SL member's divisor counts
#   ONLY live TIGHTEN_SL members and this cap therefore genuinely bounds that
#   divisor at 3. The arithmetic in "WHY 3" above is now true as stated:
#   PRE_ENTRY_VETO promotions, however many, cannot dilute a TIGHTEN_SL
#   member at all. (Before I1 the cap capped the TIGHTEN_SL half while the
#   divisor counted both halves, so one live veto member was enough to push
#   every TIGHTEN_SL member into the exact absorbing state this cap exists to
#   prevent - the cap's own comment claimed a guarantee the code did not
#   provide.)
# - What it still does NOT bound: how strong a TIGHTEN_SL member has to be to
#   use its way out. At a full family of 3 only a near-perfect rule clears
#   `authority_tighten_threshold`; a weaker member is silent (safe, but it
#   cannot accumulate forward evidence through the firing-based TIGHTEN_SL
#   track). Since I1's sibling fix I2, that member is no longer stuck
#   forever: the time-based "no forward evidence" demotion path in
#   `track_and_demote_underperforming_heuristics` retires a heuristic that
#   accumulates zero forward samples for `_FORWARD_MAX_SILENT_DAYS`, which
#   frees its slot.
#
# REFUSAL, NOT FAILURE: an over-cap candidate is left exactly as it was -
# `VALIDATED`, unwritten, unpromoted - and is picked up by the next promotion
# pass after a demotion frees a slot. Same refusal-not-failure pattern as
# every other gate in this pipeline; nothing is rejected, nothing is lost.
_MAX_LIVE_TIGHTEN_SL_HEURISTICS = 3


def _llm_heuristic_id(candidate_id: str) -> str:
    return f"{_LLM_HEURISTIC_ID_PREFIX}{candidate_id}"


def _target_decision_type(candidate: dict) -> str:
    """The candidate's own declared target, with Task 4B's documented legacy
    default. Read through ONE helper everywhere (validation routing reads the
    same `or _TARGET_TIGHTEN_SL`), so the cap below, Task 6's forward
    tracking and the validation router can never disagree about what a NULL
    row is."""
    return candidate.get("target_decision_type") or _TARGET_TIGHTEN_SL


def _per_target_family_sizes(live: list[dict], incoming: list[dict]) -> dict[str, int]:
    """Cap B's divisor, PER `target_decision_type` (review finding I1,
    2026-09-17): how many live `ga-llm:*` heuristics of each target type there
    will be once this pass finishes. A TIGHTEN_SL member is divided by the
    TIGHTEN_SL count only, a PRE_ENTRY_VETO member by the PRE_ENTRY_VETO count
    only - see the I1 section in the Task 5 module comment above for why that
    is sound (disjoint vocabularies + fail-closed matching + C1's
    empty-condition guard) and what it fixes (the cardinality cap's own
    arithmetic, which was being defeated by veto members counting toward a
    TIGHTEN_SL member's divisor)."""
    sizes: dict[str, int] = {}
    for row in (*live, *incoming):
        target = _target_decision_type(row)
        sizes[target] = sizes.get(target, 0) + 1
    return sizes


def _within_tighten_sl_cardinality_cap(live: list[dict], incoming: list[dict]) -> list[dict]:
    """`incoming` minus the TIGHTEN_SL-targeted candidates that would push the
    live TIGHTEN_SL family past `_MAX_LIVE_TIGHTEN_SL_HEURISTICS` - see the
    section above. `live` is the SAME "live" this function's own rescale uses
    (`_live_promoted_llm_candidates`: promoted, `ga-llm:*`, `demoted_at IS
    NULL`), so a demotion frees a slot the moment it lands. PRE_ENTRY_VETO
    candidates pass through untouched, and never consume a slot."""
    slots = _MAX_LIVE_TIGHTEN_SL_HEURISTICS - sum(
        1 for row in live if _target_decision_type(row) == _TARGET_TIGHTEN_SL
    )

    accepted: list[dict] = []
    for row in incoming:
        if _target_decision_type(row) != _TARGET_TIGHTEN_SL:
            accepted.append(row)
            continue
        if slots <= 0:
            log_event(
                row["run_id"],
                event="ga_llm_tighten_sl_promotion_refused_at_cap",
                candidate_id=row["candidate_id"],
                cap=_MAX_LIVE_TIGHTEN_SL_HEURISTICS,
            )
            continue
        slots -= 1
        accepted.append(row)
    return accepted


def _with_usable_conditions(validated: list[dict]) -> list[dict]:
    """`validated` minus every row whose `condition_json` is empty, non-object
    or unparseable - C1's promotion-time MIRROR of the validation guard (see
    the C1 section above). This can only ever fire for a row that reached
    `VALIDATED` WITHOUT passing today's validation guard: a legacy row from
    before it existed, or one some future writer transitions by another route.
    It is deliberately a second, independent check rather than trust in the
    first: this is the last point before a row acts on real capital.

    REFUSAL, NOT FAILURE, exactly like the cardinality cap below: the row is
    left `VALIDATED`, unwritten and unpromoted, and the refusal is logged. It
    is not marked REJECTED here - `record_..._validation` only transitions
    from `PROPOSED`, and inventing a second status-write path for it would be
    precisely the extra write path into this table the plan forbids.

    An unparseable `condition_json` is refused rather than raised on: promotion
    is a batch pass, and one corrupt row must not be able to block every other
    candidate's promotion. (Validation's own stance differs deliberately - it
    parses outside any try/except, letting a corrupt row fail loudly, which is
    the sibling self-critique functions' documented precedent. Here the
    conservative action - don't write it into the live table - is available
    without failing anything at all.)"""
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
            event="ga_llm_promotion_refused_unusable_condition",
            candidate_id=row["candidate_id"],
            reason=_EMPTY_CONDITION_REJECTION_REASON,
            condition_json=row["condition_json"],
        )
    return usable


def _live_promoted_llm_candidates(repo: Repository) -> list[dict]:
    """The promoted candidates whose real heuristic row is still live: not
    demoted, and carrying a `ga-llm:*` heuristic id (the only ids this
    function ever writes - the prefix check keeps the co-firing divisor
    provably about THIS family and nothing else)."""
    return [
        row
        for row in repo.find_promoted_guardian_authority_heuristic_candidates()
        if row["demoted_at"] is None
        and str(row["promoted_heuristic_id"] or "").startswith(_LLM_HEURISTIC_ID_PREFIX)
    ]


def _write_llm_heuristic(
    repo: Repository, candidate: dict, heuristic_id: str, family_size: int, now: datetime
) -> None:
    """THE single call site this task adds to `upsert_guardian_authority_
    heuristic` - shared by the newly-promoted rows and by the rescale of the
    already-live ones deliberately, so that the plan's "exactly one new write
    path into the real table" constraint stays literally true and every
    `ga-llm:*` row in existence provably carries the same
    `raw / family_size` scaling. See the Task 5 module section above for
    where each value comes from and why `family_size` divides the
    adjustment (and only the adjustment - `confidence` describes how sure the
    test split is about the rule itself, which co-firing does not change)."""
    deviation = float(candidate["test_correct_rate"]) - 0.5
    repo.upsert_guardian_authority_heuristic(
        heuristic_id=heuristic_id,
        description=candidate["description"],
        condition_json=candidate["condition_json"],
        adjustment=deviation * _ADJUSTMENT_SCALE / family_size,
        confidence=abs(deviation) * 2.0,
        sample_size=int(candidate["test_sample_size"]),
        updated_at=now,
    )


def promote_validated_heuristic_candidates(repo: Repository, now: datetime) -> int:
    """Promotion gate (Task 5). Writes every currently-`VALIDATED` candidate
    into the real `guardian_authority_heuristics` table via the existing,
    unmodified `upsert_guardian_authority_heuristic`, then transitions its
    candidate row `VALIDATED -> PROMOTED` via Task 1's own one-time,
    `WHERE status = 'VALIDATED'`-guarded `promote_guardian_authority_
    heuristic_candidate`. Returns the number of candidates actually promoted
    by THIS call (a row some concurrent/earlier call already promoted is a
    structural no-op and is not counted - same idempotency discipline as the
    two functions above).

    Every already-live `ga-llm:*` heuristic is rewritten in the same pass
    with the new family divisor, which is what keeps the R3 co-firing cap
    true continuously rather than only at the instant of a promotion; see
    the Task 5 module section above for the mechanism and its proof.

    A candidate whose `condition_json` is empty, non-object or unparseable is
    refused outright (C1's promotion-time mirror - see `_with_usable_
    conditions`), and a TIGHTEN_SL-targeted candidate is additionally refused
    (left VALIDATED, counted in neither the return value nor the divisor) when
    the live TIGHTEN_SL family already stands at `_MAX_LIVE_TIGHTEN_SL_HEURISTICS` -
    see that constant's own section above for why the cap exists, why it
    lives here, and why its value is 3."""
    validated = _with_usable_conditions(
        repo.find_validated_guardian_authority_heuristic_candidates()
    )
    if not validated:
        # Nothing to promote means nothing to rescale either: the live family
        # is unchanged, so its existing divisor is still correct.
        return 0

    # Sorted by candidate_id purely for deterministic write order - the
    # values written do not depend on it, but WHICH TIGHTEN_SL candidates win
    # the last free slots of the cap below does, so the order is pinned
    # rather than left to the database's own row order. Already-live rows
    # first: see the ordering note in the module section above.
    live = sorted(_live_promoted_llm_candidates(repo), key=lambda row: row["candidate_id"])
    incoming = _within_tighten_sl_cardinality_cap(
        live, sorted(validated, key=lambda row: row["candidate_id"])
    )
    if not incoming:
        # Every validated candidate was refused by the cap: the live family is
        # unchanged, so - exactly as in the empty-queue case above - its
        # existing divisor is still correct and nothing must be rewritten.
        return 0

    family_sizes = _per_target_family_sizes(live, incoming)

    for row in live:
        _write_llm_heuristic(
            repo,
            row,
            row["promoted_heuristic_id"],
            family_sizes[_target_decision_type(row)],
            now,
        )

    promoted = 0
    for row in incoming:
        heuristic_id = _llm_heuristic_id(row["candidate_id"])
        _write_llm_heuristic(
            repo, row, heuristic_id, family_sizes[_target_decision_type(row)], now
        )
        if repo.promote_guardian_authority_heuristic_candidate(
            row["candidate_id"], heuristic_id, now
        ):
            promoted += 1

    return promoted


# ---------------------------------------------------------------------------
# Task 6 (2026-09-15, Guardian Authority Live Autonomy): FORWARD-PERFORMANCE
# TRACKING + AUTO-DEMOTION - step 4 of 4 (propose -> validate -> promote ->
# TRACK/DEMOTE), and Acceptance Criterion 5's whole mechanism: "a promoted
# heuristic whose real forward performance degrades gets demoted (adjustment
# set to 0.0) automatically, with full audit trail".
#
# --------------------------------------------------------------------------
# What "forward" means, and why every track is windowed to promoted_at
# --------------------------------------------------------------------------
# A promoted heuristic earned its place on a HELD-OUT TEST SPLIT of history
# that existed before it did. This step asks a different, harder question: how
# has the rule done since it started acting? Every sample counted below is
# therefore strictly AFTER the candidate's own `promoted_at` - never a
# decision or a closed position from before the rule existed. Including
# pre-promotion evidence would just re-measure (a subset of) what validation
# already measured and would blunt exactly the degradation this step exists to
# catch.
#
# --------------------------------------------------------------------------
# Two tracks, because the two decision types leave two different traces
# --------------------------------------------------------------------------
# TIGHTEN_SL: the heuristic's real fired decisions DO resolve (a tightened
# stop either helped or did not), so the track record is the heuristic's own
# resolved `guardian_authority_decisions` rows, attributed through the
# `matched_heuristic_ids_json` column Task 2 populates at the orchestration
# layer. Three filters are inherited verbatim from the two existing consumers
# of that table (`update_heuristics_from_resolved_decisions` and this module's
# own `_tighten_sl_evidence_pool`), not re-invented:
#   - `decision_type == "TIGHTEN_SL"` and a non-null `expectation_correct`
#     (CLOSE_EARLY resolves with None; PRE_ENTRY_VETO never resolves at all);
#   - `bool(row.get("intervention_applied"))` - the R1 fix, and the single
#     most important filter here. One losing position sitting above the
#     tighten threshold re-saves a decision EVERY tick, and every one of those
#     rows resolves to the same outcome from the same trade. Without this
#     filter a single position could clear the forward sample-size bar on its
#     own and retire a heuristic on the evidence of one trade. `bool(...)`
#     rather than `is True` for the reason both siblings document: SQLite has
#     no boolean type, so a stored True round-trips as the int 1.
#
# PRE_ENTRY_VETO: its real fired decisions can NEVER resolve. `resolve_
# pending_decisions` permanently skips a PRE_ENTRY_VETO row's counterfactual
# by the original Guardian Authority plan's own deliberate, documented scope
# limit ("this task has no market-data infrastructure to evaluate it... Not a
# bug, not a TODO"), so the attribution-based track above would give this half
# of the family ZERO forward-performance safety net the moment it starts
# acting on real capital. The 2026-09-16 addendum closes that with the SAME
# mechanism Task 4B already built and reviewed for validation: `_pre_entry_
# veto_evidence_pool` (unmodified, no new call path near it, still read-only),
# windowed to `closed_at > promoted_at` and filtered by the promoted
# heuristic's OWN `condition_json` through the real, unmodified
# `heuristic_condition_matches` - literally validation's question, re-asked
# forward. No new mechanism is invented for this task.
#
# Each promoted heuristic is measured ONLY against the pool its own
# `target_decision_type` names - the same "untested stays untested, never
# evidence" routing Task 4B established, via the same `_target_decision_type`
# helper, so a NULL/legacy row reads as TIGHTEN_SL here exactly as it does at
# validation and at the cardinality cap.
#
# --------------------------------------------------------------------------
# The bar: 15 samples, correct_rate < 0.4 - and why neither is the validation
# threshold
# --------------------------------------------------------------------------
# `_FORWARD_MIN_SAMPLE_SIZE = 15`, HALF of validation's `_MIN_SAMPLE_SIZE =
# 30`, and named "canary" on purpose. Validation's job is to keep a weak rule
# OUT, so its sample bar should be hard to clear. This step's job is the
# opposite: to get a rule that is actively costing money OUT, quickly, while
# real forward data accumulates far more slowly than the historical pool
# validation drew on. Per Acceptance Criterion 5 it must be able to catch real
# degradation before 30 more real decisions/closed positions have piled up
# behind it. 15 is still large enough that no single trade can reach it (the
# `intervention_applied` filter above guarantees distinct interventions), and
# the asymmetry is the safe direction: demoting too eagerly costs a missed
# opportunity that promotion can re-earn, while demoting too late costs real
# capital.
#
# `_FORWARD_ADVERSE_DEVIATION = 0.10` is how far the forward evidence must
# have moved AGAINST the heuristic's own prediction before it is retired. It is
# deliberately NOT "anything below what got it promoted" (a rule that merely
# regressed toward 0.5 is uninformative, not harmful - it contributes noise,
# and Cap B already bounds how loud that noise can be) and deliberately NOT 0
# (which would retire half the family on a coin flip). It sits inside
# `_MIN_MISCALIBRATION`'s own 0.15 floor rather than at it: a heuristic that
# has crossed all the way to a 0.15 adverse deviation is already MORE
# miscalibrated, in the wrong direction, than the bar it had to clear to be
# promoted at all - which would be a late canary, not an early one. 0.10 is
# the same order of magnitude as that established floor - "meaningfully
# against it, not merely not-perfect" - while firing one step sooner.
#
# --------------------------------------------------------------------------
# WHY THE BAR IS SIGN-AWARE, AND NOT A FLAT `correct_rate < 0.4` (controller
# ruling, 2026-09-16, after Task 6's first implementation)
# --------------------------------------------------------------------------
# A promoted heuristic's stored `adjustment` carries a DIRECTION, and it can
# legitimately be negative. `promote_validated_heuristic_candidates` derives it
# as `(test_correct_rate - 0.5) * _ADJUSTMENT_SCALE / family_size`, exactly as
# `update_heuristics_from_resolved_decisions` has always done, so a candidate
# whose held-out split said "under this condition the action was usually the
# WRONG call" is promoted with a NEGATIVE adjustment - a rule that pushes AWAY
# from the action. That is a first-class, intended outcome on both tracks: a
# TIGHTEN_SL heuristic can be promoted to discourage tightening, and a
# PRE_ENTRY_VETO heuristic can be promoted to discourage vetoing.
#
# A flat "forward correct_rate below 0.4 -> demote" silently assumes every
# promoted heuristic points the positive way, and gets the negative ones
# exactly backwards. For a negative-adjustment heuristic a LOW forward
# correct_rate is not disconfirming evidence at all - it is CONFIRMING
# evidence: the rule argued against the action, the action happened anyway
# because the rest of the family outvoted it, and the action turning out badly
# is precisely what the rule predicted. The flat bar would retire it for
# continuing to be right, and (worse) retire it fastest exactly when it is most
# right.
#
# So the bar is stated in terms of AGREEMENT with the heuristic's own
# direction, the same way every other calibration judgement in this pipeline
# already is (Task 4/4B's train/test sign-agreement check; Task 5's
# deviation-sign-derives-adjustment-sign rule):
#
#     forward_deviation = forward_correct_rate - 0.5
#     promoted_sign     = +1 if stored adjustment > 0 else -1
#     demote  <=>  sample_size >= 15
#                  and promoted_sign * forward_deviation <= -0.10
#
# `promoted_sign * forward_deviation` is the forward evidence expressed in the
# heuristic's OWN direction: positive means the forward record agrees with what
# the rule predicted, negative means it has swung the other way. Reading off
# the two cases:
#   - positive adjustment -> demote when forward_correct_rate <= 0.40
#     (identical to the flat rule this replaced - no behaviour change at all
#     for the family's positive half);
#   - negative adjustment -> demote when forward_correct_rate >= 0.60, i.e.
#     only when the forward evidence genuinely contradicts it.
#
# The sign is read from the heuristic's REAL stored adjustment (the value
# `evaluate_heuristics` actually sums), not re-derived from the candidate's
# `test_correct_rate` - they provably agree, since the divisor and
# `_ADJUSTMENT_SCALE` are both positive, but the live row is the thing actually
# acting on capital and is therefore what "which way does this rule push"
# should mean. A live, undemoted `ga-llm:*` row can never store exactly 0.0
# (validation's `_MIN_MISCALIBRATION = 0.15` floor guarantees
# `|test_correct_rate - 0.5| >= 0.15` before promotion is possible at all), so
# the `> 0 else -1` split has no meaningful third case; a row whose heuristic
# is missing from the table entirely is skipped rather than guessed at, since
# its direction is unknowable and there is nothing live to silence anyway.
#
# Both constants are shared by both tracks: the sign-blindness this ruling
# fixed was present on BOTH (a PRE_ENTRY_VETO candidate whose condition
# correlates with real WINS is promoted negative, to push away from vetoing
# there, and has the identical failure mode), and there is no reason to hold
# the two decision types to different bars.
#
# --------------------------------------------------------------------------
# The two writes, and why their ORDER is binding (2026-09-16 addendum)
# --------------------------------------------------------------------------
# A demotion is `mark_guardian_authority_heuristic_candidate_demoted` followed
# by `upsert_guardian_authority_heuristic(adjustment=0.0, confidence=0.0,
# sample_size=<forward sample>)` - always both, always in THAT order, never
# one without the other. Nothing is ever deleted: the candidate row stays
# `PROMOTED` (with `demoted_at`/`demotion_reason` filled in) and the real
# heuristic row stays in the table with its own description and condition
# intact, contributing exactly 0.0 to `evaluate_heuristics`' summation - a
# genuine no-op, and a complete audit trail.
#
# The order is not stylistic. The two writes are separately committed (there
# is no shared transaction - same as every other write pair against this
# table), and `promote_validated_heuristic_candidates` above rescales every
# live `ga-llm:*` heuristic on every pass, excluding demoted rows by
# `demoted_at IS NOT NULL`. If the zeroing upsert ran FIRST, a promotion pass
# interleaving between the two writes would still see `demoted_at IS NULL`,
# count the row as live, and rescale its 0.0 back to a nonzero value - which
# the following `mark_..._demoted` would then freeze in place forever, since
# every subsequent promotion pass skips demoted rows. The result would be a
# permanently-live, permanently-non-rescalable heuristic acting on real
# capital after forward performance already retired it. Marking FIRST closes
# the window completely: from the instant `demoted_at` is set, no promotion
# pass - concurrent or later - can touch the row again, whatever the zeroing
# upsert's own timing turns out to be.
#
# `mark_..._demoted`'s own `WHERE status = 'PROMOTED' AND demoted_at IS NULL`
# guard is also what makes this step idempotent: it returns False for a row
# some earlier/concurrent pass already demoted, and this function then skips
# the zeroing write and the count entirely - same "a structural no-op is not
# counted" discipline as the three steps above.
#
# Error handling follows `validate_pending_heuristic_candidates` and
# `promote_validated_heuristic_candidates` directly above (no blanket
# try/except): a pure DB-in/DB-out batch step with no AI call, no budget gate
# and no watermark lets a failure propagate to its caller rather than silently
# half-demoting.
# ---------------------------------------------------------------------------

# The canary thresholds - see the section above for why each is what it is,
# why neither is the validation threshold of the same name, and why the
# deviation bar is measured in the heuristic's OWN direction rather than as a
# flat correct_rate floor.
_FORWARD_MIN_SAMPLE_SIZE = 15
_FORWARD_ADVERSE_DEVIATION = 0.1

# ---------------------------------------------------------------------------
# I2 (final whole-branch review, 2026-09-17): the time-based "no forward
# evidence" demotion path, and why a count-based canary alone is not enough.
#
# THE HOLE IT CLOSES: `_forward_pre_entry_veto_stats` builds its pool from
# real CLOSED positions. A PRE_ENTRY_VETO heuristic strong enough to actually
# veto prevents the position from ever being opened - so every entry it
# successfully blocks is a position that never opens, never closes, and never
# enters that pool. The better it works (or the more aggressively it is
# wrong), the less evidence it generates about itself. Its forward sample
# size can therefore never reach `_FORWARD_MIN_SAMPLE_SIZE`, and before this
# fix it could never be demoted by ANY mechanism: the mirror image of the
# TIGHTEN_SL track's own documented absorbing state, but on the half of the
# family that acts on every single real entry decision.
#
# THE RULE: a promoted, not-yet-demoted `ga-llm:*` heuristic with EXACTLY
# ZERO forward samples for `_FORWARD_MAX_SILENT_DAYS` days since its own
# `promoted_at` is demoted on a "no evidence of continued value" basis. It
# applies alongside - never instead of - the sample-size/adverse-deviation
# rule above: a heuristic can be retired by either path.
#
# ZERO, not "below the floor", is a deliberate boundary. A heuristic with any
# forward evidence at all, however little, is a MEASURABLE rule that simply
# has not accumulated enough record yet, and is exactly what the existing
# count-based canary governs; letting the time rule reach into that range
# would demote on the evidence of one or two trades, which is the failure
# mode the `intervention_applied` filter and the n>=15 floor were both added
# to prevent. The two rules therefore partition the space (n == 0 vs. n > 0)
# instead of overlapping in an untested way.
#
# WHY 14 DAYS: deliberately conservative in the direction this codebase
# already frames every other threshold here (`_MIN_SAMPLE_SIZE`,
# `_MAX_LIVE_TIGHTEN_SL_HEURISTICS`) - "demoting too eagerly costs a missed
# opportunity that promotion can re-earn, while demoting too late costs real
# capital".
# - Both tracks generate evidence FAST when there is any to generate.
#   Positions close within `max_position_hold_hours` (6h by default), so a
#   veto condition describing a recurring entry pattern should see matching
#   closed positions within days; a TIGHTEN_SL decision is re-evaluated every
#   tick (60s). Two full weeks of literally nothing is not "early days", it
#   is a rule nothing can be said about.
# - It is not shorter, because a promoted rule deserves a real chance: a
#   quiet market week, a narrow condition, or a paused bot must not retire
#   the whole family on the first Monday.
# - THE ACCEPTED COST, stated rather than glossed: a trading pause longer
#   than the bar retires every live `ga-llm:*` heuristic. That is the safe
#   direction (a demoted heuristic contributes exactly 0.0 and a fresh
#   candidate can re-earn the same pattern), and it is the only available
#   direction - from inside this pipeline, "successfully preventing losses"
#   and "silently acting on a pattern that no longer exists" are the SAME
#   observation: no evidence.
# ---------------------------------------------------------------------------
_FORWARD_MAX_SILENT_DAYS = 14


def _days_since_promotion(promoted_at: str, now: datetime) -> float | None:
    """Days elapsed between the ISO-8601 `promoted_at` string this table
    stores and `now`, or None when that cannot be measured (an unparseable
    timestamp). None means "no measurable silence", and the caller then does
    NOT demote - demoting on an unmeasurable record is the one thing this
    step must not do, the same stance as its existing `not promoted_at` skip.

    A naive/aware mismatch is normalized to UTC rather than raised on:
    production always writes tz-aware timestamps, but a caller passing a
    naive `now` must not be able to turn a housekeeping step into an
    exception inside the trading tick."""
    try:
        promoted = datetime.fromisoformat(promoted_at)
    except (TypeError, ValueError):
        return None
    if (promoted.tzinfo is None) != (now.tzinfo is None):
        if promoted.tzinfo is None:
            promoted = promoted.replace(tzinfo=now.tzinfo)
        else:
            now = now.replace(tzinfo=promoted.tzinfo)
    return (now - promoted).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# I4 (final whole-branch review, 2026-09-17): orphan reconciliation.
#
# THE WINDOW: `promote_validated_heuristic_candidates` writes the real
# heuristic row (`upsert_guardian_authority_heuristic`) BEFORE it marks the
# candidate PROMOTED (`promote_guardian_authority_heuristic_candidate`), and
# the two writes are separately committed (no shared transaction - same as
# every other write pair against these tables). That order is forced, not
# chosen: the candidate row has to record the heuristic id the write
# produced, which is the opposite of demotion's own binding mark-then-zero
# order. So a crash - or a failure of the second write - between them leaves
# a LIVE `ga-llm:*` row in the real `guardian_authority_heuristics` table
# whose candidate is still `VALIDATED`.
#
# WHY THAT IS NOT SELF-CORRECTING: `_live_promoted_llm_candidates` only ever
# sees PROMOTED candidates, so such a row is invisible to BOTH halves of this
# pipeline - it is excluded from Cap B's divisor accounting (every other
# member is then scaled as if it did not exist, while it contributes its full
# adjustment to every matching decision) and from this function's own
# demotion sweep (which iterates candidates, not heuristics). It would act on
# real capital indefinitely, unowned and unmeasurable.
#
# THE FIX, and why ZEROING rather than logging alone: a logged-but-live
# orphan is still acting. Zeroing removes its voice - the row itself stays on
# file, verbatim, exactly as a demotion leaves one - and the state is
# genuinely recoverable rather than destructive: if the interrupted promotion
# did land its candidate transition after all, the row is owned again and the
# next promotion pass rescales it back to its earned value (a live,
# non-demoted `ga-llm:*` row is rewritten on every pass). So the conservative
# action costs at most one pass of silence for a rule that was never
# accounted for anyway.
#
# Two deliberate narrowings:
# - Only `ga-llm:*` ids are considered. `ga-hc:state:*` rows belong to
#   authority.py's own self-critique pass and have no candidate row BY
#   DESIGN; they are not orphans, and this module must never write to them.
# - An orphan already at 0.0/0.0 is left completely alone: nothing is acting
#   on capital any more, and rewriting (and re-logging) it on every tick
#   forever would be pure noise.
# ---------------------------------------------------------------------------

# This reconciliation has no run_id of its own to log under - it is triggered
# by the STATE of a table, not by a run. A fixed, greppable id is used
# instead of borrowing an unrelated one.
_ORPHAN_RECONCILIATION_RUN_ID = "ga-llm-orphan-reconciliation"


def _zero_orphan_llm_heuristic(repo: Repository, row: dict, now: datetime) -> None:
    """Silences one orphaned `ga-llm:*` heuristic through the same existing,
    unmodified `upsert_guardian_authority_heuristic` every other write in
    this pipeline uses. The row is preserved verbatim (same description, same
    condition, same sample size) - only its adjustment and confidence are
    zeroed, exactly as a demotion does."""
    repo.upsert_guardian_authority_heuristic(
        heuristic_id=row["heuristic_id"],
        description=row["description"],
        condition_json=row["condition_json"],
        adjustment=0.0,
        confidence=0.0,
        sample_size=int(row["sample_size"] or 0),
        updated_at=now,
    )


def _reconcile_orphan_llm_heuristics(repo: Repository, now: datetime) -> int:
    """Zeroes every live `ga-llm:*` heuristic row with no PROMOTED candidate
    referencing it, and returns how many were silenced. See the I4 section
    above for what produces such a row and why zeroing is the right response.

    Deliberately reads ALL promoted candidates - including demoted ones,
    which keep `status='PROMOTED'` and their `promoted_heuristic_id` as an
    audit trail - so a demoted row counts as OWNED, not orphaned."""
    owned = {
        str(row["promoted_heuristic_id"] or "")
        for row in repo.find_promoted_guardian_authority_heuristic_candidates()
    }

    zeroed = 0
    for row in repo.find_guardian_authority_heuristics():
        heuristic_id = str(row["heuristic_id"])
        if not heuristic_id.startswith(_LLM_HEURISTIC_ID_PREFIX):
            continue
        if heuristic_id in owned:
            continue
        if float(row["adjustment"]) == 0.0 and float(row["confidence"]) == 0.0:
            continue
        _zero_orphan_llm_heuristic(repo, row, now)
        log_event(
            _ORPHAN_RECONCILIATION_RUN_ID,
            event="ga_llm_orphan_heuristic_zeroed",
            heuristic_id=heuristic_id,
            previous_adjustment=float(row["adjustment"]),
            previous_confidence=float(row["confidence"]),
            reason=(
                "live ga-llm heuristic with no PROMOTED candidate - a promotion "
                "that wrote the heuristic row but never marked its candidate"
            ),
        )
        zeroed += 1
    return zeroed


def _outcome_stats(outcomes: list[bool]) -> tuple[int, float]:
    """`(sample_size, correct_rate)` - the same plain count/ratio tally
    `_split_stats` computes for a condition-filtered pool, for a track whose
    rows are already attributed and therefore need no condition matching.
    `correct_rate` is `0.0` (a placeholder, never read as meaningful) when
    the sample is empty: an n=0 track always fails the sample-size bar before
    any caller looks at its rate."""
    n = len(outcomes)
    if n == 0:
        return 0, 0.0
    return n, sum(1 for correct in outcomes if correct) / n


def _forward_tighten_sl_stats(
    resolved_decisions: list[dict], heuristic_id: str, promoted_at: str
) -> tuple[int, float]:
    """This heuristic's OWN forward record among real, resolved, genuinely
    applied TIGHTEN_SL interventions decided after `promoted_at`. See the
    Task 6 module section above for each of the four filters and where it
    comes from."""
    outcomes: list[bool] = []
    for decision in resolved_decisions:
        if decision["decision_type"] != _TARGET_TIGHTEN_SL:
            continue
        if decision["expectation_correct"] is None:
            continue
        if not bool(decision.get("intervention_applied")):
            continue  # R1 - the repeated-tick filter, see module section
        decided_at = decision["decided_at"]
        # Both timestamps are ISO-8601 strings written by `.isoformat()`, so
        # lexicographic order IS chronological order - the same property
        # `_most_recent_rows`/`_split_pool_chronologically` already rely on.
        # Strictly greater: a decision made in the same instant as the
        # promotion was not made BY the promoted heuristic.
        if not decided_at or decided_at <= promoted_at:
            continue
        matched_ids = _safe_json_list(decision["matched_heuristic_ids_json"]) or []
        if heuristic_id not in matched_ids:
            continue
        outcomes.append(bool(decision["expectation_correct"]))
    return _outcome_stats(outcomes)


def _forward_pre_entry_veto_stats(
    pool: list[tuple[str, dict, bool]], condition: dict, promoted_at: str
) -> tuple[int, float]:
    """This heuristic's OWN forward record among real positions closed after
    `promoted_at` whose entry evidence its own condition genuinely matches -
    Task 4B's pool and Task 4's `_split_stats`, both unmodified, asked
    forward instead of retrospectively."""
    forward = [row for row in pool if row[0] > promoted_at]
    return _split_stats(forward, condition)


def track_and_demote_underperforming_heuristics(repo: Repository, now: datetime) -> int:
    """Forward-performance tracking and auto-demotion (Task 6). For every
    promoted, not-yet-demoted `ga-llm:*` heuristic, computes its own forward
    record on the track its `target_decision_type` names, and demotes it -
    `mark_guardian_authority_heuristic_candidate_demoted` FIRST, then the
    zeroing `upsert_guardian_authority_heuristic` (the order is binding; see
    the Task 6 module section above) - on EITHER of two independent paths:

    - the count-based canary: the forward record reaches
      `_FORWARD_MIN_SAMPLE_SIZE` samples AND has deviated at least
      `_FORWARD_ADVERSE_DEVIATION` from the 0.5 baseline in the direction
      OPPOSITE to the heuristic's own stored adjustment (see the module
      section for why the bar is sign-aware and not a flat correct_rate
      floor);
    - the time-based silence bar (I2): the forward record is still EMPTY
      `_FORWARD_MAX_SILENT_DAYS` days after `promoted_at` - the only way a
      successfully-vetoing PRE_ENTRY_VETO heuristic, which destroys its own
      forward evidence by preventing the very positions that would produce
      it, can ever be retired. See that constant's own section above.

    Also reconciles ORPHANED `ga-llm:*` heuristic rows before doing any of
    that (I4 - see `_reconcile_orphan_llm_heuristics`): a live row in the real
    table with no PROMOTED candidate referencing it, which a crash between
    promotion's own two writes can leave behind, is zeroed and logged. Such a
    row is invisible to every other part of this pipeline, so this is the
    only place it can be caught.

    Returns the number of heuristics actually demoted by THIS call (orphan
    zeroings are NOT counted - nothing was demoted). A row some
    concurrent/earlier call already demoted is a structural no-op via
    `mark_..._demoted`'s own `demoted_at IS NULL` guard and is not counted -
    same idempotency discipline as the three steps above."""
    # I4: run BEFORE the live-candidate loop (and before its early return) -
    # an orphan's defining feature is that no candidate row points at it, so
    # a sweep that iterates candidates can never find it, and a repository
    # with zero promoted candidates is exactly the state a first, crashed
    # promotion leaves behind. Not counted in this function's return value:
    # nothing was demoted, an unowned row was silenced (it has its own
    # `ga_llm_orphan_heuristic_zeroed` log event).
    _reconcile_orphan_llm_heuristics(repo, now)

    # The SAME "live" the promotion pass's own rescale uses - one definition
    # (`promoted`, `ga-llm:*`, `demoted_at IS NULL`), read through the same
    # helper, so the half of the plan that writes adjustments and the half
    # that zeroes them can never disagree about which rows are in play.
    live = _live_promoted_llm_candidates(repo)
    if not live:
        return 0

    # Read once per call, not once per heuristic: all three are whole-table
    # aggregate reads, and every heuristic on a given track is measured
    # against the same underlying evidence (only the window and the
    # attribution/condition filter differ per heuristic). The veto pool is
    # built lazily because a family with no PRE_ENTRY_VETO members should not
    # pay for a full `find_closed_positions()` scan.
    #
    # `find_guardian_authority_heuristics` (existing, unmodified - read only,
    # never written by this module) is what supplies each rule's own DIRECTION:
    # the sign of the adjustment `evaluate_heuristics` actually sums.
    live_adjustments = {
        row["heuristic_id"]: float(row["adjustment"])
        for row in repo.find_guardian_authority_heuristics()
    }
    resolved_decisions: list[dict] | None = None
    veto_pool: list[tuple[str, dict, bool]] | None = None
    # Lazily built exactly like veto_pool above, for the same reason: a
    # family with no TAKE_PROFIT members should not pay for a full
    # find_closed_positions()+observations scan.
    take_profit_pool: list[tuple[str, dict, bool]] | None = None

    demoted = 0
    for candidate in sorted(live, key=lambda row: row["candidate_id"]):
        promoted_at = candidate["promoted_at"]
        heuristic_id = candidate["promoted_heuristic_id"]
        if not promoted_at or not heuristic_id:
            # Defensive - `promote_guardian_authority_heuristic_candidate`
            # writes both in the same UPDATE that sets status='PROMOTED', so
            # no production path produces this. A row without a promotion
            # timestamp has no forward window to measure, and demoting on an
            # unmeasurable record is the one thing this step must not do.
            continue
        if heuristic_id not in live_adjustments:
            # Defensive - promotion writes the heuristic row before it marks
            # the candidate PROMOTED, so a PROMOTED candidate always has one.
            # Without it there is no direction to judge the forward record
            # against, and nothing live to silence either.
            continue

        target = _target_decision_type(candidate)
        if target == _TARGET_PRE_ENTRY_VETO:
            if veto_pool is None:
                veto_pool = _pre_entry_veto_evidence_pool(repo)
            # The candidate's own condition, read through the single-row
            # accessor the brief names. `condition_json` is write-once on this
            # table (only `save_guardian_authority_heuristic_candidate` ever
            # sets it), so this is necessarily the same string the batch row
            # carries - the separate read costs one indexed primary-key lookup
            # per PRE_ENTRY_VETO heuristic and makes the condition's
            # provenance explicit at the point it decides a demotion. Parsed,
            # never re-serialized, so what is matched forward is byte-for-byte
            # what validation matched.
            promoted_row = repo.get_guardian_authority_heuristic_candidate(
                candidate["candidate_id"]
            )
            sample_size, correct_rate = _forward_pre_entry_veto_stats(
                veto_pool, json.loads(promoted_row["condition_json"]), promoted_at
            )
        elif target == _TARGET_TAKE_PROFIT:
            # Same closed-position-counterfactual mechanism as PRE_ENTRY_
            # VETO immediately above (reused via the same generic `_forward_
            # pre_entry_veto_stats` - its own body has no PRE_ENTRY_VETO-
            # specific logic, it is generic over any `list[tuple[str, dict,
            # bool]]` pool), NOT the TIGHTEN_SL firing-attribution track:
            # TAKE_PROFIT's forward evidence is `_take_profit_evidence_pool`,
            # built from every real guardian_observations row on a closed
            # position, which accumulates whether or not this heuristic ever
            # actually fired. This is also why TAKE_PROFIT needs no
            # TIGHTEN_SL-style cardinality cap at promotion time (see
            # `_MAX_LIVE_TIGHTEN_SL_HEURISTICS`'s own section): only
            # TIGHTEN_SL's forward-tracking is firing-attribution-based and
            # can reach the absorbing state that cap exists to prevent.
            if take_profit_pool is None:
                take_profit_pool = _take_profit_evidence_pool(repo)
            promoted_row = repo.get_guardian_authority_heuristic_candidate(
                candidate["candidate_id"]
            )
            sample_size, correct_rate = _forward_pre_entry_veto_stats(
                take_profit_pool, json.loads(promoted_row["condition_json"]), promoted_at
            )
        else:
            if resolved_decisions is None:
                resolved_decisions = repo.find_resolved_guardian_authority_decisions()
            sample_size, correct_rate = _forward_tighten_sl_stats(
                resolved_decisions, heuristic_id, promoted_at
            )

        # The forward evidence expressed in the heuristic's OWN direction:
        # positive means it agrees with what the rule predicted, negative
        # means the record has swung the other way. See the module section
        # above for why a flat correct_rate floor is wrong here.
        promoted_adjustment = live_adjustments[heuristic_id]
        promoted_sign = 1 if promoted_adjustment > 0 else -1
        agreement = promoted_sign * (correct_rate - 0.5)

        # Two independent demotion paths, partitioned by sample size so they
        # can never overlap: n == 0 is the time-based "no evidence of
        # continued value" rule (I2 - see its own section above), n > 0 is the
        # original count-based canary. `reason` staying None means neither
        # fired and the heuristic is left exactly as it was.
        reason: str | None = None
        if sample_size == 0:
            silent_days = _days_since_promotion(promoted_at, now)
            if silent_days is not None and silent_days >= _FORWARD_MAX_SILENT_DAYS:
                reason = (
                    f"no forward evidence: zero forward {target} samples in "
                    f"{silent_days:.1f} days since promoted_at={promoted_at} "
                    f"(bar: {_FORWARD_MAX_SILENT_DAYS} days)"
                )
        elif sample_size >= _FORWARD_MIN_SAMPLE_SIZE and agreement <= -_FORWARD_ADVERSE_DEVIATION:
            reason = (
                f"forward correct_rate {correct_rate:.4f} deviates "
                f"{agreement:+.4f} in the direction of this heuristic's own "
                f"adjustment {promoted_adjustment:+.4f} (bar: "
                f"{-_FORWARD_ADVERSE_DEVIATION}) over n={sample_size} forward "
                f"{target} samples since promoted_at={promoted_at}"
            )
        if reason is None:
            continue

        # ORDER IS BINDING - mark first, zero second. See the module section
        # above for the interleaving this closes.
        if not repo.mark_guardian_authority_heuristic_candidate_demoted(
            candidate["candidate_id"], now, reason
        ):
            continue  # already demoted by a concurrent/earlier pass
        repo.upsert_guardian_authority_heuristic(
            heuristic_id=heuristic_id,
            # The row is kept on file verbatim - same description, same
            # condition - so the audit trail shows what was retired, not a
            # blank placeholder. Only its voice is removed.
            description=candidate["description"],
            condition_json=candidate["condition_json"],
            adjustment=0.0,
            confidence=0.0,
            sample_size=sample_size,
            updated_at=now,
        )

        log_event(
            candidate["run_id"],
            event="ga_llm_heuristic_demoted",
            candidate_id=candidate["candidate_id"],
            heuristic_id=heuristic_id,
            target_decision_type=target,
            forward_sample_size=sample_size,
            forward_correct_rate=correct_rate,
            promoted_adjustment=promoted_adjustment,
            forward_agreement=agreement,
            demotion_reason=reason,
        )
        demoted += 1

    return demoted


# ---------------------------------------------------------------------------
# Task 7 (2026-09-15, Guardian Authority Live Autonomy): the periodic-tick
# orchestrator. Wires the four steps above into a single call - propose ->
# validate -> promote -> track/demote, in that order - mirroring the
# multi-step try/except-per-step orchestration pattern this codebase already
# established for Shadow Mode's own `run_guardian_authority_shadow_tick`
# (crypto_trading/paper_trading/guardian_authority_shadow.py) and the real
# Task 9 self-critique tick wiring (guardian/tick.py): each step gets its
# own try/except and its own `log_event` name, so one step's failure can
# never block, mask, or be confused with another's.
#
# TWO authority_enabled gates, deliberately (2026-09-17 final-review fix
# wave - this function originally had none of its own). Gating the WHOLE
# pipeline at the call site in the chosen loop (see discovery_loop.py) is
# still the primary mechanism, exactly the same division of responsibility
# `run_guardian_authority_shadow_tick`'s own caller in monitoring_loop.py
# uses for `authority_shadow_enabled`, and it STAYS - the isolation suite
# proves every real call site is gated. The internal early-return below is
# defense in depth for this function specifically, because of what it is: a
# WRITE path into `guardian_authority_heuristics`, the one table the live
# decision core reads on every single real decision. Anything that reaches
# this function by another route - a future loop, a maintenance script, a
# REPL - would otherwise run the entire propose/validate/promote/demote
# pipeline with the feature switched off. The flag is read defensively and
# fails CLOSED: unreadable means off, and reading it can never raise into
# the hosting trading tick.
#
# Same-tick propose -> validate behavior (deliberate choice, not an
# accident): validate_pending_heuristic_candidates is called unconditionally
# right after propose_candidate_heuristics, against the SAME live
# repository, with no re-query gate suppressing rows written this same
# tick. propose_candidate_heuristics commits its candidate row to SQLite
# before returning (Task 3's own watermark-claimed-up-front discipline), so
# validate's own `find_proposed_guardian_authority_heuristic_candidates()`
# query - run moments later, same call - already sees it. A candidate
# proposed this tick can therefore be validated (and, if it clears the
# bar, promoted) in this SAME call. This was chosen over adding an
# artificial "skip anything proposed this run_id" gate because: (1) neither
# Task 3 nor Task 4 needs such a gate to behave correctly - validate reads
# whatever is PROPOSED regardless of when it was proposed, exactly like a
# second, independent call would; (2) propose only ever runs at most once
# per UTC day (its own watermark), so a same-tick pickup is a rare, harmless
# latency win, never a flood; (3) inventing a suppression gate would be new,
# untested surface area this task does not need. See
# test_self_improvement_tick.py::
# test_a_candidate_proposed_this_tick_is_validated_in_the_same_call for the
# proof.
def run_godfather_self_improvement_tick(
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    now: datetime,
) -> None:
    """Top-level periodic-tick orchestrator for the whole self-improvement
    pipeline. Calls, in order, `propose_candidate_heuristics` (Task 3),
    `validate_pending_heuristic_candidates` (Task 4), `promote_validated_
    heuristic_candidates` (Task 5), and `track_and_demote_underperforming_
    heuristics` (Task 6) - each in its own try/except with its own
    `log_event` name, so a crash in any one step never prevents the
    remaining steps from running this same tick. Never raises.

    Returns immediately, having done nothing at all, when
    `settings.guardian.authority_enabled` is false - see the section above
    for why this internal gate exists alongside (never instead of) the one at
    the call site."""
    # Defense in depth (2026-09-17 final-review fix wave, Task 7 gap). Read
    # defensively and fail CLOSED: this function is a write path into the
    # table the live decision core reads on every real decision, and the one
    # thing a gate on that path must never do is raise inside the trading
    # tick that hosts it - or default to ON when it cannot tell.
    try:
        authority_enabled = bool(settings.guardian.authority_enabled)
    except Exception:
        authority_enabled = False
    if not authority_enabled:
        return

    try:
        propose_candidate_heuristics(repo, runner, settings, run_id, now)
    except Exception as exc:
        log_event(
            run_id, event="godfather_self_improvement_propose_failed",
            error_type=type(exc).__name__, error=str(exc),
        )

    try:
        validate_pending_heuristic_candidates(repo, now)
    except Exception as exc:
        log_event(
            run_id, event="godfather_self_improvement_validate_failed",
            error_type=type(exc).__name__, error=str(exc),
        )

    try:
        promote_validated_heuristic_candidates(repo, now)
    except Exception as exc:
        log_event(
            run_id, event="godfather_self_improvement_promote_failed",
            error_type=type(exc).__name__, error=str(exc),
        )

    try:
        track_and_demote_underperforming_heuristics(repo, now)
    except Exception as exc:
        log_event(
            run_id, event="godfather_self_improvement_track_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
