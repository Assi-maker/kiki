"""Guardian Authority self-improvement, step 1 of 4: PROPOSE (design spec:
docs/superpowers/specs/2026-09-15-guardian-authority-live-autonomy-design.md,
"What 'self-improvement' concretely means here").

The full pipeline is propose -> validate -> promote -> track/demote. THIS
module only proposes. Everything it writes lands in one place -
`guardian_authority_heuristic_candidates`, status `PROPOSED` - a table the
real decision engine never reads. `evaluate_heuristics` reads
`guardian_authority_heuristics`, and nothing here can write there: the only
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
    compute_breakdown_by_signal_type,
    compute_guardian_exit_effectiveness,
)
from crypto_trading.guardian.authority import _reconstruct_tighten_sl_factors
from crypto_trading.guardian.tick import _budget_allows_one_more_call, _utc_day_start
from crypto_trading.logging import log_event
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

    return {
        "run_id": run_id,
        "resolved_shadow_decisions": shadows,
        "resolved_real_decisions": reals,
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
        "observed_factor_names": _observed_factor_names(shadows, reals),
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
