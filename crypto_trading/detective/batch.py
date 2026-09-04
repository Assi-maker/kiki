from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.detective.context import build_position_analysis_context
from crypto_trading.detective.stats import (
    compute_batch_win_loss_counts,
    compute_breakdown_by_signal_type,
)
from crypto_trading.logging import log_event
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.detective import DetectiveAnalysisRecord, DetectiveBatchAnalysis
from crypto_trading.schemas.event import Event
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import Repository

_DETECTIVE_AGENT_FILE = "crypto-detective.md"

# Kostnadsbudget: samma konservativa worst-case-princip som
# orchestrator.py::_CONSERVATIVE_COST_PER_CANDIDATE_USD, men för ETT
# Detective-batchanrop (en batch, en modellrunda) istället för sju roller.
# En batch på upp till `batch_size` stängda positioners fulla underlag
# (evidence_record + upp till sju assessments per position) kan bli
# betydligt större än en enskild rolls kontext - 30 000 inputtokens är en
# generös övre gräns, samma max_tokens=16000/dyraste-modell-antagande som
# orchestrator.py: (30000/1e6)*2 + (16000/1e6)*10 = $0.22, avrundat uppåt
# till $0.25 som äkta säkerhetsnät.
_WORST_CASE_COST_PER_BATCH_USD = Decimal("0.25")


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _safe_get_candidate(repo: Repository, candidate_id: str) -> Candidate | None:
    """En korrupt candidate-rad (CorruptCandidateStateError) får aldrig
    krascha en hel Detective-batch - samma "en trasig rad hoppas över,
    resten fortsätter"-princip som storage/repository.py::
    find_candidates_by_status()/find_all_candidates() redan använder."""
    try:
        return repo.get_candidate(candidate_id)
    except CorruptCandidateStateError:
        return None


