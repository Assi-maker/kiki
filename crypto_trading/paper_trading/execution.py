from __future__ import annotations

from decimal import Decimal
from typing import Literal

from crypto_trading.schemas.trade import Direction, Position

FILL_MODEL_VERSION = "v1"

_FUNDING_PERIOD_HOURS = Decimal("8")


def compute_fill_price(
    reference_price: Decimal,
    direction: Direction,
    spread_pct: Decimal,
    slippage_pct: Decimal,
    side: Literal["entry", "exit"],
) -> Decimal:
    """SPEC §11: simulated_fill_price = theoretical price justerat för ett
    konfigurerbart spread+slippage-antagande. Alltid till traderns nackdel
    (aldrig ett gynnsamt antagande) - LONG-entry/SHORT-exit betalar mer,
    LONG-exit/SHORT-entry får mindre."""
    adjustment_pct = spread_pct + slippage_pct
    worse_direction = {
        ("LONG", "entry"): 1,
        ("LONG", "exit"): -1,
        ("SHORT", "entry"): -1,
        ("SHORT", "exit"): 1,
    }[(direction, side)]
    return reference_price * (1 + worse_direction * adjustment_pct)


def compute_fees(size: Decimal, fee_pct: Decimal) -> Decimal:
    """Fees räknas på notional (size är redan ett USDT-notional-belopp från
    position_sizing.py), inte på fill_price*size."""
    return size * fee_pct


def compute_funding(size: Decimal, funding_rate: Decimal, hold_hours: Decimal) -> Decimal:
    """Medveten förenkling: en enda funding rate-sampling (vid positionens
    öppning) multiplicerad med antal HELA 8h-funding-perioder som passerat -
    inte en tidsserie av funding-observationer under hela hålltiden. BingX
    debiterar funding vid fasta 8h-tidpunkter, inte prorata."""
    whole_periods = int(hold_hours // _FUNDING_PERIOD_HOURS)
    return size * funding_rate * whole_periods


def compute_pnl(position: Position) -> Decimal:
    """Fas 6 (Telegram CLOSED-notis, SPEC §12): result = notional (size) *
    prisavkastning, minus fees/funding - LONG-only (samma antagande som
    position_opening.py/position_closing.py). Beräknas rent, ephemeralt för
    notisformatering - lagras ALDRIG som ett eget Position-fält (undviker
    en andra sanning/schemaändring för ett värde som alltid kan härledas
    från redan persisterade fält)."""
    price_return = (position.simulated_fill_exit - position.simulated_fill_entry) / (
        position.simulated_fill_entry
    )
    gross_pnl = position.size * price_return
    return gross_pnl - position.fees - position.funding
