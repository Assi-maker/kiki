from __future__ import annotations

from decimal import Decimal

from crypto_trading.detective.context import signal_type_for_candidate
from crypto_trading.performance.metrics import (
    compute_expectancy,
    compute_profit_factor,
    compute_win_rate,
    trade_pnls,
)
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.trade import Position


def _optional_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _is_blocked_by_exposure(position: Position) -> bool:
    """Samma definition/semantik som performance/paper_track_report.py::
    _is_blocked_by_exposure() - en position vars `size` trycktes till 0 av
    max_total_exposure_pct-taket (paper_trading/position_sizing.py::
    compute_position_size()) representerar noll verklig marknadsexponering
    och ska aldrig räknas som en break-even-trade i Detectives statistik."""
    return position.size == Decimal("0")


def compute_batch_win_loss_counts(positions: list[Position]) -> dict:
    """Samma win/loss/breakeven-definition som performance/paper_track_
    report.py redan använder (p>0/p<0/p==0 på compute_pnl()), återanvänd
    via trade_pnls() - ingen egen PnL-formel. Nollstorlekspositioner
    (blockerade av max_total_exposure_pct, se _is_blocked_by_exposure())
    exkluderas innan pnls/counts beräknas - annars skulle de felaktigt
    räknas som break-even (2026-09-03, explicit användarkrav)."""
    closed = [p for p in positions if p.status == "CLOSED"]
    real_positions = [p for p in closed if not _is_blocked_by_exposure(p)]
    pnls = trade_pnls(real_positions)
    return {
        "win_count": sum(1 for p in pnls if p > 0),
        "loss_count": sum(1 for p in pnls if p < 0),
        "breakeven_count": sum(1 for p in pnls if p == 0),
        "blocked_by_exposure_count": len(closed) - len(real_positions),
    }


def compute_breakdown_by_signal_type(
    positions: list[Position], candidates_by_id: dict[str, Candidate]
) -> dict[str, dict]:
    """Grupperar stängda positioner på signaltyp (context.py::
    signal_type_for_candidate()) och återanvänder performance/metrics.py:s
    redan testade PnL-/win-rate-/profit-factor-/expectancy-formler rakt av
    - samma mönster som performance/metrics.py::compute_breakdown_by_
    instrument(), bara en annan grupperingsnyckel. En position vars
    candidate saknas i `candidates_by_id` (t.ex. en korrupt rad Detective
    redan filtrerat bort - se detective/batch.py) grupperas som "unknown",
    aldrig utelämnad tyst. Nollstorlekspositioner (_is_blocked_by_exposure())
    exkluderas innan grupperingen, av samma skäl som i
    compute_batch_win_loss_counts()."""
    closed = [p for p in positions if p.status == "CLOSED" and not _is_blocked_by_exposure(p)]
    grouped: dict[str, list[Position]] = {}
    for position in closed:
        signal_type = signal_type_for_candidate(candidates_by_id.get(position.candidate_id))
        grouped.setdefault(signal_type, []).append(position)

    result = {}
    for signal_type, group in grouped.items():
        pnls = trade_pnls(group)
        result[signal_type] = {
            "trade_count": len(pnls),
            "win_rate": _optional_str(compute_win_rate(pnls)),
            "profit_factor": _optional_str(compute_profit_factor(pnls)),
            "expectancy_usdt": _optional_str(compute_expectancy(pnls)),
        }
    return result
