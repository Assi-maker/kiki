from __future__ import annotations

from decimal import Decimal


def unrealized_profit_pct(avg_price: Decimal, mark_price: Decimal) -> Decimal:
    """LONG-only (this codebase's sole supported direction, see
    paper_trading/position_opening.py::_DIRECTION). avg_price is the
    real exchange fill entry (get_position()'s own 'avgPrice' field -
    exchange state, never this module's locally-stored data), mark_price
    is the real exchange mark price (get_position()'s 'markPrice' -
    consistent with this project's SL/TP orders, which already trigger
    on workingType=MARK_PRICE, not last-traded price)."""
    return (mark_price - avg_price) / avg_price
