from __future__ import annotations

from decimal import Decimal

from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.trade import Position


def trade_pnls(positions: list[Position]) -> list[Decimal]:
    """PnL per stängd position, via paper_trading.execution.compute_pnl()
    - den enda PnL-källan i hela systemet (PLAN_CRYPTO_PHASE8.md, Global
    Constraints). Filtrerar bort allt som inte är status == 'CLOSED'
    internt, defensivt oavsett vad anroparen skickar in - compute_pnl()
    kräver simulated_fill_exit, som bara är satt på stängda positioner."""
    return [compute_pnl(p) for p in positions if p.status == "CLOSED"]


def compute_cumulative_pnl(pnls: list[Decimal]) -> Decimal:
    """sum(pnls), Decimal('0') för tom lista - en tom historik har
    verkligen noll kumulativ PnL, det är inte en gap-markering."""
    return sum(pnls, Decimal("0"))


def compute_win_rate(pnls: list[Decimal]) -> Decimal | None:
    """count(p > 0) / count(alla) - break-even (p == 0) räknas i nämnaren,
    aldrig i täljaren. None för tom lista (odefinierat, inte 0%)."""
    if not pnls:
        return None
    wins = sum(1 for p in pnls if p > 0)
    return Decimal(wins) / Decimal(len(pnls))


def compute_expectancy(pnls: list[Decimal]) -> Decimal | None:
    """Genomsnittlig PnL per stängd trade - explicit vald definition
    (PLAN_CRYPTO_PHASE8.md §0), inte vinstprocent x snittvinst-formeln.
    None för tom lista."""
    if not pnls:
        return None
    return sum(pnls, Decimal("0")) / Decimal(len(pnls))


def compute_profit_factor(pnls: list[Decimal]) -> Decimal | None:
    """sum(vinster) / abs(sum(förluster)). None om pnls är tom ELLER om det
    inte finns några förluster (division med noll - odefinierat, aldrig
    Infinity/fabricerat). 0 (giltigt tal) om det finns förluster men inga
    vinster."""
    if not pnls:
        return None
    wins = sum((p for p in pnls if p > 0), Decimal("0"))
    losses = sum((p for p in pnls if p < 0), Decimal("0"))
    if losses == 0:
        return None
    return wins / abs(losses)


def _closed_positions_sorted_by_closed_at(positions: list[Position]) -> list[Position]:
    """Filtrerar till status == 'CLOSED' och sorterar kronologiskt på
    closed_at - beräknat internt, litar aldrig på anroparens ordning."""
    closed = [p for p in positions if p.status == "CLOSED"]
    return sorted(closed, key=lambda p: p.closed_at)


def compute_drawdown(positions: list[Position]) -> Decimal | None:
    """Max peak-to-trough över en kronologisk kumulativ PnL-kurva. None om
    inga stängda positioner (odefinierat). 0 (giltigt) om det finns trades
    men aldrig en nedgång från den löpande toppen."""
    ordered = _closed_positions_sorted_by_closed_at(positions)
    if not ordered:
        return None
    running = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for position in ordered:
        running += compute_pnl(position)
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
    return max_drawdown


def compute_equity_curve(positions: list[Position]) -> list[dict]:
    """[{"closed_at": iso-sträng, "cumulative_pnl": str(Decimal)}, ...],
    kronologiskt sorterat internt. Tom lista om inga stängda positioner."""
    ordered = _closed_positions_sorted_by_closed_at(positions)
    running = Decimal("0")
    curve = []
    for position in ordered:
        running += compute_pnl(position)
        curve.append({"closed_at": position.closed_at.isoformat(), "cumulative_pnl": str(running)})
    return curve


def _optional_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _group_summary(positions: list[Position]) -> dict:
    """Delad byggsten för compute_breakdown_by_instrument/_by_direction -
    återanvänder trade_pnls/compute_cumulative_pnl/compute_win_rate rakt av,
    ingen egen PnL-/win-rate-formel."""
    pnls = trade_pnls(positions)
    return {
        "trade_count": len(pnls),
        "cumulative_pnl": str(compute_cumulative_pnl(pnls)),
        "win_rate": _optional_str(compute_win_rate(pnls)),
    }


def compute_breakdown_by_instrument(positions: list[Position]) -> dict[str, dict]:
    """{"BTCUSDT": {"trade_count": int, "cumulative_pnl": str, "win_rate":
    str|None}, ...} - en nyckel per instrument som faktiskt förekommer bland
    stängda positioner, aldrig en förutbestämd lista."""
    closed = [p for p in positions if p.status == "CLOSED"]
    instruments = sorted({p.instrument for p in closed})
    return {
        instrument: _group_summary([p for p in closed if p.instrument == instrument])
        for instrument in instruments
    }


def compute_breakdown_by_direction(positions: list[Position]) -> dict[str, dict]:
    """Samma delfält som compute_breakdown_by_instrument, grupperat på
    direction istället. Riktningsagnostisk - itererar över de riktningar
    som FAKTISKT finns i indatan, antar aldrig bara LONG (trots att
    produktionspipelinen idag bara producerar LONG-positioner,
    paper_trading/position_closing.py: _DIRECTION="LONG", oförändrad i
    denna fas)."""
    closed = [p for p in positions if p.status == "CLOSED"]
    directions = sorted({p.direction for p in closed})
    return {
        direction: _group_summary([p for p in closed if p.direction == direction])
        for direction in directions
    }
