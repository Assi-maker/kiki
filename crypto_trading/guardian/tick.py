from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_live_trading import BingXLiveTradingConnector
from crypto_trading.guardian.ai_context import build_ai_context, should_invoke_ai
from crypto_trading.guardian.authority import (
    decide_open_position,
    decide_take_profit,
    evaluate_heuristics,
    resolve_pending_decisions,
    update_heuristics_from_resolved_decisions,
)
from crypto_trading.guardian.authority_live import (
    apply_live_sl_tightening,
    recover_claimed_live_sl_tightenings,
)
from crypto_trading.guardian.data import GuardianDataSource, fetch_btc_regime_rsi, fetch_current_price, fetch_fresh_evidence
from crypto_trading.guardian.deterministic import (
    classify_guardian_state, compute_decay_score, compute_funding_decay_factor,
    compute_market_regime_factor, compute_momentum_decay_factor, compute_progress_ratio,
    compute_secondary_confirmation_lost_factor, compute_time_decay_factor, compute_unrealized_pnl,
    compute_volume_decay_factor,
)
from crypto_trading.logging import log_event
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianAssessment, GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_GUARDIAN_AGENT_FILE = "crypto-guardian.md"
_WORST_CASE_COST_PER_CALL_USD = Decimal("0.20")  # same constant as orchestrator.py / detective/batch.py


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _budget_allows_one_more_call(repo: Repository, settings: Settings, now: datetime) -> bool:
    day_start = _utc_day_start(now)
    daily_count = repo.count_ai_calls_since(day_start)
    daily_cost = repo.sum_ai_cost_since(day_start)
    calls_would_exceed = daily_count + 1 > settings.budget_limits.max_ai_calls_per_day
    cost_would_exceed = daily_cost + _WORST_CASE_COST_PER_CALL_USD > settings.budget_limits.max_daily_ai_cost_usd
    return not (calls_would_exceed or cost_would_exceed)


