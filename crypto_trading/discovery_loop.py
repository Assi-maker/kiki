from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.godfather.priority_boost import run_priority_boost_self_improvement_tick
from crypto_trading.guardian.self_improvement import run_godfather_self_improvement_tick
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.market_snapshot import LiveMarketDataSource, build_live_snapshot
from crypto_trading.paper_trading.live_discovery_gate import (
    SUPPRESSED_CAPACITY,
    SUPPRESSED_CAPITAL,
    LiveDiscoveryDecision,
    LiveDiscoveryGate,
)
from crypto_trading.paper_trading.recovery_sweep import sweep_confirmed_candidates_without_position
from crypto_trading.paper_trading.replay import run_single_cycle
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository


def run_discovery_tick(
    connector: LiveMarketDataSource,
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
    screener_runner: AgentRunner | None = None,
    live_connector: object | None = None,
    live_market_data_connector: object | None = None,
    live_discovery_gate: LiveDiscoveryGate | None = None,
) -> list[Position]:
    """En periodisk discovery-tick (SPEC §7, PLAN_CRYPTO_PHASE5.md Task 7):
    bygger en live `MarketSnapshot` (Task 6) och kör den genom exakt samma
    `run_single_cycle()`-pipeline som `replay.py` (Task 5/Beslut 1) - ingen
    duplicerad pipeline-logik, ingen skillnad mellan replay och live utöver
    varifrån snapshoten kommer. Det dagliga AI-anropstaket och
    `ANALYSIS_INTERRUPTED`-återupptagningen (Task 4) körs oförändrat inuti
    `run_single_cycle -> run_discovery_cycle`, aldrig kringgått här.

    Fail-safe på loop-nivå (Global Constraints, SPEC §8.3): ett oväntat
    undantag - connector nere, ett programmeringsfel mitt i en candidates
    analys, vad som helst - kraschar aldrig anroparen (`run_forever`).
    Det fångas, loggas och skrivs till `runs.errors`; en candidate som redan
    hann bli `UNDER_AI_ANALYSIS` innan kraschen läks av nästa ticks
    `sweep_interrupted_analyses` + återupptagningspolicy (Task 4) - ingen ny
    recovery-mekanism behövs här, den är redan komponerad av de tidigare
    tasken.

    `clock=lambda: datetime.now(UTC)` (bugfix 2026-08-31, bekräftad mot en
    riktig live-körning): `build_live_snapshot()`s staleness-kontroll för
    varje hämtad post bedöms mot en färsk tidpunkt tagen direkt efter just
    den postens nätverksanrop, inte mot detta `now` (fånget här, före hela
    den sekventiella hämtningsloopen). Utan detta blev varje instrument som
    hämtades mer än några sekunder in i en flera-minuter-lång live-hämtning
    felaktigt `data_quality_invalid` - se market_snapshot.py::
    build_live_snapshot() för full förklaring.

    Layer 1 capacity/cost gate (2026-09-06, spec:
    docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md §7):
    when live_connector is not None (only true once LIVE is armed in
    run.py), a reconciled live-capacity/margin check runs BEFORE the
    snapshot/candidate pipeline. If live is full or under-margined, this
    tick is skipped entirely - no snapshot fetch, no candidate search, no
    AI calls - logged as a clean 'ok' run, not an error. live_connector is
    None (the default) everywhere in this codebase today, so this is a
    zero-behavior-change no-op until that thread exists and is armed.

    AI-cost optimization (2026-09-19): that gate is now an analysis BUDGET,
    not just a full/not-full switch (paper_trading/live_discovery_gate.py).
    With live armed it derives, from reconciled free LIVE slots AND real
    BingX available margin, how many new LIVE positions could actually be
    opened: 0 (all slots taken - `discovery_suppressed_live_capacity` - or
    too little margin - `discovery_suppressed_live_capital`, or the check
    itself failed - `discovery_suppressed_live_check_failed`, fail-closed)
    skips the tick before any market fetch, candidate search or AI call;
    N > 0 caps the candidates sent to full analysis at N; "all slots free
    and affordable" leaves the normal budget untouched. Only this
    discovery tick is suppressed: the recovery sweep above, GODFATHER's
    <=1-call/day tick, and the separate monitoring/Guardian/live-execution
    threads all keep running. The final pre-order gate in
    live_execution.process_pending_positions is unchanged and still has the
    last word. Each tick records one DISCOVERY_LIVE_GATE event so
    performance/live_ai_cost_report.py can measure how often each gate
    blocks. `live_discovery_gate` carries the suppression cooldown across
    ticks (run_forever owns one); None = a fresh, non-debouncing gate.
    Candidates that waited longer than LIVE's signal TTL are never
    analysed (stale signals never consume AI)."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "discovery", now)
    try:
        sweep_confirmed_candidates_without_position(
            repo, connector, settings.risk_limits, now, run_id, settings
        )
    except Exception as exc:
        log_event(
            run_id, event="recovery_sweep_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
    # Task 7 (Guardian Authority Live Autonomy, 2026-09-15): the full
    # self-improvement pipeline (propose -> validate -> promote -> track/
    # demote, crypto_trading/guardian/self_improvement.py::
    # run_godfather_self_improvement_tick). Its own try/except (never
    # shared with the recovery sweep above or the live-capacity gate
    # below), gated by settings.guardian.authority_enabled - NOT
    # authority_shadow_enabled, a completely separate, already-shipped
    # concern (see self_improvement.py module docstring). Wired here
    # (discovery_loop.py), not monitoring_loop.py, because propose_
    # candidate_heuristics (Task 3) makes an LLM call and needs an
    # AgentRunner: run_monitoring_tick has no `runner` parameter at all
    # (only a LivePriceSource connector), while run_discovery_tick already
    # has `runner` in scope for exactly this reason.
    #
    # Placed BEFORE the live-capacity gate below (and its own early
    # return) so this pipeline runs every discovery tick regardless of
    # whether live capacity permits opening a new position this tick - an
    # unrelated concern, same "each concern gets its own try/except,
    # independent of the others" discipline monitoring_loop.py already
    # established for its own per-feature tick calls.
    try:
        if settings.guardian.authority_enabled:
            run_godfather_self_improvement_tick(repo, runner, settings, run_id, now)
    except Exception as exc:
        log_event(
            run_id, event="godfather_self_improvement_tick_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
    # GODFATHER priority-boost self-improvement (2026-09-18 expansion,
    # crypto_trading/godfather/priority_boost.py) - its own, completely
    # separate propose/validate/promote/demote pipeline, gated by its own
    # settings.godfather.priority_boost_enabled flag (independent of
    # settings.guardian.authority_enabled above - deliberately, see
    # config/loader.py::GodfatherConfig's own docstring). Own try/except,
    # same "one step's failure never blocks another concern's tick" rule
    # as every other independent concern in this function.
    try:
        run_priority_boost_self_improvement_tick(repo, runner, settings, run_id, now)
    except Exception as exc:
        log_event(
            run_id, event="godfather_priority_boost_tick_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
    analysis_cap: int | None = None
    stale_after_seconds: int | None = None
    if live_connector is not None:
        gate = live_discovery_gate or LiveDiscoveryGate(cooldown_seconds=0)
        decision = gate.evaluate(
            repo, live_connector, live_market_data_connector or connector,
            settings.live_execution, run_id, now,
        )
        if decision.suppressed_reason is not None:
            outcome = f"suppressed_{decision.suppressed_reason}"
        else:
            outcome = "normal" if decision.max_candidates is None else "capped"
        _record_gate_event(repo, run_id, now, outcome, decision)
        if decision.suppressed_reason is not None:
            log_event(
                run_id,
                event={
                    SUPPRESSED_CAPACITY: "discovery_suppressed_live_capacity",
                    SUPPRESSED_CAPITAL: "discovery_suppressed_live_capital",
                }.get(decision.suppressed_reason, "discovery_suppressed_live_check_failed"),
                active_count=decision.active_count, free_slots=decision.free_slots,
                affordable_slots=decision.affordable_slots,
                available_margin=decision.available_margin, from_cache=decision.from_cache,
            )
            repo.complete_run(run_id, datetime.now(UTC), "ok", [], instruments_scanned=0)
            return []
        analysis_cap = decision.max_candidates
        stale_after_seconds = settings.live_execution.signal_ttl_seconds
    try:
        snapshot = build_live_snapshot(
            connector, settings, now, clock=lambda: datetime.now(UTC), run_id=run_id
        )
        positions = run_single_cycle(
            snapshot,
            repo,
            runner,
            settings,
            run_id,
            news_connector=news_connector,
            external_data_connector=external_data_connector,
            screener_runner=screener_runner,
            live_analysis_cap=analysis_cap,
            stale_candidate_after_seconds=stale_after_seconds,
        )
        repo.complete_run(
            run_id, datetime.now(UTC), "ok", [], instruments_scanned=len(snapshot.instruments)
        )
        return positions
    except Exception as exc:
        log_event(
            run_id, event="discovery_tick_failed", error_type=type(exc).__name__, error=str(exc)
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])
        return []


def _record_gate_event(
    repo: Repository, run_id: str, now: datetime, outcome: str, decision: LiveDiscoveryDecision
) -> None:
    """One measurement row per LIVE-armed discovery tick (never raises - a
    failed measurement write must not disturb the trading tick)."""
    try:
        repo.record_event(
            Event(
                event_id=f"DISCOVERY_LIVE_GATE:{run_id}",
                event_type="DISCOVERY_LIVE_GATE",
                aggregate_type="discovery_run",
                aggregate_id=run_id,
                occurred_at=now,
                run_id=run_id,
                schema_version=1,
                payload={
                    "outcome": outcome,
                    "max_candidates": decision.max_candidates,
                    "active_count": decision.active_count,
                    "free_slots": decision.free_slots,
                    "affordable_slots": decision.affordable_slots,
                    "usable_slots": decision.usable_slots,
                    "available_margin": decision.available_margin,
                    "from_cache": decision.from_cache,
                },
            )
        )
    except Exception as exc:
        log_event(
            run_id, event="discovery_gate_event_failed",
            error_type=type(exc).__name__, error=str(exc),
        )


def run_forever(
    connector: LiveMarketDataSource,
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
    screener_runner: AgentRunner | None = None,
    live_connector: object | None = None,
    live_market_data_connector: object | None = None,
) -> None:
    live_discovery_gate = LiveDiscoveryGate(
        cooldown_seconds=settings.live_execution.discovery_gate_cooldown_seconds
    )
    while True:
        run_discovery_tick(
            connector,
            repo,
            runner,
            settings,
            news_connector=news_connector,
            external_data_connector=external_data_connector,
            screener_runner=screener_runner,
            live_connector=live_connector,
            live_market_data_connector=live_market_data_connector,
            live_discovery_gate=live_discovery_gate,
        )
        time.sleep(settings.pipeline.discovery_interval_minutes * 60)
