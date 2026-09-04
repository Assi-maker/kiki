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


def compute_batch_win_loss_counts(positions: list[Position]) -> dict:
    """Samma win/loss/breakeven-definition som performance/paper_track_
    report.py redan använder (p>0/p<0/p==0 på compute_pnl()), återanvänd
    via trade_pnls() - ingen egen PnL-formel."""
    pnls = trade_pnls(positions)
    return {
        "win_count": sum(1 for p in pnls if p > 0),
        "loss_count": sum(1 for p in pnls if p < 0),
        "breakeven_count": sum(1 for p in pnls if p == 0),
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
    aldrig utelämnad tyst."""
    closed = [p for p in positions if p.status == "CLOSED"]
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
