from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from crypto_trading.schemas.trade import Direction, Position

if TYPE_CHECKING:
    from crypto_trading.storage.repository import Repository

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


def has_paper_exit_data(position: Position) -> bool:
    """False for a position closed only by the LIVE exit mirror
    (`repo.close_position_for_live_exit`), which never writes PAPER's
    simulated_fill_exit/fees/funding - `compute_pnl` cannot be evaluated."""
    return (
        position.simulated_fill_exit is not None
        and position.fees is not None
        and position.funding is not None
    )


def compute_pnl_or_none(position: Position) -> Decimal | None:
    """`compute_pnl`, or None (unknown - never an invented value) when the
    position has no PAPER exit data. Same formula, no new one."""
    return compute_pnl(position) if has_paper_exit_data(position) else None


# ---------------------------------------------------------------------------
# Realized P/L with its provenance (2026-09-26). A position closed only by
# the LIVE exit mirror (`repo.close_position_for_live_exit`) has no PAPER
# exit data, so `compute_pnl` cannot be evaluated on it. Its outcome may
# still be known - from the LIVE execution's own verified exchange exit - or
# it may not; this is the one place that decides which, so no caller guesses.
# ---------------------------------------------------------------------------

# How a LIVE exit price was obtained (`live_executions.exit_fill_source`).
# Only the exchange's own fills are a verified exit; a ticker price at
# reconciliation time is not.
_VERIFIED_LIVE_FILL_SOURCES = frozenset({"EXCHANGE_ORDER", "MARKET_CLOSE"})
# Rows closed before exit_fill_source existed (NULL): these two exit reasons
# are written ONLY by live_execution.py's own market-close paths, from the
# exchange's response to that close - so their price is a fill by
# construction. stop_loss/target rows may have come from the ticker
# fallback and stay unverifiable.
_MARKET_CLOSE_EXIT_REASONS = frozenset({"TIME_LIMIT", "GUARDIAN_EXIT"})


@dataclass(frozen=True)
class RealizedPnl:
    """`pnl_usdt` is at the SOURCE's own size - the paper position's for
    PAPER, the exchange execution's for LIVE; the two are never mixed.
    `net_return` (P/L over that same notional) is what makes them
    comparable. UNVERIFIABLE carries no number at all, never an invented
    one."""

    status: Literal["VERIFIED", "UNVERIFIABLE"]
    source: Literal["PAPER", "LIVE"] | None
    pnl_usdt: Decimal | None
    net_return: Decimal | None
    reason: str | None = None
    fees_source: Literal["PAPER_MODEL", "EXCHANGE", "MODELLED"] | None = None

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"

    def paper_size_equivalent(self, position: Position) -> Decimal | None:
        """The outcome in the paper position's own units, for comparing
        against other paper-size values (Guardian observations'
        unrealized_pnl). PAPER is returned exactly as `compute_pnl` gives
        it; LIVE is its real net return applied to the paper size."""
        if not self.verified:
            return None
        if self.source == "PAPER":
            return self.pnl_usdt
        return self.net_return * position.size


def _positive_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _unverifiable(reason: str) -> RealizedPnl:
    return RealizedPnl("UNVERIFIABLE", None, None, None, reason)


def resolve_realized_pnl(
    position: Position, live_execution: dict | None, fee_pct: Decimal
) -> RealizedPnl:
    """PAPER when the paper exit exists (unchanged `compute_pnl`), else the
    LIVE execution's verified exchange exit, else UNVERIFIABLE.

    LIVE: `direction x (exit - entry) x filled quantity`, minus fees -
    the exchange's recorded fees when present, otherwise the same
    `fee_pct x notional` model the paper book is charged (MODELLED) - and
    minus recorded funding (paper sign convention: positive = cost).
    LIVE closes do not record funding today; it is then 0, as it is for
    any paper trade held under one 8h funding period."""
    if position.status != "CLOSED":
        return _unverifiable("NOT_CLOSED")
    if has_paper_exit_data(position):
        pnl = compute_pnl(position)
        return RealizedPnl(
            "VERIFIED", "PAPER", pnl,
            pnl / position.size if position.size != 0 else None,
            fees_source="PAPER_MODEL",
        )
    if live_execution is None:
        return _unverifiable("NO_PAPER_EXIT_AND_NO_LIVE_EXECUTION")
    if live_execution.get("phase") != "CLOSED":
        return _unverifiable("LIVE_EXECUTION_NOT_CLOSED")
    fill_source = live_execution.get("exit_fill_source")
    if fill_source is None and live_execution.get("exit_reason") in _MARKET_CLOSE_EXIT_REASONS:
        fill_source = "MARKET_CLOSE"
    if fill_source not in _VERIFIED_LIVE_FILL_SOURCES:
        return _unverifiable(
            "LIVE_EXIT_FROM_TICKER" if fill_source == "TICKER" else "LIVE_EXIT_SOURCE_UNRECORDED"
        )
    quantity = _positive_decimal(live_execution.get("entry_quantity"))
    entry = _positive_decimal(live_execution.get("exchange_fill_entry"))
    exit_ = _positive_decimal(live_execution.get("exchange_fill_exit"))
    if quantity is None or entry is None or exit_ is None:
        return _unverifiable("LIVE_FILL_DATA_MISSING")
    notional = quantity * entry
    sign = Decimal("-1") if position.direction == "SHORT" else Decimal("1")
    gross = sign * (exit_ - entry) * quantity
    recorded_fees = live_execution.get("realized_fees_usdt")
    if recorded_fees is not None:
        fees, fees_source = Decimal(str(recorded_fees)), "EXCHANGE"
    else:
        fees, fees_source = compute_fees(notional, fee_pct), "MODELLED"
    recorded_funding = live_execution.get("realized_funding_usdt")
    funding = Decimal(str(recorded_funding)) if recorded_funding is not None else Decimal("0")
    pnl = gross - fees - funding
    return RealizedPnl("VERIFIED", "LIVE", pnl, pnl / notional, fees_source=fees_source)


@lru_cache(maxsize=1)
def _configured_fee_pct() -> Decimal:
    from crypto_trading.config.loader import get_settings

    return get_settings().risk_limits.fee_pct


def realized_pnl_for(
    repo: Repository, position: Position, fee_pct: Decimal | None = None
) -> RealizedPnl:
    """`resolve_realized_pnl` with the LIVE row looked up. `fee_pct`
    defaults to the paper book's own configured fee rate."""
    return resolve_realized_pnl(
        position,
        repo.get_live_execution(position.position_id),
        fee_pct if fee_pct is not None else _configured_fee_pct(),
    )