def process_one_position(
    repo: Repository,
    connector: GuardianDataSource,
    runner: AgentRunner,
    settings: Settings,
    position: Position,
    run_id: str,
    now: datetime,
    live_connector: BingXLiveTradingConnector | None = None,
) -> GuardianObservation | None:
    candidate = repo.get_candidate(position.candidate_id)
    secondary_timeframe = (
        candidate.evidence_record.secondary_timeframe_evidence.timeframe
        if candidate is not None and candidate.evidence_record.secondary_timeframe_evidence is not None
        else None
    )
    fresh_evidence = fetch_fresh_evidence(connector, position.instrument, secondary_timeframe, settings, now)
    current_price = fetch_current_price(connector, position.instrument)
    btc_rsi = fetch_btc_regime_rsi(connector, settings, now)
    if fresh_evidence is None or current_price is None or btc_rsi is None or candidate is None:
        return None  # fail-safe skip - never a guessed observation

    entry_evidence = candidate.evidence_record
    factors = {
        "time_decay": compute_time_decay_factor(
            position.opened_at, now, settings.risk_limits.max_position_hold_hours
        ),
        "momentum_decay": compute_momentum_decay_factor(
            Decimal(str(entry_evidence.momentum_breakout_evidence.value)),
            Decimal(str(fresh_evidence.momentum_breakout_evidence.value)),
        ),
        "volume_decay": compute_volume_decay_factor(
            Decimal(str(entry_evidence.volume_evidence.value)),
            Decimal(str(fresh_evidence.volume_evidence.value)),
        ),
        "funding_decay": compute_funding_decay_factor(
            Decimal(str(entry_evidence.funding_oi_evidence.value)),
            Decimal(str(fresh_evidence.funding_oi_evidence.value)),
        ),
        "secondary_confirmation_lost": compute_secondary_confirmation_lost_factor(
            entry_evidence.secondary_timeframe_evidence, fresh_evidence.secondary_timeframe_evidence
        ),
        "market_regime": compute_market_regime_factor(btc_rsi),
    }
    decay_score = compute_decay_score(factors, settings.guardian.factor_weights)
    progress_ratio = compute_progress_ratio(position.simulated_fill_entry, position.target, current_price)
    unrealized_pnl = compute_unrealized_pnl(position, current_price)
    new_state = classify_guardian_state(decay_score, unrealized_pnl > 0, settings.guardian)

    # Guardian Authority tick-time decision (2026-09-14, Task 7). Ships
    # default OFF (settings.guardian.authority_enabled is False) - with the
    # flag off this whole block is not even entered, so behavior is
    # byte-identical to before this task existed: heuristics are never read,
    # decide_open_position is never called, and the function falls straight
    # through to the pre-existing AI-invocation/observation-save flow below
    # exactly as it always has.
    if settings.guardian.authority_enabled:
        heuristics = repo.find_guardian_authority_heuristics()
        guardian_cfg = settings.guardian
        decision, expected_outcome, expected_direction, confidence, proposed_sl = (
            decide_open_position(
                factors, new_state, position.stop_loss, position.simulated_fill_entry,
                heuristics, guardian_cfg.authority_tighten_threshold,
                guardian_cfg.authority_close_threshold,
            )
        )
        if decision != "NO_ACTION":
            # Task 2 (2026-09-15, Guardian Authority Live Autonomy): a
            # second, duplicate, side-effect-free evaluate_heuristics call
            # purely to recover the matched_ids decide_open_position already
            # computed internally (and discarded after building its own
            # expected_outcome text) - reconstructed identically to
            # decide_open_position's own internal merge (factors +
            # guardian_state), so the SAME heuristics match. Costs one extra
            # cheap pure-function call per real decision; does not touch
            # decide_open_position/evaluate_heuristics themselves.
            _, matched_ids = evaluate_heuristics({**factors, "guardian_state": new_state}, heuristics)
            decision_id = f"ga:{position.position_id}:{now.isoformat()}"
            # I2 hardening fix (2026-09-14): CLOSE_EARLY's write
            # (repo.save_guardian_observation with state="EXIT", below) is
            # known-successful right here, by construction, with no
            # partial/no-op path - so intervention_applied=True is saved
            # immediately. TIGHTEN_SL's write attempt hasn't happened yet at
            # this point in the function (it's the branch right below) -
            # its outcome is genuinely unknown until then, so it is saved as
            # None ("not yet determined") and updated afterward via
            # repo.mark_guardian_authority_decision_intervention_applied
            # once the write attempt's real outcome is known (both the
            # PAPER and LIVE sub-branches below).
            intervention_applied = True if decision == "CLOSE_EARLY" else None
            repo.save_guardian_authority_decision(
                decision_id, position.position_id, position.candidate_id, decision, now,
                # decide_open_position returns a single, already
                # self-explanatory text (matched-heuristic descriptions
                # baked in by its own _build_expected_outcome_text) rather
                # than a separate matched_ids list - same precedent Task 6's
                # maybe_open_position_for_candidate already established for
                # decide_pre_entry's identically-shaped return value: both
                # reasoning and expected_outcome intentionally carry the
                # same text here, there is nothing further to say.
                reasoning=expected_outcome, expected_outcome=expected_outcome,
                expected_direction=expected_direction, confidence=confidence, run_id=run_id,
                old_sl=str(position.stop_loss),
                new_sl=str(proposed_sl) if proposed_sl is not None else None,
                intervention_applied=intervention_applied,
                matched_heuristic_ids_json=json.dumps(matched_ids),
            )
            if decision == "TIGHTEN_SL":
                # Same LIVE/PAPER branch Profit Protection itself uses: an
                # ACTIVE live_executions row means this is a real LIVE
                # position, and the real order must be tightened on the
                # exchange (Task 5), never via the PAPER-only
                # tighten_position_stop_loss column update.
                live_row = repo.get_live_execution(position.position_id)
                if live_row is not None and live_row["phase"] == "ACTIVE":
                    if live_connector is None:
                        # Task 10 diagnostic fix (carried forward from Task
                        # 7's review, Minor/non-blocking): a real
                        # misconfiguration - LIVE execution disabled at
                        # startup after some positions were already opened
                        # LIVE - reaches this branch with live_connector=None.
                        # Before this fix, that fell straight through to
                        # apply_live_sl_tightening(repo, None, ...), which
                        # fails safe (no order placed, no DB write - Task 5's
                        # own design) but only via a generic AttributeError
                        # from the try/except below, logged every tick
                        # forever for that position with no indication of
                        # WHY. The fail-safe OUTCOME is unchanged by this
                        # fix (no order is ever placed either way) - this
                        # only replaces the confusing generic exception with
                        # a distinct, purpose-built diagnostic event, and
                        # skips the call entirely rather than attempting it.
                        log_event(
                            run_id, event="ga_tick_live_sl_tightening_skipped_no_connector",
                            position_id=position.position_id, instrument=position.instrument,
                            severity="ERROR",
                        )
                        # I2 hardening fix: no write was ever attempted.
                        repo.mark_guardian_authority_decision_intervention_applied(
                            decision_id, False, now,
                        )
                    else:
                        # apply_live_sl_tightening is a single-position API
                        # that deliberately lets exceptions propagate (Task
                        # 5's own docstring: "the caller owns batch
                        # isolation"). One malformed/failing LIVE tightening
                        # attempt must never abort the rest of this tick's
                        # batch - wrap it here, exactly like the existing
                        # AI-call try/except pattern already used elsewhere
                        # in this file, and log+continue.
                        try:
                            apply_live_sl_tightening(
                                repo, live_connector, position.position_id, position.instrument,
                                proposed_sl, run_id, now,
                            )
                        except Exception as exc:  # noqa: BLE001 - one bad LIVE tighten must never block the batch
                            log_event(
                                run_id, event="ga_tick_live_sl_tightening_error",
                                position_id=position.position_id, error_type=type(exc).__name__,
                                error=str(exc),
                            )
                            # I2 hardening fix: the attempt failed - not a
                            # confirmed success, regardless of whatever
                            # intermediate claim-row state may or may not
                            # exist.
                            repo.mark_guardian_authority_decision_intervention_applied(
                                decision_id, False, now,
                            )
                        else:
                            # I2 hardening fix: apply_live_sl_tightening
                            # itself always returns None (no status) - read
                            # back the resulting claim row and check its
                            # real terminal status. ONLY "SL_REPLACED"
                            # counts as applied; every other status
                            # (ABORTED_*/UNCERTAIN_*/REPLACEMENT_PARTIAL/
                            # CLAIMED/ANOMALY_*/missing row) is
                            # conservatively "not applied" - fail-closed for
                            # calibration purposes (see authority_live.py's
                            # own status vocabulary).
                            #
                            # Final-review fix (2026-09-15): the claim row in
                            # guardian_authority_live_sl_actions is a
                            # PERMANENT, position-lifetime row keyed by
                            # position_id ALONE (INSERT OR IGNORE - see
                            # authority_live.py's apply_live_sl_tightening
                            # idempotency-gate docstring). Once a position has
                            # genuinely tightened once, EVERY later tick's
                            # apply_live_sl_tightening call is a same-position
                            # no-op that returns without writing anything -
                            # but a bare status=="SL_REPLACED" check would
                            # then read back that SAME old terminal row and
                            # wrongly mark this brand-new decision as applied
                            # too. Requiring claimed_at to be exactly THIS
                            # tick's `now` closes that: only the one tick
                            # whose call actually performed the claim (and
                            # therefore wrote claimed_at=now) can ever see a
                            # match. Exact ISO-string equality, same join
                            # style _reconstruct_tighten_sl_factors uses for
                            # decided_at/observed_at elsewhere in this
                            # codebase - claim_guardian_authority_live_sl_action
                            # writes claimed_at from this exact same `now`
                            # object.
                            live_action = repo.get_guardian_authority_live_sl_action(
                                position.position_id
                            )
                            applied = (
                                live_action is not None
                                and live_action["status"] == "SL_REPLACED"
                                and live_action["claimed_at"] == now.isoformat()
                            )
                            repo.mark_guardian_authority_decision_intervention_applied(
                                decision_id, applied, now,
                            )
                else:
                    tightened = repo.tighten_position_stop_loss(
                        position.position_id, proposed_sl, now
                    )
                    if not tightened:
                        # Final-review fix C1/M1: this return value used to
                        # be silently discarded, so a refusal by the DB-level
                        # "only ever tighten" guard (the independent second
                        # enforcement layer alongside decide_open_position's
                        # own upstream check) produced zero visible trace
                        # anywhere. Same event-naming/severity precedent as
                        # authority_live.py's sibling
                        # ga_live_sl_aborted_invalid_tightening.
                        log_event(
                            run_id, event="ga_tick_paper_sl_tighten_refused",
                            position_id=position.position_id, instrument=position.instrument,
                            old_sl=str(position.stop_loss), new_sl=str(proposed_sl),
                            decision_id=decision_id, severity="ERROR",
                        )
                    # I2 hardening fix: `tightened` (already captured above
                    # since the C1/M1 fix) IS the write attempt's real
                    # outcome - use it directly, no new logic needed.
                    repo.mark_guardian_authority_decision_intervention_applied(
                        decision_id, tightened, now,
                    )
            elif decision == "CLOSE_EARLY":
                # CLOSE_EARLY writes via the exact same downstream mechanism
                # the deterministic EXIT state already uses -
                # repo.save_guardian_observation() with state="EXIT" - and
                # NO other closing code (Global Constraint). Returns
                # immediately after saving it: this is the tick's ONE
                # observation for this position, so the function must not
                # also fall through into the AI-narration path below (which
                # would spend an AI call/budget on a position already
                # flagged for closing) or the function's own final
                # observation build/save at the end (which would save a
                # SECOND observation for the same tick/position).
                exit_observation = GuardianObservation(
                    observation_id=f"ga-exit:{position.position_id}:{now.isoformat()}",
                    position_id=position.position_id, observed_at=now, state="EXIT",
                    decay_score=decay_score, progress_ratio=progress_ratio,
                    unrealized_pnl=unrealized_pnl,
                    factors={name: float(value) for name, value in factors.items()}, run_id=run_id,
                )
                repo.save_guardian_observation(exit_observation)
                log_event(
                    run_id, event="ga_tick_close_early", position_id=position.position_id,
                    decision_id=decision_id, confidence=confidence,
                )
                return exit_observation
        else:
            # TAKE_PROFIT (added alongside TIGHTEN_SL/CLOSE_EARLY): only
            # ever evaluated once decide_open_position's own downside-
            # protection decision comes back NO_ACTION this tick - downside
            # protection (tightening/closing on decay) always takes
            # precedence over profit-locking. Uses a completely separate,
            # disjoint factor vocabulary (progress_ratio/
            # unrealized_pnl_positive, neither of which is part of
            # `factors`/guardian_state above) and its own pure function/
            # threshold (decide_take_profit) - see that function's own
            # docstring for why this is safe to layer on top of the same
            # untyped live heuristics table `heuristics` already read above.
            profit_factors = {
                "progress_ratio": float(progress_ratio),
                "unrealized_pnl_positive": unrealized_pnl > 0,
            }
            tp_decision, tp_expected_outcome, tp_expected_direction, tp_confidence = (
                decide_take_profit(
                    profit_factors, heuristics, guardian_cfg.authority_take_profit_threshold,
                )
            )
            if tp_decision == "TAKE_PROFIT":
                _, tp_matched_ids = evaluate_heuristics(profit_factors, heuristics)
                tp_decision_id = f"ga:{position.position_id}:{now.isoformat()}"
                repo.save_guardian_authority_decision(
                    tp_decision_id, position.position_id, position.candidate_id,
                    "TAKE_PROFIT", now,
                    reasoning=tp_expected_outcome, expected_outcome=tp_expected_outcome,
                    expected_direction=tp_expected_direction, confidence=tp_confidence,
                    run_id=run_id,
                    # No SL is ever touched by TAKE_PROFIT.
                    old_sl=None, new_sl=None,
                    # Known-successful by construction, same reasoning as
                    # CLOSE_EARLY above: this closes via the guaranteed-
                    # success EXIT-observation mechanism below, never an
                    # exchange order, so there is no write-attempt outcome
                    # to determine later.
                    intervention_applied=True,
                    matched_heuristic_ids_json=json.dumps(tp_matched_ids),
                )
                tp_exit_observation = GuardianObservation(
                    observation_id=f"ga-exit:{position.position_id}:{now.isoformat()}",
                    position_id=position.position_id, observed_at=now, state="EXIT",
                    decay_score=decay_score, progress_ratio=progress_ratio,
                    unrealized_pnl=unrealized_pnl,
                    factors={name: float(value) for name, value in factors.items()}, run_id=run_id,
                )
                repo.save_guardian_observation(tp_exit_observation)
                log_event(
                    run_id, event="ga_tick_take_profit", position_id=position.position_id,
                    decision_id=tp_decision_id, confidence=tp_confidence,
                )
                return tp_exit_observation

    previous = repo.find_latest_guardian_observation(position.position_id)
    ai_reasoning: str | None = None
    ai_cost_usd: Decimal | None = None
    if should_invoke_ai(previous, new_state):
        if _budget_allows_one_more_call(repo, settings, now):
            context = build_ai_context(candidate, factors, decay_score, progress_ratio, unrealized_pnl, new_state)
            agent_def = load_agent_definition(_GUARDIAN_AGENT_FILE)
            assessment: GuardianAssessment = runner.run(agent_def, context, GuardianAssessment)
            billed = getattr(runner, "last_call_billed", True)
            cost = getattr(runner, "last_call_cost_usd", Decimal("0"))
            if billed:
                repo.record_ai_call_event(
                    Event(
                        event_id=f"AI_CALL_MADE:guardian:{position.position_id}:{run_id}",
                        event_type="AI_CALL_MADE", aggregate_type="position",
                        aggregate_id=position.position_id, occurred_at=now, run_id=run_id,
                        schema_version=1,
                        payload={"role": "guardian", "status": assessment.status, "cost_usd": str(cost)},
                    )
                )
                ai_cost_usd = cost
            if assessment.status == "ok":
                ai_reasoning = assessment.reasoning
        else:
            log_event(run_id, event="guardian_ai_deferred_budget", position_id=position.position_id)

    observation = GuardianObservation(
        observation_id=f"{position.position_id}:{now.isoformat()}",
        position_id=position.position_id,
        observed_at=now,
        state=new_state,
        decay_score=decay_score,
        progress_ratio=progress_ratio,
        unrealized_pnl=unrealized_pnl,
        factors={name: float(value) for name, value in factors.items()},
        ai_reasoning=ai_reasoning,
        ai_cost_usd=ai_cost_usd,
        run_id=run_id,
    )
    repo.save_guardian_observation(observation)
    log_event(
        run_id, event="guardian_observation_recorded", position_id=position.position_id,
        state=new_state, decay_score=str(decay_score),
    )
    return observation