def run_detective_batch(
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    now: datetime,
) -> DetectiveAnalysisRecord | None:
    """En Detective-batchanalys av redan STÄNGDA PAPER-trades (Post-Trade
    Analyst). Körs ALDRIG av Orchestrator/Gate, fattar aldrig ett
    CONFIRMED/NO_TRADE/REJECTED-beslut, öppnar/stänger aldrig en position,
    och ändrar aldrig config/strategi - producerar uteslutande
    observationer/hypoteser, persisterade separat (storage/db.py::
    detective_analyses).

    Batchar (kostnadsoptimering, explicit användarkrav): väntar tills minst
    `settings.detective.batch_size` nya stängda positioner väntar (se
    Repository.find_closed_positions_pending_detective_analysis()) innan
    den gör ETT AI-anrop för hela batchen, istället för ett dyrt anrop per
    enskild trade. Returnerar None (ingen effekt alls) om tröskeln inte är
    nådd - positionerna lämnas orörda, upptäcks igen nästa tick
    (restart-säkert: se Repository.save_detective_analysis()s atomiska
    markering av vilka positioner en given analys täckte).

    Delar samma dagliga $-budget som de sju rollerna/Opportunity Screener
    (settings.budget_limits.max_daily_ai_cost_usd/max_ai_calls_per_day) -
    kringgår den ALDRIG (explicit användarkrav): om en konservativ
    worst-case-uppskattning av detta batchanrop skulle spränga taket,
    skjuts batchen upp till en senare tick (positionerna förblir
    ej-analyserade, ingen förlorad historik).

    Ett misslyckat AI-anrop (status != "ok", t.ex. retry-uttömning) blir
    ändå en persisterad status="failed"-rad OCH markerar batchens
    positioner som analyserade - annars skulle en permanent misslyckande
    batch försöka om varje tick i all oändlighet."""
    batch_size = settings.detective.batch_size
    pending_count = repo.count_closed_positions_pending_detective_analysis()
    if pending_count < batch_size:
        return None

    day_start = _utc_day_start(now)
    daily_count_at_start = repo.count_ai_calls_since(day_start)
    daily_cost_at_start = repo.sum_ai_cost_since(day_start)
    calls_would_exceed = daily_count_at_start + 1 > settings.budget_limits.max_ai_calls_per_day
    cost_would_exceed = (
        daily_cost_at_start + _WORST_CASE_COST_PER_BATCH_USD
        > settings.budget_limits.max_daily_ai_cost_usd
    )
    if calls_would_exceed or cost_would_exceed:
        log_event(
            run_id,
            event="detective_batch_deferred_budget",
            pending_count=pending_count,
            calls_would_exceed=calls_would_exceed,
            cost_would_exceed=cost_would_exceed,
        )
        return None

    batch_positions = repo.find_closed_positions_pending_detective_analysis(batch_size)
    candidates_by_id: dict[str, Candidate] = {}
    gate_decisions_by_position: dict[str, dict | None] = {}
    for position in batch_positions:
        candidate = _safe_get_candidate(repo, position.candidate_id)
        if candidate is not None:
            candidates_by_id[position.candidate_id] = candidate
        gate_decisions_by_position[position.position_id] = repo.get_gate_decision(
            position.candidate_id
        )

    position_contexts = [
        build_position_analysis_context(
            position,
            candidates_by_id.get(position.candidate_id),
            gate_decisions_by_position.get(position.position_id),
        )
        for position in batch_positions
    ]

    counts = compute_batch_win_loss_counts(batch_positions)
    signal_type_breakdown = compute_breakdown_by_signal_type(batch_positions, candidates_by_id)

    context: dict = {
        "run_id": run_id,
        "batch_trades": position_contexts,
        "batch_signal_type_breakdown": signal_type_breakdown,
    }

    historical_breakdown = None
    all_closed = repo.find_closed_positions()
    if len(all_closed) >= settings.detective.min_history_for_win_loss_comparison:
        all_candidates_by_id = {}
        for position in all_closed:
            candidate = _safe_get_candidate(repo, position.candidate_id)
            if candidate is not None:
                all_candidates_by_id[position.candidate_id] = candidate
        historical_breakdown = compute_breakdown_by_signal_type(all_closed, all_candidates_by_id)
        context["historical_signal_type_breakdown"] = historical_breakdown

    agent_def = load_agent_definition(_DETECTIVE_AGENT_FILE)
    assessment = runner.run(agent_def, context, DetectiveBatchAnalysis)

    billed = getattr(runner, "last_call_billed", True)
    cost_usd = getattr(runner, "last_call_cost_usd", Decimal("0"))
    if billed:
        repo.record_ai_call_event(
            Event(
                event_id=f"AI_CALL_MADE:detective:{run_id}",
                event_type="AI_CALL_MADE",
                aggregate_type="detective",
                aggregate_id=run_id,
                occurred_at=now,
                run_id=run_id,
                schema_version=1,
                payload={
                    "role": "detective",
                    "status": assessment.status,
                    "cost_usd": str(cost_usd),
                },
            )
        )

    record_status = "ok" if assessment.status == "ok" else "failed"
    record = DetectiveAnalysisRecord(
        analysis_id=f"detective-{uuid.uuid4()}",
        created_at=now,
        position_ids=[p.position_id for p in batch_positions],
        win_count=counts["win_count"],
        loss_count=counts["loss_count"],
        breakeven_count=counts["breakeven_count"],
        status=record_status,
        observations=assessment.observations if record_status == "ok" else [],
        winning_patterns=assessment.winning_patterns if record_status == "ok" else [],
        losing_patterns=assessment.losing_patterns if record_status == "ok" else [],
        stats_snapshot={
            "batch_signal_type_breakdown": signal_type_breakdown,
            "historical_signal_type_breakdown": historical_breakdown,
        },
        ai_cost_usd=cost_usd if billed else Decimal("0"),
    )
    repo.save_detective_analysis(record)
    log_event(
        run_id,
        event="detective_batch_completed",
        analysis_id=record.analysis_id,
        position_count=len(batch_positions),
        status=record.status,
        win_count=record.win_count,
        loss_count=record.loss_count,
    )
    return record
