"""The one cost model every GODFATHER simulation goes through.

Requirement: "a hypothetical improvement that only works before costs is
not an improvement". Every simulated exit - counterfactual, stop policy,
thesis policy - is priced here, and here only, through the SAME
`compute_fill_price` / `compute_fees` / `compute_funding` the live paper
engine uses (`paper_trading/execution.py`) with the configured
spread/slippage/fee. A policy that trades more therefore pays more, and
cannot look good by turning over the book for free.
"""

from __future__ import annotations

from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.paper_trading.execution import (
    compute_fees,
    compute_fill_price,
    compute_funding,
)
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")


def direction_sign(position: Position) -> Decimal:
    return Decimal("-1") if position.direction == "SHORT" else Decimal("1")


def _hold_hours(minutes: float) -> Decimal:
    return Decimal(str(max(0.0, minutes))) / Decimal("60")


def simulate_exit_pnl(
    position: Position,
    exit_reference_price: Decimal,
    minutes_in_trade: float,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
    size: Decimal | None = None,
) -> Decimal:
    """P/L of closing `size` (default: the whole position) at
    `exit_reference_price`, net of spread, slippage, fees and funding.

    Not a new formula: the gross term is the same `size * price_return`
    as `execution.py::compute_pnl`.
    """
    notional = position.size if size is None else size
    fill = compute_fill_price(
        exit_reference_price,
        position.direction,
        risk_limits.spread_pct,
        risk_limits.slippage_pct,
        "exit",
    )
    entry = position.simulated_fill_entry
    if entry == _ZERO:
        return _ZERO
    price_return = (fill - entry) / entry
    gross = notional * price_return * direction_sign(position)
    fees = compute_fees(notional, risk_limits.fee_pct)
    funding = compute_funding(notional, funding_rate, _hold_hours(minutes_in_trade))
    return gross - fees - funding


def implied_funding_rate(position: Position) -> Decimal:
    """Back out the per-period funding rate the real close actually
    charged, so a simulated exit is charged on the same basis rather than
    on an assumed zero. Falls back to 0 when the real position carries no
    funding data (a LIVE-mirrored close), which is the only honest choice
    available there."""
    if position.funding is None or position.size == _ZERO or position.closed_at is None:
        return _ZERO
    hold_hours = Decimal(
        str((position.closed_at - position.opened_at).total_seconds() / 3600)
    )
    periods = int(hold_hours // Decimal("8"))
    if periods <= 0:
        return _ZERO
    return position.funding / (position.size * periods)
