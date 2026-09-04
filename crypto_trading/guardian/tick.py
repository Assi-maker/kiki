from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.guardian.ai_context import build_ai_context, should_invoke_ai
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
) -> list[GuardianObservation]:
    observations = []
    for position in repo.find_open_positions():
        observation = process_one_position(repo, connector, runner, settings, position, run_id, now)
        if observation is not None:
            observations.append(observation)
    return observations
