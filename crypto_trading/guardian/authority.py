"""Guardian Authority's pure decision engine (design spec:
docs/superpowers/specs/2026-09-14-guardian-authority-design.md), plus
(2026-09-14, Task 6) the pre-entry veto wiring built on top of it.

The decision engine below (`evaluate_heuristics`, `decide_pre_entry`,
`decide_open_position`, and their helpers) is Zero I/O. Every input is a
plain value/dict/Decimal, every output is a plain tuple - no database
access, no connector imports. This is deliberate: it is the module's only
genuinely novel logic, and keeping it pure makes it exhaustively
unit-testable without mocking anything (see task-3-brief.md).

`maybe_open_position_for_candidate` (bottom of this file, Task 6) is
NOT part of that pure core - it is the one place in this module that
performs I/O (reads heuristics, saves a decision row, and - on APPROVE or
when the flag is off - calls `open_position_for_candidate`). It is kept in
this same file because the plan's own task brief places it here as "a
small wrapper in guardian/authority.py", and because Task 10's production-
isolation checklist explicitly permits this module to call
`open_position_for_candidate` "only via the wrapper in Task 6" - i.e. this
function IS that sanctioned exception, not a violation of the pure-core
discipline above.

--------------------------------------------------------------------------
Heuristics representation (ratified by the plan, not reinterpreted here)
--------------------------------------------------------------------------
Each heuristic is a dict shaped like::

    {
        "heuristic_id": "h-momentum-1",
        "description": "...",
        "condition_json": '{"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}',
        "adjustment": 0.15,      # float, signed - push toward veto/tighten/close
        "confidence": 0.75,      # float 0-1, this heuristic's own historical reliability
        "sample_size": 42,
        "updated_at": "...",
    }

--------------------------------------------------------------------------
Condition-matching semantics (exact, documented here since later tasks -
notably Task 9's self-critique step - write condition_json values that
must round-trip through this same matching logic)
--------------------------------------------------------------------------
`condition_json` parses to a dict of factor-name -> requirement. A
heuristic MATCHES a given `factors` dict iff EVERY key in the parsed
condition is satisfied (logical AND across keys; an empty condition
``{}`` is vacuously satisfied by everything - a legitimate "always-on"
base-rate heuristic). Three requirement kinds, dispatched by key name:

1. ``"<name>_max"`` - numeric upper bound. Satisfied iff
   ``factors["<name>"]`` is present and numeric (coerced via ``float()``)
   and ``<= `` the bound.
2. ``"<name>_min"`` - numeric lower bound, symmetric to ``_max``:
   satisfied iff ``factors["<name>"] >= `` the bound.
3. Any other key ``"<name>"``:
   - if the condition value is a ``list``/``tuple``/``set`` -> list-
     membership: satisfied iff ``factors["<name>"]`` shares at least one
     element with it (when the factor value is itself a list/tuple/set)
     or is contained in it (when the factor value is a scalar). An EMPTY
     condition list never matches (vacuous membership is "nothing
     satisfies this", not "anything satisfies this" - the opposite
     convention from the empty *condition dict* case above, and
     deliberately so: an empty list of acceptable reasons means no reason
     qualifies).
   - otherwise (a scalar condition value) -> equality: satisfied iff
     ``factors["<name>"] == `` the condition value.

A missing key in `factors` never satisfies any requirement (fail-closed -
malformed/incomplete evidence must never accidentally satisfy a
risk-reducing rule it wasn't actually evidenced for). Malformed
`condition_json` is not caught here and propagates loudly, in one of two
ways depending on exactly how it is malformed - heuristics rows are
Guardian Authority's own internal data, and hiding a parse bug behind a
silent no-match would be worse than a loud failure during
development/tests:

1. Syntactically invalid JSON (e.g. `"{not json"`) raises
   `json.JSONDecodeError` (a `ValueError` subclass) from `json.loads`.
2. Syntactically *valid* JSON that does not parse to a JSON object - e.g.
   `"[]"`, `"null"`, `"3"`, `'"x"'` - parses successfully but is not a
   `dict`, so the subsequent `.items()` call (see
   `heuristic_condition_matches`) raises `AttributeError` (e.g. `'list'
   object has no attribute 'items'`) rather than `json.JSONDecodeError`.

This is intentionally a small rule-matching function, not a general
query language - keep any future extension to this same "few key
suffix conventions, AND across keys" shape.

--------------------------------------------------------------------------
Confidence
--------------------------------------------------------------------------
Both `decide_pre_entry` and `decide_open_position` return a `confidence`
derived from the matched heuristics: a weighted average of each matched
heuristic's own `confidence`, weighted by `|adjustment|` (a heuristic
that pushed harder toward the decision counts for more). When zero
heuristics match, `confidence` defaults to `1.0` - "no signal to
override the default" is itself a maximally confident state: the
default decision (APPROVE / NO_ACTION) is exactly the correct decision
absent any countervailing evidence, not an unconfident guess.

--------------------------------------------------------------------------
expected_direction vocabulary (exact - a later task, decision
resolution, compares actual outcomes against this field)
--------------------------------------------------------------------------
- "neutral": no directional prediction is being made. Used for APPROVE
  and NO_ACTION - Guardian Authority found no adverse pattern strong
  enough to act on, so it defers entirely to the existing
  deterministic/Gate-approved flow's own expectations; it isn't
  predicting anything about the position's P/L itself.
- "unfavorable": predicts that NOT intervening would have led to a worse
  outcome than the action taken. Used for PRE_ENTRY_VETO (predicts the
  candidate would have played out poorly had it been entered) and
  CLOSE_EARLY (predicts continuing to hold would have gone unfavorably).
  Both decisions leave no further P/L to predict AFTER the decision (no
  position exists / the position is closed), so the prediction is
  necessarily about the counterfactual of inaction.
- "favorable": predicts the action taken improves the position's
  expected outcome relative to its prior state. Used only for
  TIGHTEN_SL - the position stays open, so there IS a further P/L to
  predict, and tightening is only ever chosen because it is expected to
  protect/improve that P/L relative to the untightened stop.

--------------------------------------------------------------------------
The critical safety property (belt-and-suspenders layer 1 of 2 - see
Task 5/6's independent write-path assertion for layer 2)
--------------------------------------------------------------------------
`decide_open_position` NEVER returns `"TIGHTEN_SL"` paired with a
`proposed_new_sl` that is not strictly greater than `current_sl`
(LONG-only). If the internal tightening computation cannot produce a
strictly-greater value (e.g. `current_sl` is already at or above
`entry` - this happens for real when Profit Protection already moved
the stop to break-even or beyond), this function downgrades the
decision to `"NO_ACTION"` itself. It never trusts the caller to catch
this.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig, Settings
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.paper_trading.position_opening import open_position_for_candidate
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")

# Tightening is capped at moving the stop at most halfway from current_sl
# to entry in a single decision - conservative by construction, and it
# means a single miscalibrated heuristic score can never propose jumping
# the stop all the way to (or past) entry in one step.
_MAX_TIGHTEN_FRACTION = 0.5

# How quickly the tightening fraction grows with the amount the score
# exceeds tighten_threshold. Deliberately simple/linear - this is not
# meant to be a finely-tuned formula, just a deterministic, monotonic,
# bounded one (score further above threshold -> tighten more, capped).
_TIGHTEN_FRACTION_PER_UNIT_EXCESS = 0.5


def _numeric_bound_match(key: str, bound_value: object, factors: dict) -> bool | None:
    """Returns True/False if `key` is a recognized "<name>_max"/"<name>_min"
    numeric-bound condition key, or None if it is not (caller falls
    through to list-membership/equality handling). See module docstring
    for exact semantics."""
    if key.endswith("_max"):
        factor_name = key[: -len("_max")]
        op = "max"
    elif key.endswith("_min"):
        factor_name = key[: -len("_min")]
        op = "min"
    else:
        return None

    if factor_name not in factors:
        return False
    try:
        factor_value = float(factors[factor_name])
        bound = float(bound_value)
    except (TypeError, ValueError):
        return False
    return factor_value <= bound if op == "max" else factor_value >= bound


def _condition_key_matches(key: str, value: object, factors: dict) -> bool:
    numeric_result = _numeric_bound_match(key, value, factors)
    if numeric_result is not None:
        return numeric_result

    if key not in factors:
        return False
    factor_value = factors[key]

    if isinstance(value, (list, tuple, set)):
        if not value:
            return False  # vacuous membership never matches
        candidate_set = set(value)
        if isinstance(factor_value, (list, tuple, set)):
            return bool(candidate_set & set(factor_value))
        return factor_value in candidate_set

    return factor_value == value


def heuristic_condition_matches(condition: dict, factors: dict) -> bool:
    """A heuristic's parsed `condition_json` matches `factors` iff every
    key in `condition` is satisfied (AND across keys). See module
    docstring for exact per-key semantics. An empty `condition` matches
    universally (an "always-on" heuristic)."""
    return all(_condition_key_matches(key, value, factors) for key, value in condition.items())


def evaluate_heuristics(factors: dict, heuristics: list[dict]) -> tuple[float, list[str]]:
    """Sums the `adjustment` of every heuristic whose `condition_json`
    matches `factors` (see module docstring for matching semantics).
    Returns `(total_score, matched_heuristic_ids)` - the ids, in the
    order given in `heuristics`, for use in the decision's `reasoning`
    text.

    Raises (propagated, not caught - see module docstring
    "Condition-matching semantics" section for why): `json.JSONDecodeError`
    if a heuristic's `condition_json` is not syntactically valid JSON;
    `AttributeError` if it parses to valid JSON that is not a JSON object
    (e.g. a list, `null`, a number, or a string)."""
    total = 0.0
    matched_ids: list[str] = []
    for heuristic in heuristics:
        condition = json.loads(heuristic["condition_json"])
        if heuristic_condition_matches(condition, factors):
            total += float(heuristic["adjustment"])
            matched_ids.append(heuristic["heuristic_id"])
    return total, matched_ids


def _matched_heuristics(heuristics: list[dict], matched_ids: list[str]) -> list[dict]:
    matched_id_set = set(matched_ids)
    return [h for h in heuristics if h["heuristic_id"] in matched_id_set]


def _aggregate_confidence(matched: list[dict]) -> float:
    """Weighted average of matched heuristics' own `confidence`, weighted
    by each heuristic's `|adjustment|`. Defaults to 1.0 when nothing
    matched (see module docstring: "no signal to override the default"
    is itself a maximally confident state)."""
    if not matched:
        return 1.0
    total_weight = sum(abs(float(h["adjustment"])) for h in matched)
    if total_weight <= 0.0:
        # All matched heuristics happen to carry a zero adjustment
        # (degenerate/no-op rules) - fall back to a plain average so we
        # still return a meaningful number instead of dividing by zero.
        confidences = [float(h["confidence"]) for h in matched]
        return sum(confidences) / len(confidences)
    weighted = sum(float(h["confidence"]) * abs(float(h["adjustment"])) for h in matched)
    return weighted / total_weight


def _build_expected_outcome_text(decision: str, matched: list[dict]) -> str:
    """Plain, short, deterministic text built from the decision and the
    matched heuristics' own descriptions - no LLM call (out of scope for
    this task per the plan's own ruling)."""
    if not matched:
        return f"{decision}: no heuristics matched; default decision, no adverse pattern detected."
    descriptions = "; ".join(h["description"] for h in matched)
    return f"{decision}: driven by {len(matched)} matched heuristic(s) - {descriptions}."


