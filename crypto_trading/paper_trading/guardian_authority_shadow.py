"""Guardian Authority Shadow/Observation Mode - tick-time half
(docs/superpowers/specs/2026-09-15-guardian-authority-shadow-design.md).

Closes the cold-start deadlock discovered post-merge in Guardian Authority
("GODFATHER"): with `active_heuristics_count == 0` (today's real production
state), `authority_enabled: true` is a structural no-op - every decision
defaults to APPROVE/NO_ACTION, and NO_ACTION is never logged
(guardian/tick.py: `if decision != "NO_ACTION":`), so no data ever
accumulates for Task 9's self-critique to learn from, regardless of the
flag. This module logs GODFATHER's HYPOTHETICAL open-position decisions for
every PAPER position - including hypothetical NO_ACTION - into its own
table (`guardian_authority_shadow_observations`), completely independent of
`authority_enabled` and WITHOUT ever reading from or writing to real
position/order state.

Modeled directly on `profit_protection_experiment.py` (`seed_shadows_for_
position` / `advance_shadow` / `run_profit_protection_experiment_tick`) -
the exact same proven shape: seed -> advance -> resolve, one row per real
PAPER position, per-shadow try/except isolation so one shadow's crash never
aborts the batch, same orphan-abandonment discipline for a position that
vanished from `open_positions`.

Hard safety rule (grep-provable, not just a behavioral promise): this
module NEVER imports or calls `open_position_for_candidate`,
`tighten_position_stop_loss`, `apply_live_sl_tightening`,
`place_stop_loss_order`, `cancel_order`, `set_leverage`, or
`repo.save_guardian_observation(..., state="EXIT")`. Its only writes
anywhere are the `guardian_authority_shadow_observations` repository
methods below. `decide_open_position` (guardian/authority.py) is reused
completely unmodified - same import, same call signature, same argument
order the real (non-shadow) call site in guardian/tick.py::
process_one_position uses.

Factors: this module has no market-data connector of its own (unlike
guardian/tick.py's real call site, which fetches fresh evidence/BTC RSI to
compute decay factors). Recomputing that formula here would be a second,
divergent copy of it - instead this module reads the real Guardian's own
already-computed, already-persisted factors for the position
(`guardian_observations`, written every tick by guardian/tick.py::
process_one_position regardless of `authority_enabled` - that block is
unconditional in the real function) via
`repo.find_latest_guardian_observation`. This is the same "read-only
duplicate of an existing read" pattern `profit_protection_experiment.py`'s
own `_guardian_state_for` already established for Guardian state (though
that one additionally applies a staleness guard relevant only to its own
real EXIT action - this module takes no real action, so no such guard is
needed or applied here; the real observation's own freshness is whatever
cadence guardian_loop.py runs at).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.guardian.authority import decide_open_position
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")


def seed_shadow_for_position(
    repo: Repository, position: Position, now: datetime, run_id: str
) -> None:
    """Unconditional (no activation-watermark gate, unlike Profit
    Protection's own seed function - this module never acts on anything,
    so there is no historical-orphan-auto-open risk to guard against).
    Idempotent via `seed_guardian_authority_shadow`'s own INSERT OR IGNORE
    on `shadow_id` (= `position_id`, 1:1 - GODFATHER has no parallel-
    threshold concept, unlike Profit Protection's shadow_id shape)."""
    repo.seed_guardian_authority_shadow(
        shadow_id=position.position_id,
        position_id=position.position_id,
        candidate_id=position.candidate_id,
        instrument=position.instrument,
        opened_at=position.opened_at,
        created_at=now,
        run_id=run_id,
    )


def _position_factors(repo: Repository, position_id: str) -> dict:
    """See module docstring's "Factors" section. Returns `{}` (never
    raises, never guesses) when no real Guardian observation exists yet
    for this position - e.g. a shadow seeded this same tick, before
    guardian_loop.py has ever ticked for it."""
    observation = repo.find_latest_guardian_observation(position_id)
    if observation is None:
        return {}
    return json.loads(observation["factors"])


def advance_shadow(
    shadow: dict,
    position: Position,
    guardian_state: str,
    tighten_threshold: float,
    close_threshold: float,
    current_price: Decimal,
    candle_high: Decimal,
    candle_low: Decimal,
    now: datetime,
    repo: Repository,
) -> None:
    """One shadow row, one tick. `current_price` is accepted for interface
    parity with the price_lookup tuple every caller unpacks (candle_low,
    candle_high, current_price, funding_rate) but is not itself consumed by
    this function - unlike Profit Protection's own advance_shadow, this
    module never computes a theoretical exit price, so there is nothing
    for `current_price` to feed into here.

    1. `record_guardian_authority_shadow_tick` (mfe/mae + a fresh
       last_factors_json snapshot) - unconditional, exactly like Profit
       Protection's own tick recording, reusing the identical running-
       extremum formula: `mfe = max(mfe, candle_high - entry)`,
       `mae = min(mae, candle_low - entry)` (entry = position.theoretical_
       entry, matching PP's own seed-time choice of field for this calc).
    2. Only if `shadow["status"] == "OBSERVING"`: evaluate the REAL,
       unmodified `decide_open_position` against the real, current
       heuristics table (possibly empty), and - only if it returns
       something other than NO_ACTION - register the shadow's one-time,
       immutable hypothetical decision. Once DECIDED, later ticks keep
       updating mfe/mae/last_factors_json (step 1) but never re-enter this
       block (repo.decide_guardian_authority_shadow's own WHERE
       status = 'OBSERVING' guard would refuse it anyway - this early
       return is belt-and-suspenders, not the only enforcement).

    Never writes to `positions`, never calls any close/open/order
    function - this function's only writes are the two repo calls above.
    """
    shadow_id = shadow["shadow_id"]
    entry_price = position.theoretical_entry

    mfe = max(Decimal(shadow["mfe"]), candle_high - entry_price)
    mae = min(Decimal(shadow["mae"]), candle_low - entry_price)

    position_factors = _position_factors(repo, position.position_id)
    # Exactly the merge decide_open_position performs internally
    # (`factors = {**position_factors, "guardian_state": guardian_state}`,
    # guardian/authority.py) - reproduced here only so the SAME merged dict
    # can be persisted as this tick's factors_json/last_factors_json
    # snapshot (decide_open_position itself returns no such dict).
    factors = {**position_factors, "guardian_state": guardian_state}
    factors_json = json.dumps(factors)

    repo.record_guardian_authority_shadow_tick(shadow_id, mfe, mae, factors_json, now)

    if shadow["status"] != "OBSERVING":
        return

    heuristics = repo.find_guardian_authority_heuristics()
    decision, expected_outcome, expected_direction, confidence, proposed_sl = decide_open_position(
        position_factors, guardian_state, position.stop_loss, position.simulated_fill_entry,
        heuristics, tighten_threshold, close_threshold,
    )
    if decision == "NO_ACTION":
        return

    repo.decide_guardian_authority_shadow(
        shadow_id, decision, now, expected_outcome, expected_direction, confidence,
        factors_json, proposed_sl, now,
    )


def _resolve_on_close(repo: Repository, shadow: dict, position: Position, now: datetime) -> None:
    """Called for a position present in `closed_positions` this tick.
    `baseline_pnl` reuses `compute_pnl` exactly, same PnL-parity data
    sourcing precedent `profit_protection_experiment.py`'s own backfill
    step already established - never a second PnL formula.

    `shadow["status"] == "OBSERVING"` (hypothetical NO_ACTION for the
    position's entire life): registers the retroactive NO_ACTION decision
    at close time, using `last_factors_json` (written on every tick,
    including the very last one before close, by `record_guardian_
    authority_shadow_tick` above) as the snapshot - there is no per-tick
    factors_json for an OBSERVING row (that column is written only once,
    by `decide_guardian_authority_shadow`, which this row never reached).

    `shadow["status"] == "DECIDED"`: per the design spec's "Resolution"
    section (reusing Task 8's established semantics verbatim, not
    inventing new ones) - `TIGHTEN_SL` gets a real `expectation_correct`
    (identical sign-comparison rule `resolve_pending_decisions` already
    uses: `(expected_direction == "favorable") == (actual_pnl > 0)`) and a
    real `prediction_error` (I3's own Brier component, reused verbatim:
    `(confidence - (1.0 if expectation_correct else 0.0)) ** 2`).
    `CLOSE_EARLY` gets both `None` (SQL NULL) - no counterfactual-of-
    inaction mechanism exists; scoring it would silently mis-score a
    correct early close as wrong, same reasoning Task 8's own ruling
    already established for the real (non-shadow) CLOSE_EARLY case.
    """
    baseline_pnl = compute_pnl(position)

    if shadow["status"] == "OBSERVING":
        factors_json = (
            shadow["last_factors_json"] if shadow["last_factors_json"] is not None else "{}"
        )
        repo.resolve_guardian_authority_shadow_no_action(
            shadow["shadow_id"], factors_json, position.exit_reason, baseline_pnl,
            position.closed_at, now,
        )
        return

    # shadow["status"] == "DECIDED"
    expectation_correct: bool | None = None
    prediction_error: float | None = None
    if shadow["shadow_decision"] == "TIGHTEN_SL":
        predicted_favorable = shadow["expected_direction"] == "favorable"
        actual_favorable = baseline_pnl > _ZERO
        expectation_correct = predicted_favorable == actual_favorable
        confidence = float(shadow["confidence"])
        prediction_error = (confidence - (1.0 if expectation_correct else 0.0)) ** 2

    repo.resolve_guardian_authority_shadow_decided(
        shadow["shadow_id"], position.exit_reason, baseline_pnl, position.closed_at,
        expectation_correct, prediction_error, now,
    )


def run_guardian_authority_shadow_tick(
    repo: Repository,
    open_positions: list[Position],
    closed_positions: list[Position],
    price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]],
    now: datetime,
    settings: Settings,
    run_id: str,
) -> None:
    """Top-level orchestrator, mirroring `run_profit_protection_experiment_
    tick`'s exact structure: (1) seed every open position not yet seeded,
    (2) advance every open shadow whose position is still open and has a
    candle this tick, abandoning any whose position vanished from
    `open_positions` (same stranded-shadow handling as Profit Protection's
    own, same per-shadow try/except isolation, same log_event on failure),
    (3) resolve every shadow (OBSERVING or DECIDED) whose position appears
    in `closed_positions` this tick. Called from the same place
    `run_profit_protection_experiment_tick` is called (monitoring loop,
    after `close_triggered_positions`), wrapped in the caller's own
    try/except - this function itself only needs to guard against writing
    anything outside `guardian_authority_shadow_observations`.

    A shadow whose position is still open but has no real Guardian
    observation yet this run (guardian_loop.py has never ticked for it -
    e.g. a position seeded this very same tick) is skipped for THIS tick
    only, same category as a genuinely missing candle: there is no
    guardian_state to hypothetically decide against yet, so nothing is
    guessed - mfe/mae simply catch up once an observation exists, same as
    Profit Protection's own "transient missing candle, not abandoned"
    precedent.
    """
    if not settings.guardian.authority_shadow_enabled:
        return

    # 1) Seed every currently-open position not yet seeded (unconditional -
    # no activation-watermark gate, no price_lookup gate: this module never
    # acts on anything, so seeding early costs nothing and risks nothing).
    for position in open_positions:
        seed_shadow_for_position(repo, position, now, run_id)

    # 2) Advance every currently-open shadow (includes any just seeded
    # above, since find_open_guardian_authority_shadows() re-queries after
    # the seed loop's own commits).
    open_position_ids = {position.position_id for position in open_positions}
    position_by_id = {position.position_id: position for position in open_positions}
    tighten_threshold = settings.guardian.authority_tighten_threshold
    close_threshold = settings.guardian.authority_close_threshold
    for shadow in repo.find_open_guardian_authority_shadows():
        if shadow["position_id"] not in open_position_ids:
            # Same "unhooked catch-up path" stranded-shadow handling as
            # profit_protection_experiment.py's own orchestrator: a shadow
            # can never legitimately still be OBSERVING/DECIDED once its
            # real position has left find_open_positions().
            repo.abandon_guardian_authority_shadow(shadow["shadow_id"], now)
            continue
        if shadow["instrument"] not in price_lookup:
            continue  # position genuinely still open - just no candle this tick
        try:
            position = position_by_id[shadow["position_id"]]
            candle_low, candle_high, current_price, _funding_rate = price_lookup[
                shadow["instrument"]
            ]
            observation = repo.find_latest_guardian_observation(shadow["position_id"])
            if observation is None:
                continue  # no real Guardian observation yet - nothing to evaluate against this tick
            guardian_state = observation["state"]
            advance_shadow(
                shadow, position, guardian_state, tighten_threshold, close_threshold,
                current_price, candle_high, candle_low, now, repo,
            )
        except Exception as exc:
            # Same "isolate one item's failure, keep processing the batch"
            # pattern as profit_protection_experiment.py's own advance
            # loop - a single malformed/unexpected shadow row must never
            # abort every OTHER shadow's advance this tick, nor skip step
            # 3's resolve loop entirely.
            log_event(
                run_id,
                event="guardian_authority_shadow_advance_failed",
                shadow_id=shadow["shadow_id"],
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue

    # 3) Resolve whatever closed this same tick (read-only against
    # `positions` - matches Profit Protection's own backfill step).
    for position in closed_positions:
        if position.exit_reason is None or position.fees is None or position.funding is None:
            continue  # defensive - close_triggered_positions always sets these
        shadow = repo.get_guardian_authority_shadow(position.position_id)
        if shadow is None or shadow["status"] not in ("OBSERVING", "DECIDED"):
            continue
        _resolve_on_close(repo, shadow, position, now)
