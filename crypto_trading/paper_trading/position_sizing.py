from __future__ import annotations

from decimal import Decimal


def compute_position_size(
    entry_price: Decimal,
    stop_loss_price: Decimal,
    capital: Decimal,
    risk_per_trade_pct: Decimal,
    open_positions_notional: Decimal,
    max_total_exposure_pct: Decimal,
) -> Decimal:
    """Regelbaserad storlek (SPEC §11): förlust vid stop = risk_per_trade_pct
    av kapitalet, klippt av max_total_exposure_pct:s återstående kapacitet.
    Ren funktion, inga sidoeffekter."""
    stop_distance_pct = abs(entry_price - stop_loss_price) / entry_price
    if stop_distance_pct <= 0:
        return Decimal("0")  # fail-closed: odefinierat stop-avstånd, aldrig en gissad storlek

    risk_amount = capital * risk_per_trade_pct
    raw_size = risk_amount / stop_distance_pct

    max_exposure = capital * max_total_exposure_pct
    available_exposure = max(Decimal("0"), max_exposure - open_positions_notional)

    return min(raw_size, available_exposure)