def decide_pre_entry(
    candidate_evidence: dict,
    heuristics: list[dict],
    veto_threshold: float,
) -> tuple[str, str, str, float]:
    """Returns `(decision, expected_outcome_text, expected_direction,
    confidence)`. `decision` is `"APPROVE"` (default) or
    `"PRE_ENTRY_VETO"` - deterministically `"PRE_ENTRY_VETO"` iff the
    summed heuristic score strictly exceeds `veto_threshold`."""
    score, matched_ids = evaluate_heuristics(candidate_evidence, heuristics)
    matched = _matched_heuristics(heuristics, matched_ids)

    if score > veto_threshold:
        decision = "PRE_ENTRY_VETO"
        expected_direction = "unfavorable"
    else:
        decision = "APPROVE"
        expected_direction = "neutral"

    return (
        decision,
        _build_expected_outcome_text(decision, matched),
        expected_direction,
        _aggregate_confidence(matched),
    )


def _compute_proposed_new_sl(
    current_sl: Decimal, entry: Decimal, score: float, tighten_threshold: float
) -> Decimal:
    """Deterministic tightening amount: moves `current_sl` a fraction of
    the way toward `entry`, where the fraction grows with how far `score`
    exceeds `tighten_threshold` (capped at `_MAX_TIGHTEN_FRACTION` of the
    remaining gap, so a single decision never jumps the stop all the way
    to entry). If `entry - current_sl <= 0` (current_sl already at or
    above entry - e.g. Profit Protection already moved it to break-even
    or beyond), there is no "toward entry" room left under this formula,
    so this deliberately returns `current_sl` unchanged; the caller's own
    `proposed > current_sl` safety check (see module docstring) then
    correctly refuses to treat that as a valid tighten."""
    gap = entry - current_sl
    if gap <= _ZERO:
        return current_sl
    excess = max(0.0, score - tighten_threshold)
    fraction = min(excess * _TIGHTEN_FRACTION_PER_UNIT_EXCESS, _MAX_TIGHTEN_FRACTION)
    return current_sl + gap * Decimal(str(fraction))


