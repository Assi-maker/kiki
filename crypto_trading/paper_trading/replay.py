from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.guardian.authority import (
    maybe_open_position_for_candidate,
    maybe_record_pre_entry_shadow,
)
from crypto_trading.logging import log_event
from crypto_trading.orchestrator import _ROLE_ORDER, run_discovery_cycle
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.market import (
    FundingRate,
    InstrumentMetadata,
    Kline,
    OpenInterest,
    Ticker,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.screening.candidate_engine import (
    apply_opportunity_screening,
    prioritize_and_apply_budget,
    process_evidence,
)
from crypto_trading.screening.eligibility_filter import check_eligibility, select_top_n
from crypto_trading.screening.quant_screener import evaluate_candidate
from crypto_trading.storage.repository import Repository

_OPPORTUNITY_SCREENER_AGENT_FILE = "crypto-opportunity-screener.md"


class MarketSnapshot(BaseModel):
    """En handkonstruerad, redan hämtad tidpunkt i en historisk replay (se
    PLAN_CRYPTO_PHASE4.md beslut 5 - ingen BingX-backfill/paginering här).
    `klines`/`funding_rates` är kumulativa listor upp till `simulated_now`
    (samma form quant_screener redan filtrerar via _sorted_up_to); `tickers`
    representerar ögonblicksbilden VID simulated_now."""

    model_config = {"arbitrary_types_allowed": True}

    simulated_now: datetime
    instruments: dict[str, InstrumentMetadata]
    tickers: dict[str, Ticker]
    klines: dict[str, list[Kline]]
    funding_rates: dict[str, list[FundingRate]]
    data_quality_status: dict[str, Literal["ok", "invalid"]]
    secondary_klines: dict[str, list[Kline]] = {}
    secondary_funding_rates: dict[str, list[FundingRate]] = {}
    # 2026-09-29: the open interest discovery already fetches per top-N symbol
    # (previously only quality-checked, then discarded). Empty in replays.
    open_interest: dict[str, OpenInterest] = {}


def run_replay(
    snapshots: list[MarketSnapshot],
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
) -> list[Position]:
    """Kedjar hela discovery->gate->paper-trading-pipelinen mot en
    tidsordnad lista handkonstruerade snapshots, genom att köra
    run_single_cycle() en gång per snapshot i tidsordning. Look-ahead-bias-
    fritt (SPEC §8.4): varje steg skickar bara evaluated_at=snapshot.simulated_now
    in i quant_screener, som redan filtrerar bort framtida datapunkter
    internt (_sorted_up_to, Fas 2)."""
    ordered = sorted(snapshots, key=lambda s: s.simulated_now)
    all_confirmed: list[Position] = []

    for snapshot in ordered:
        all_confirmed.extend(run_single_cycle(snapshot, repo, runner, settings, run_id))

    return [repo.get_position(p.position_id) for p in all_confirmed]


def run_single_cycle(
    snapshot: MarketSnapshot,
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
    screener_runner: AgentRunner | None = None,
    live_analysis_cap: int | None = None,
    stale_candidate_after_seconds: int | None = None,
    clock: Callable[[], datetime] | None = None,
    pre_ai_shadow: Callable[[tuple, MarketSnapshot, str], None] | None = None,
) -> list[Position]:
    """En enda discovery->gate->paper-trading-cykel mot EN snapshot (Fas 5,
    PLAN_CRYPTO_PHASE5.md Task 5/Beslut 1) - faktoriserad ut ur run_replay()
    så att discovery_loop.py (Fas 5) kan anropa exakt samma logik en gång per
    live tick, utan duplicering av pipeline-logiken. `news_connector`/
    `external_data_connector` (Fas 5.5 Task 3) är valfria passthrough-
    parametrar till run_discovery_cycle - run_replay() skickar aldrig in
    dem, så replay-vägens determinism/look-ahead-bias-garantier är
    mekaniskt opåverkade.

    `screener_runner` (kostnadsoptimering 2026-09-02): valfri, separat
    AgentRunner för den billiga Opportunity Screener-etappen (t.ex. Haiku
    4.5). `None` (default) = exakt samma beteende som innan denna etapp
    fanns - varje befintligt anrop utan denna parameter är opåverkat.
    replay.py:s egen `run_replay()` skickar aldrig in den (samma
    determinism-skäl som news_connector ovan).

    `live_analysis_cap` / `stale_candidate_after_seconds` (2026-09-19,
    AI-cost optimization; both default None = unchanged behavior, replay
    never passes them): the LIVE-armed discovery tick's analysis budget -
    number of free-and-affordable LIVE slots, and LIVE's signal TTL. The cap
    is applied at TWO points so it is never bypassed: as the candidate
    budget handed to prioritize_and_apply_budget() (the BEST-ranked
    candidates are the ones kept, the rest become BUDGET_LIMITED with reason
    "live_slot_budget" and are never sent to the Haiku prescreen or the
    7-role chain), and as a hard ceiling inside run_discovery_cycle() (which
    also covers leftover ANALYSIS_INTERRUPTED candidates). It only limits
    HOW MANY qualified candidates get analysed - it never changes what the
    analysis, the Gate, sizing or any risk limit does."""
    eligible_tickers = _select_eligible_tickers(snapshot, settings)
    top_n_symbols = select_top_n(eligible_tickers, settings.pipeline.top_n)

    secondary_timeframe = (
        settings.pipeline.screener_timeframes[1]
        if len(settings.pipeline.screener_timeframes) > 1
        else None
    )

    new_candidates = []
    # 2026-09-29 funnel observability: counted here, recorded once per cycle.
    skips: dict[str, list[str]] = {"rejected_cooldown": [], "kernel_reject_cooldown": []}
    quant_shortlist = data_invalid = 0
    for symbol in top_n_symbols:
        evidence = evaluate_candidate(
            instrument=symbol,
            timeframes=settings.pipeline.screener_timeframes,
            klines=snapshot.klines.get(symbol, []),
            funding_rates=snapshot.funding_rates.get(symbol, []),
            data_quality_status=snapshot.data_quality_status.get(symbol, "invalid"),
            evaluated_at=snapshot.simulated_now,
            price_volatility_threshold_pct=settings.pipeline.screener_price_volatility_threshold_pct,
            lookback=settings.pipeline.screener_lookback_periods,
            rsi_period=settings.pipeline.screener_rsi_period,
            rsi_overbought_threshold=settings.pipeline.screener_rsi_overbought_threshold,
            volume_zscore_threshold=settings.pipeline.screener_volume_zscore_threshold,
            funding_rate_threshold_pct=settings.pipeline.screener_funding_rate_threshold_pct,
            secondary_timeframe=secondary_timeframe,
            secondary_klines=snapshot.secondary_klines.get(symbol, []),
            secondary_funding_rates=snapshot.secondary_funding_rates.get(symbol, []),
        )
        if evidence.data_quality_status == "invalid":
            data_invalid += 1
        elif evidence.outcome == "worth_deeper_analysis":
            quant_shortlist += 1
        candidate = process_evidence(
            repo,
            evidence,
            discovery_run_id=run_id,
            created_at=snapshot.simulated_now,
            cooldown_minutes=settings.pipeline.cooldown_minutes,
            evidence_change_threshold=float(
                settings.pipeline.evidence_change_threshold_for_reanalysis
            ),
            # Root-cause-fix (2026-09-02): utan detta referenspris hade Risk
            # Agent aldrig kunnat svara med ett absolut, Decimal-parsbart
            # suggested_stop_loss/suggested_target (position_opening.py) -
            # bara en kvalitativ beskrivning som alltid misslyckades
            # parsningen (0/10 CONFIRMED öppnade någonsin en position).
            reference_price=snapshot.tickers[symbol].last_price,
            kernel_reject_cooldown_minutes=settings.pipeline.kernel_reject_cooldown_minutes,
            on_skip=lambda reason, symbol=symbol: skips[reason].append(symbol),
        )
        if candidate is not None:
            new_candidates.append(candidate)

    _record_market_observations(repo, snapshot, top_n_symbols, run_id)
    if pre_ai_shadow is not None:
        # 2026-09-29 SHADOW pre-AI feasibility: measured, never acted on. It
        # gets an immutable copy and its outcome is ignored - every candidate
        # continues to the budget, the AI chain, the Gate and the Safety Kernel.
        try:
            pre_ai_shadow(tuple(new_candidates), snapshot, run_id)
        except Exception as exc:  # noqa: BLE001
            log_event(run_id, event="pre_ai_feasibility_shadow_failed", error_type=type(exc).__name__,
                      error=str(exc)[:300])
    liquidity_by_instrument = {t.instrument: t.quote_volume for t in eligible_tickers}
    candidate_budget = settings.budget_limits.max_candidates_per_discovery_run
    cap_is_binding = live_analysis_cap is not None and live_analysis_cap < candidate_budget
    if cap_is_binding:
        candidate_budget = live_analysis_cap
    within_budget, over_budget = prioritize_and_apply_budget(
        repo,
        new_candidates,
        liquidity_by_instrument,
        candidate_budget,
        snapshot.simulated_now,
        run_id,
        settings=settings,
        limited_reason="live_slot_budget" if cap_is_binding else None,
    )

    if screener_runner is not None:
        screener_agent_def = load_agent_definition(_OPPORTUNITY_SCREENER_AGENT_FILE)
        apply_opportunity_screening(
            repo,
            within_budget,
            screener_agent_def,
            screener_runner,
            settings.budget_limits.max_candidates_for_ai_prescreen,
            settings.budget_limits.max_candidates_for_full_analysis,
            settings.budget_limits.opportunity_screening_enforce,
            snapshot.simulated_now,
            run_id,
        )

    processed = run_discovery_cycle(
        repo,
        runner,
        settings,
        run_id,
        news_connector=news_connector,
        external_data_connector=external_data_connector,
        now=snapshot.simulated_now,
        max_analyses=live_analysis_cap,
        stale_after_seconds=stale_candidate_after_seconds,
        clock=clock,
    )

    opened = _open_positions_for_confirmed_candidates(processed, snapshot, repo, settings, run_id)
    _record_funnel(
        repo, run_id, snapshot.simulated_now,
        markets_scanned=len(snapshot.instruments), eligible=len(eligible_tickers),
        top_n=len(top_n_symbols), quant_shortlist=quant_shortlist, data_invalid=data_invalid,
        skips=skips, created=[c for c in new_candidates if c.status != "DATA_INVALID"],
        budget_limited=len(over_budget), processed=processed, opened=len(opened),
    )

    price_lookup = _build_price_lookup(snapshot)
    close_triggered_positions(
        repo, price_lookup, snapshot.simulated_now, settings.risk_limits, run_id
    )

    return opened


def _record_market_observations(repo, snapshot, symbols, run_id: str) -> None:
    """2026-09-29: keep the open interest, latest funding rate and price that
    this cycle ALREADY fetched (no extra API call), so OI/funding research
    becomes possible going forward - OI has no public history. Never raises."""
    if not snapshot.open_interest:
        return
    try:
        rows = []
        for sym in symbols:
            oi = snapshot.open_interest.get(sym)
            if oi is None:
                continue
            funding = snapshot.funding_rates.get(sym) or []
            ticker = snapshot.tickers.get(sym)
            rows.append({
                "symbol": sym, "observed_at": oi.observed_at.isoformat(),
                "open_interest": str(oi.open_interest),
                "funding_rate": str(funding[-1].funding_rate) if funding else None,
                "last_price": str(ticker.last_price) if ticker is not None else None,
            })
        repo.save_market_observations(run_id, snapshot.simulated_now, rows)
    except Exception as exc:  # noqa: BLE001 - measurement never disturbs the tick
        log_event(run_id, event="market_observations_failed", error_type=type(exc).__name__,
                  error=str(exc)[:300])


def _record_funnel(repo, run_id, now, *, markets_scanned, eligible, top_n, quant_shortlist,
                   data_invalid, skips, created, budget_limited, processed, opened) -> None:
    """One DISCOVERY_FUNNEL row per discovery cycle (2026-09-29): every stage
    count, so an audit can say exactly where candidates disappeared. The
    per-candidate path after the Gate (Safety Kernel, execution) lives in
    its own tables - see performance/funnel_report.py. Never raises."""
    try:
        gate: dict[str, dict[str, int]] = {}
        ai_ok = ai_failed = 0
        for c in processed:
            roles = [getattr(c, r, None) for r in _ROLE_ORDER]
            if all(a is not None and a.status == "ok" for a in roles):
                ai_ok += 1
            else:
                ai_failed += 1
            evaluation = repo.get_gate_evaluation(c.candidate_id)
            reasons = (evaluation or {}).get("detail", {}).get("reasons") or ["unknown"]
            bucket = gate.setdefault(c.status, {})
            for reason in reasons:
                bucket[reason] = bucket.get(reason, 0) + 1
        payload = {
            "markets_scanned": markets_scanned, "eligible": eligible, "top_n": top_n,
            "quant_shortlist": quant_shortlist, "data_invalid": data_invalid,
            "skipped_rejected_cooldown": len(skips["rejected_cooldown"]),
            "skipped_kernel_reject_cooldown": len(skips["kernel_reject_cooldown"]),
            "skipped_symbols": skips,
            "candidates_created": len(created), "budget_limited": budget_limited,
            "ai_started": len(processed), "ai_all_roles_ok": ai_ok, "ai_failed": ai_failed,
            "gate": gate, "confirmed": sum(1 for c in processed if c.status == "CONFIRMED"),
            "paper_positions_opened": opened,
        }
        repo.record_event(Event(
            event_id=f"DISCOVERY_FUNNEL:{run_id}", event_type="DISCOVERY_FUNNEL",
            aggregate_type="discovery_run", aggregate_id=run_id, occurred_at=now,
            run_id=run_id, schema_version=1, payload=payload,
        ))
        log_event(run_id, event="discovery_funnel",
                  **{k: v for k, v in payload.items() if k != "skipped_symbols"})
    except Exception as exc:  # noqa: BLE001 - measurement must never disturb the trading tick
        log_event(run_id, event="discovery_funnel_failed", error_type=type(exc).__name__, error=str(exc))


def _open_positions_for_confirmed_candidates(
    processed: list[Candidate],
    snapshot: MarketSnapshot,
    repo: Repository,
    settings: Settings,
    run_id: str,
) -> list[Position]:
    """Opens a PAPER position immediately for each newly-CONFIRMED candidate
    from this cycle's run_discovery_cycle() call, using this cycle's own
    fresh ticker price as the reference price - unchanged from the original
    inline-loop behavior.

    Wrapped per-candidate (P2 remediation, 2026-09-11): one candidate's
    failure (a missing snapshot.tickers entry, a transient open_position_
    for_candidate() error) must never stop the remaining candidates in the
    same batch from getting their position opened too - the same "one bad
    item never blocks the batch" principle used everywhere else in this
    codebase. A candidate skipped here is not permanently lost: paper_
    trading/recovery_sweep.py::sweep_confirmed_candidates_without_position()
    recovers it on a later discovery tick."""
    opened: list[Position] = []
    for candidate in processed:
        if candidate.status != "CONFIRMED":
            continue
        try:
            reference_price = snapshot.tickers[candidate.instrument].last_price
            position = maybe_open_position_for_candidate(
                repo, candidate, settings.risk_limits, reference_price,
                snapshot.simulated_now, run_id, settings,
            )
        except Exception as exc:
            log_event(
                run_id, event="position_open_failed", candidate_id=candidate.candidate_id,
                instrument=candidate.instrument, error_type=type(exc).__name__, error=str(exc),
            )
            continue
        if position is not None:
            opened.append(position)
        # Task 6 (Guardian Authority Shadow/Observation Mode, 2026-09-15):
        # purely observational sibling call, placed AFTER `position` has
        # already been used (the append above) so that even a hypothetical
        # violation of maybe_record_pre_entry_shadow's own "never raises"
        # guarantee could not retroactively affect whether this candidate's
        # real position was opened/returned - it is a genuine no-op on the
        # real code path in every respect but timing. Never gated behind a
        # branch that could be skipped while maybe_open_position_for_
        # candidate still proceeds - this candidate's real outcome is fully
        # decided by this point either way.
        maybe_record_pre_entry_shadow(candidate, repo, settings, run_id, snapshot.simulated_now)
    return opened


def _select_eligible_tickers(snapshot: MarketSnapshot, settings: Settings) -> list[Ticker]:
    eligible = []
    for symbol, ticker in snapshot.tickers.items():
        instrument = snapshot.instruments.get(symbol)
        if instrument is None:
            continue
        dq = snapshot.data_quality_status.get(symbol, "invalid")
        ok, _reason = check_eligibility(
            instrument,
            ticker,
            dq,
            settings.pipeline.eligibility_min_quote_volume_24h_usdt,
            settings.pipeline.eligibility_max_spread_pct,
        )
        if ok:
            eligible.append(ticker)
    return eligible


def _build_price_lookup(
    snapshot: MarketSnapshot,
) -> dict[str, tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Övervakning tittar bara på den SENASTE candle:n vid detta snapshot
    (inte kumulativt historiskt low/high, som skulle blanda ihop tidpunkter
    positionen aldrig var öppen under)."""
    price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for symbol, klines in snapshot.klines.items():
        visible = [k for k in klines if k.observed_at <= snapshot.simulated_now]
        if not visible:
            continue
        latest = max(visible, key=lambda k: k.observed_at)
        current_price = (
            snapshot.tickers[symbol].last_price if symbol in snapshot.tickers else latest.close
        )
        funding_rates = snapshot.funding_rates.get(symbol, [])
        visible_funding = [f for f in funding_rates if f.observed_at <= snapshot.simulated_now]
        funding_rate = (
            max(visible_funding, key=lambda f: f.observed_at).funding_rate
            if visible_funding
            else Decimal("0")
        )
        price_lookup[symbol] = (latest.low, latest.high, current_price, funding_rate)
    return price_lookup