def run_guardian_tick_body(
    repo: Repository, connector: GuardianDataSource, runner: AgentRunner, settings: Settings,
    run_id: str, now: datetime,
    live_connector: BingXLiveTradingConnector | None = None,
) -> list[GuardianObservation]:
    if live_connector is not None:
        # Restart/crash recovery MUST run once per tick, before any
        # apply_live_sl_tightening call in the same tick (Task 5's own
        # docstring requirement) - exactly as live_profit_protection.py's
        # own tick does internally. Guarded on live_connector rather than
        # settings.guardian.authority_enabled: a stale CLAIMED row left over
        # from when the flag was on must still be resolved forward even if
        # the flag is later turned off, and this is a cheap no-op (an empty
        # find_claimed_guardian_authority_live_sl_actions() result, no
        # exchange calls) whenever nothing is actually claimed.
        recover_claimed_live_sl_tightenings(repo, live_connector, run_id, now)

    # Guardian Authority self-critique (2026-09-14, Task 9), gated by the
    # same flag as the tick-time decision block in process_one_position
    # above. Runs ONCE per tick (not once per position), above the
    # per-position loop - same placement pattern as
    # recover_claimed_live_sl_tightenings above. Per this plan's own
    # cadence ruling: "opportunistic, not a new scheduler - runs as the
    # last step of the SAME tick that just resolved at least one new
    # decision". update_heuristics_from_resolved_decisions is only called
    # when resolve_pending_decisions actually resolved >= 1 decision this
    # tick - a tick that resolves nothing must not waste effort re-deriving
    # heuristics from unchanged data. Flag off (default): neither function
    # is ever called - byte-identical to before this task existed.
    if settings.guardian.authority_enabled:
        resolved_count = resolve_pending_decisions(repo, now)
        if resolved_count >= 1:
            update_heuristics_from_resolved_decisions(repo, now)

    observations = []
    for position in repo.find_open_positions():
        observation = process_one_position(
            repo, connector, runner, settings, position, run_id, now, live_connector,
        )
        if observation is not None:
            observations.append(observation)
    return observations