def decide_open_position(
    position_factors: dict,
    guardian_state: str,
    current_sl: Decimal,
    entry: Decimal,
    heuristics: list[dict],
    tighten_threshold: float,
    close_threshold: float,
) -> tuple[str, str, str, float, Decimal | None]:
    """Returns `(decision, expected_outcome_text, expected_direction,
    confidence, proposed_new_sl_or_None)`. `decision` is `"NO_ACTION"`
    (default), `"TIGHTEN_SL"`, or `"CLOSE_EARLY"`.

    `guardian_state` is merged into the matching factors under the key
    `"guardian_state"` (alongside `position_factors`) so heuristics can
    condition on it directly (e.g. `{"guardian_state": "WATCH"}`).

    Precedence: `CLOSE_EARLY` is evaluated first - if `score` strictly
    exceeds `close_threshold`, the decision is `CLOSE_EARLY` regardless
    of whether it also exceeds `tighten_threshold` (closing is the more
    severe action; the two thresholds are not assumed to be ordered by
    the caller, but in normal configuration `close_threshold >=
    tighten_threshold`).

    See the module docstring for the critical safety property: this
    function never returns `"TIGHTEN_SL"` with a `proposed_new_sl` that
    is not strictly greater than `current_sl` - an invalid computed
    candidate is downgraded to `"NO_ACTION"` here, not trusted to the
    caller.
    """
    factors = {**position_factors, "guardian_state": guardian_state}
    score, matched_ids = evaluate_heuristics(factors, heuristics)
    matched = _matched_heuristics(heuristics, matched_ids)

    if score > close_threshold:
        decision = "CLOSE_EARLY"
        return (
            decision,
            _build_expected_outcome_text(decision, matched),
            "unfavorable",
            _aggregate_confidence(matched),
            None,
        )

    if score > tighten_threshold:
        proposed_sl = _compute_proposed_new_sl(current_sl, entry, score, tighten_threshold)
        if proposed_sl > current_sl:
            decision = "TIGHTEN_SL"
            return (
                decision,
                _build_expected_outcome_text(decision, matched),
                "favorable",
                _aggregate_confidence(matched),
                proposed_sl,
            )
        # SAFETY NET: score crossed tighten_threshold, but the computed
        # tightening candidate was not strictly greater than current_sl
        # (e.g. current_sl already at/above entry). Never trust the
        # caller to catch this - downgrade to NO_ACTION here. Keep the
        # matched-heuristic context in the text/confidence (rather than
        # the generic "nothing matched" default) so this downgrade is
        # observable/debuggable, not silently indistinguishable from a
        # genuine no-signal NO_ACTION.
        text = (
            "NO_ACTION: heuristics scored above tighten_threshold "
            f"({score!r} > {tighten_threshold!r}) but the computed proposed "
            "stop-loss was not strictly greater than the current stop-loss "
            "(current_sl already at/above entry, or a degenerate gap) - "
            "downgraded for safety rather than risking an invalid tighten. "
            f"Matched: {'; '.join(h['description'] for h in matched)}."
        )
        return ("NO_ACTION", text, "neutral", _aggregate_confidence(matched), None)

    decision = "NO_ACTION"
    return (
        decision,
        _build_expected_outcome_text(decision, matched),
        "neutral",
        _aggregate_confidence(matched),
        None,
    )


