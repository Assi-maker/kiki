"""Read-only PAPER-track-record-rapport (2026-09-03).

Datainsamlings-/valideringssteg för den kontinuerliga produktions-PAPER-
körningen (SPEC/PLAN oförändrade - ingen strategi/threshold/enforce-ändring
här). Ren rapportering: komponerar uteslutande redan testade Repository-/
performance/metrics.py-funktioner, skriver ALDRIG till DB:n, startas ALDRIG
av run.py (körs manuellt: `python -m crypto_trading.performance.
paper_track_report`).

En position vars `size` trycktes till 0 av max_total_exposure_pct-taket
(paper_trading/position_sizing.py::compute_position_size, när
open_positions_notional redan når exponeringstaket) representerar noll
verklig marknadsexponering. Den räknas här separat som
`blocked_by_exposure` och exkluderas explicit ur alla trading-mått (win
rate/profit factor/expectancy/drawdown/total P&L) - annars skulle en sådan
nollstorlekstrade felaktigt räknas som break-even i statistiken (2026-09-03,
explicit användarkrav).
"""

from __future__ import annotations

import glob
import json
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.performance.metrics import (
    compute_cumulative_pnl,
    compute_drawdown,
    compute_expectancy,
    compute_profit_factor,
    compute_win_rate,
    trade_pnls,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_CANDIDATE_STATUSES = (
    "CANDIDATE",
    "UNDER_AI_ANALYSIS",
    "ANALYSIS_INTERRUPTED",
    "CONFIRMED",
    "REJECTED",
    "NO_TRADE",
    "BUDGET_LIMITED",
)


def _is_blocked_by_exposure(position: Position) -> bool:
    return position.size == Decimal("0")


def _unrealized_pnl(connector: BingXMarketDataConnector, position: Position) -> Decimal | str:
    """Samma gross-prisavkastning * notional-formel som paper_trading/
    execution.py::compute_pnl() (LONG-only, samma antagande som resten av
    systemet) - men mot en färskt hämtad livepris istället för
    simulated_fill_exit, eftersom positionen ännu inte är stängd. Rent
    ephemeralt för rapportering, lagras aldrig."""
    try:
        ticker = connector.get_ticker(position.instrument)
        last_price = Decimal(str(ticker["lastPrice"]))
    except Exception as exc:  # nätverksfel ska aldrig krascha rapporten
        return f"unavailable: {type(exc).__name__}"
    price_return = (last_price - position.simulated_fill_entry) / position.simulated_fill_entry
    return position.size * price_return


def _sum_ai_cost_from_logs(log_glob: str) -> dict:
    haiku_calls = 0
    haiku_cost = Decimal("0")
    sonnet_calls = 0
    sonnet_cost = Decimal("0")
    for path in glob.glob(log_glob):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") != "agent_call_usage":
                    continue
                cost = Decimal(str(event.get("estimated_cost_usd", 0)))
                if event.get("model") == "claude-haiku-4-5":
                    haiku_calls += 1
                    haiku_cost += cost
                else:
                    sonnet_calls += 1
                    sonnet_cost += cost
    return {
        "haiku_calls": haiku_calls,
        "haiku_cost_usd": str(haiku_cost),
        "sonnet_calls": sonnet_calls,
        "sonnet_cost_usd": str(sonnet_cost),
        "total_cost_usd": str(haiku_cost + sonnet_cost),
    }


def build_report(
    repo: SQLiteRepository, connector: BingXMarketDataConnector, log_glob: str
) -> dict:
    candidate_counts = {
        status: len(repo.find_candidates_by_status(status)) for status in _CANDIDATE_STATUSES
    }

    all_positions = repo.find_all_positions(limit=10_000)
    real_positions = [p for p in all_positions if not _is_blocked_by_exposure(p)]
    blocked_positions = [p for p in all_positions if _is_blocked_by_exposure(p)]

    real_open = [p for p in real_positions if p.status == "OPEN_POSITION"]
    real_closed = [p for p in real_positions if p.status == "CLOSED"]

    open_position_rows = []
    for p in sorted(real_open, key=lambda x: x.opened_at):
        unrealized = _unrealized_pnl(connector, p)
        open_position_rows.append(
            {
                "instrument": p.instrument,
                "entry": str(p.simulated_fill_entry),
                "size_usdt": str(p.size),
                "stop_loss": str(p.stop_loss),
                "target": str(p.target),
                "opened_at": p.opened_at.isoformat(),
                "unrealized_pnl_usdt": (
                    str(unrealized) if isinstance(unrealized, Decimal) else unrealized
                ),
            }
        )

    closed_position_rows = []
    for p in sorted(real_closed, key=lambda x: x.closed_at):
        pnl = trade_pnls([p])[0]
        pnl_pct = (pnl / p.size) if p.size != 0 else None
        duration_hours = (p.closed_at - p.opened_at).total_seconds() / 3600
        closed_position_rows.append(
            {
                "instrument": p.instrument,
                "entry": str(p.simulated_fill_entry),
                "exit": str(p.simulated_fill_exit),
                "realized_pnl_usdt": str(pnl),
                "pnl_pct": str(pnl_pct) if pnl_pct is not None else None,
                "close_reason": p.exit_reason,
                "duration_hours": round(duration_hours, 2),
            }
        )

    pnls = trade_pnls(real_closed)
    wins = sum(1 for x in pnls if x > 0)
    losses = sum(1 for x in pnls if x < 0)
    breakeven = sum(1 for x in pnls if x == 0)

    demo_comparison_rows = []
    for p in all_positions:
        demo_row = repo.get_demo_execution(p.position_id)
        if demo_row is None or demo_row["phase"] != "CLOSED":
            continue
        entry_divergence = (
            Decimal(demo_row["exchange_fill_entry"]) - p.simulated_fill_entry
            if demo_row["exchange_fill_entry"]
            else None
        )
        exit_divergence = (
            Decimal(demo_row["exchange_fill_exit"]) - (p.simulated_fill_exit or Decimal("0"))
            if demo_row["exchange_fill_exit"] and p.simulated_fill_exit is not None
            else None
        )
        demo_comparison_rows.append(
            {
                "position_id": p.position_id,
                "instrument": p.instrument,
                "paper_exit_reason": p.exit_reason,
                "demo_exit_reason": demo_row["exit_reason"],
                "paper_fill_entry": str(p.simulated_fill_entry),
                "demo_fill_entry": demo_row["exchange_fill_entry"],
                "paper_fill_exit": str(p.simulated_fill_exit) if p.simulated_fill_exit else None,
                "demo_fill_exit": demo_row["exchange_fill_exit"],
                "entry_divergence_usdt": str(entry_divergence) if entry_divergence is not None else None,
                "exit_divergence_usdt": str(exit_divergence) if exit_divergence is not None else None,
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "candidates": candidate_counts,
        "positions_opened_real": len(real_positions),
        "positions_blocked_by_exposure": len(blocked_positions),
        "open_positions": open_position_rows,
        "closed_trades": closed_position_rows,
        "trade_stats": {
            "closed_trade_count": len(pnls),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": str(compute_win_rate(pnls)) if compute_win_rate(pnls) is not None else None,
            "total_pnl_usdt": str(compute_cumulative_pnl(pnls)),
            "profit_factor": (
                str(compute_profit_factor(pnls))
                if compute_profit_factor(pnls) is not None
                else None
            ),
            "expectancy_usdt": (
                str(compute_expectancy(pnls)) if compute_expectancy(pnls) is not None else None
            ),
            "max_drawdown_usdt": (
                str(compute_drawdown(real_closed))
                if compute_drawdown(real_closed) is not None
                else None
            ),
        },
        "ai_cost": _sum_ai_cost_from_logs(log_glob),
        "demo_comparison": demo_comparison_rows,
    }


def main() -> None:
    import sys

    settings = get_settings()
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    connector = BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url,
        timeout_seconds=10.0,
        max_retries=settings.pipeline.bingx_max_retries,
        requests_per_second=settings.pipeline.bingx_requests_per_second,
        cache_ttl_seconds=settings.pipeline.bingx_cache_ttl_seconds,
    )
    log_glob = sys.argv[1] if len(sys.argv) > 1 else "logs/paper_run_*.log"
    report = build_report(repo, connector, log_glob)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
