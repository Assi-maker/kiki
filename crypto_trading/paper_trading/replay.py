from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.orchestrator import run_discovery_cycle
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.paper_trading.position_opening import open_position_for_candidate
from crypto_trading.schemas.market import FundingRate, InstrumentMetadata, Kline, Ticker
from crypto_trading.schemas.trade import Position
from crypto_trading.screening.candidate_engine import prioritize_and_apply_budget, process_evidence
from crypto_trading.screening.eligibility_filter import check_eligibility, select_top_n
from crypto_trading.screening.quant_screener import evaluate_candidate
from crypto_trading.storage.repository import Repository


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
) -> list[Position]:
    """En enda discovery->gate->paper-trading-cykel mot EN snapshot (Fas 5,
    PLAN_CRYPTO_PHASE5.md Task 5/Beslut 1) - faktoriserad ut ur run_replay()
    så att discovery_loop.py (Fas 5) kan anropa exakt samma logik en gång per
    live tick, utan duplicering av pipeline-logiken. `news_connector`/
    `external_data_connector` (Fas 5.5 Task 3) är valfria passthrough-
    parametrar till run_discovery_cycle - run_replay() skickar aldrig in
    dem, så replay-vägens determinism/look-ahead-bias-garantier är
    mekaniskt opåverkade."""
    eligible_tickers = _select_eligible_tickers(snapshot, settings)
    top_n_symbols = select_top_n(eligible_tickers, settings.pipeline.top_n)

    new_candidates = []
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
        )
        candidate = process_evidence(
            repo,
            evidence,
            discovery_run_id=run_id,
            created_at=snapshot.simulated_now,
            cooldown_minutes=settings.pipeline.cooldown_minutes,
            evidence_change_threshold=float(
                settings.pipeline.evidence_change_threshold_for_reanalysis
            ),
        )
        if candidate is not None:
            new_candidates.append(candidate)

    liquidity_by_instrument = {t.instrument: t.quote_volume for t in eligible_tickers}
    prioritize_and_apply_budget(
        repo,
        new_candidates,
        liquidity_by_instrument,
        settings.budget_limits.max_candidates_per_discovery_run,
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
    )

    opened: list[Position] = []
    for candidate in processed:
        if candidate.status != "CONFIRMED":
            continue
        reference_price = snapshot.tickers[candidate.instrument].last_price
        position = open_position_for_candidate(
            candidate,
            repo,
            settings.risk_limits,
            reference_price,
            snapshot.simulated_now,
            run_id,
        )
        if position is not None:
            opened.append(position)

    price_lookup = _build_price_lookup(snapshot)
    close_triggered_positions(
        repo, price_lookup, snapshot.simulated_now, settings.risk_limits, run_id
    )

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