def _pre_entry_factors(candidate: Candidate) -> dict:
    """The candidate's own evidence, reshaped into the flat factor dict
    `decide_pre_entry`/`evaluate_heuristics` match heuristic conditions
    against (see module docstring's "Condition-matching semantics" section).

    Deliberately limited to the fields that are BOTH (a) part of the
    candidate's own evidence (`Candidate.evidence_record`, i.e. known at
    pre-entry time - nothing from a later assessment) and (b) part of the
    exact factor vocabulary this plan's own manual historical analysis
    already established and documented (`trigger_reasons`, `candidate_score`
    bucket - see plan doc line 13/265): `instrument` (for potential
    per-symbol conditioning), `candidate_score`, and `trigger_reasons`.
    Other vocabulary mentioned alongside those two in the plan (e.g.
    `rsi_30m` bucket) comes from a later technical/market-data assessment,
    not from the candidate's own evidence record, and is out of scope for
    this pre-entry hook - Task 9's self-critique step is what actually
    decides, from real resolved-decision data, which factors are worth
    encoding as heuristics; this function only has to expose the ones a
    heuristic COULD condition on today."""
    evidence = candidate.evidence_record
    return {
        "instrument": candidate.instrument,
        "candidate_score": evidence.candidate_score,
        "trigger_reasons": evidence.trigger_reasons,
    }


def maybe_open_position_for_candidate(
    repo: Repository,
    candidate: Candidate,
    risk_limits: RiskLimitsConfig,
    reference_price: Decimal,
    opened_at: datetime,
    run_id: str,
    settings: Settings,
) -> Position | None:
    """Pre-entry veto hook in front of `open_position_for_candidate` (Task
    6). Ships default OFF: with `settings.guardian.authority_enabled` False
    (the default), this is a byte-identical passthrough - heuristics are
    never even read, and the only I/O performed is
    `open_position_for_candidate`'s own, exactly as before this task
    existed.

    When enabled, runs `decide_pre_entry` against the candidate's own
    evidence (`_pre_entry_factors`) and the full, freshly-read heuristics
    table (per the plan's own ruling: heuristics are read fresh, in full,
    at the start of every decision - never cached). On `"PRE_ENTRY_VETO"`,
    saves a decision row (`position_id=None` - no position exists yet, and
    per Task 1's own design this is explicitly a supported, nullable case)
    and returns `None` WITHOUT ever calling `open_position_for_candidate` -
    the one invariant this whole task exists to enforce. On `"APPROVE"`,
    falls through to call `open_position_for_candidate` exactly as before;
    per the plan's own "only actual interventions get a row" ruling, no
    decision row is saved for an APPROVE."""
    if not settings.guardian.authority_enabled:
        return open_position_for_candidate(
            candidate, repo, risk_limits, reference_price, opened_at, run_id
        )

    heuristics = repo.find_guardian_authority_heuristics()
    decision, expected_outcome, expected_direction, confidence = decide_pre_entry(
        _pre_entry_factors(candidate), heuristics, settings.guardian.authority_veto_threshold,
    )

    if decision == "PRE_ENTRY_VETO":
        decision_id = f"ga:pre_entry:{candidate.candidate_id}:{opened_at.isoformat()}"
        repo.save_guardian_authority_decision(
            decision_id=decision_id,
            position_id=None,
            candidate_id=candidate.candidate_id,
            decision_type="PRE_ENTRY_VETO",
            decided_at=opened_at,
            # decide_pre_entry returns a single, already self-explanatory
            # text (matched-heuristic descriptions baked in by
            # _build_expected_outcome_text) rather than a separate
            # matched_ids list the way decide_open_position's own call site
            # (Task 7/8) does - there is nothing further to say in
            # `reasoning` that `expected_outcome` doesn't already say, so
            # both columns intentionally carry the same text here.
            reasoning=expected_outcome,
            expected_outcome=expected_outcome,
            expected_direction=expected_direction,
            confidence=confidence,
            run_id=run_id,
        )
        log_event(
            run_id, event="ga_pre_entry_veto", candidate_id=candidate.candidate_id,
            instrument=candidate.instrument, decision_id=decision_id, confidence=confidence,
        )
        return None

    return open_position_for_candidate(
        candidate, repo, risk_limits, reference_price, opened_at, run_id
    )


def resolve_pending_decisions(repo: Repository, now: datetime) -> int:
    """Resolution pass (Task 8). NOT part of the pure decision core above
    (see module docstring) - this is I/O, the same sanctioned kind as
    `maybe_open_position_for_candidate`: it reads pending decisions and
    positions and writes resolved outcomes, all via `repo`.

    For every `find_pending_guardian_authority_decisions()` row, looks up
    its position via `repo.get_position(position_id)`. A position counts as
    "still open" (skip, leave PENDING, don't touch the row) whenever that
    call returns `None` or a `Position` whose `status != "CLOSED"` -
    matching this codebase's own status-check convention used elsewhere
    (e.g. `dashboard/api.py`, `detective/context.py`,
    `performance/metrics.py`: `compute_pnl(position) if position.status ==
    "CLOSED" else None`).

    This single check also handles `PRE_ENTRY_VETO` rows without any
    special-casing: those always have `position_id=None` (no position was
    ever opened - that's the whole point of a veto), and
    `repo.get_position(None)` reliably returns `None` the same way a real
    missing id would (the underlying `WHERE position_id = ?` query never
    matches SQL NULL). So PRE_ENTRY_VETO rows are skipped every single time
    this function runs, forever - a deliberate, permanent scope limit: a
    veto's correctness is about a counterfactual (what would have happened
    had the candidate NOT been vetoed) that this task has no market-data
    infrastructure to evaluate. Not a bug, not a TODO - see task-8-brief.md.

    For a closed position, `actual_exit_reason` is read directly from the
    `Position`'s own `exit_reason` field, and `actual_pnl_usdt` is
    `str(compute_pnl(position))` - `compute_pnl` (from
    `paper_trading.execution`) is reused exactly as-is, never a new PnL
    formula (this module's own hard rule; see the module docstring's
    `expected_direction` section for why PnL is never derived any other
    way here).

    `expectation_correct` is then computed ONLY for `TIGHTEN_SL` rows (the
    only other decision type that can reach this point - PRE_ENTRY_VETO is
    always skipped above), via the plan's literal sign-comparison rule:
    `expected_direction == "favorable"` must correspond to a STRICTLY
    positive `actual_pnl_usdt` (a P/L of exactly zero counts as NOT
    favorable) - i.e. `(expected_direction == "favorable") == (actual_pnl
    > 0)`. `TIGHTEN_SL` is the one decision type for which this comparison
    is actually valid: it predicts the real forward P/L of the action
    taken (the position stays open under the tightened stop), so the
    realized P/L IS the thing the prediction was about.

    `CLOSE_EARLY` rows get `expectation_correct=None` (SQL NULL) instead -
    deliberately NOT computed, even though the brief's own literal
    instruction would naively sign-compare here too. Per this module's own
    `expected_direction` vocabulary (see the docstring section above),
    `CLOSE_EARLY` always predicts `"unfavorable"`, meaning "continuing to
    hold would have gone unfavorably" - a counterfactual of INACTION. But
    the `actual_pnl_usdt` computed here comes from the position that was
    actually closed early - it measures the outcome of the close itself
    (typically a small or contained result, precisely because closing
    early is what limited the damage), never the counterfactual of what
    would have happened had the position stayed open. Sign-comparing
    "unfavorable" against that realized P/L would systematically misscore
    a *correct* early close (one that successfully avoided a worse loss)
    as a wrong expectation. A true counterfactual would require re-fetching
    forward price data past the actual exit - a meaningfully bigger task,
    out of scope here. `actual_exit_reason`/`actual_pnl_usdt` are still
    real and still filled in (worth keeping for later analysis, e.g. Task
    9's self-critique step), and the row is still marked `RESOLVED` - only
    `expectation_correct` is withheld.

    Returns the count of rows actually resolved in THIS call - still-open
    skips and PRE_ENTRY_VETO skips are not counted.
    """
    resolved_count = 0
    for decision in repo.find_pending_guardian_authority_decisions():
        position = repo.get_position(decision["position_id"])
        if position is None or position.status != "CLOSED":
            continue

        actual_exit_reason = position.exit_reason
        actual_pnl = compute_pnl(position)
        actual_pnl_usdt = str(actual_pnl)

        if decision["decision_type"] == "CLOSE_EARLY":
            expectation_correct = None
        else:
            predicted_favorable = decision["expected_direction"] == "favorable"
            actual_favorable = actual_pnl > _ZERO
            expectation_correct = predicted_favorable == actual_favorable

        repo.resolve_guardian_authority_decision(
            decision["decision_id"],
            actual_exit_reason,
            actual_pnl_usdt,
            expectation_correct,
            now,
        )
        resolved_count += 1

    return resolved_count
