"""R: results in units of the risk the trade PLANNED to take.

USDT answers "how much capital moved"; it cannot compare a 500-USDT and a
2 500-USDT trade, and the real book's sizes span 84-2 545 USDT, so a
USDT pattern partly measures which sizing regime was in force. R answers
"how good was the trade relative to what it risked":

    initial risk %  = |entry - initial SL| / entry
    R (gross)       = direction x (exit - entry) / entry / initial risk %
    R (net)         = P/L after costs / (size x initial risk %)

**The ORIGINAL stop, never a later one.** `positions.stop_loss` can be
tightened afterwards (Guardian Authority writes it through
`tighten_position_stop_loss`); measuring against the tightened stop would
make every protected trade look like a large multiple of a tiny risk.
The original is recovered from the earliest Guardian Authority decision
that moved it (`old_sl`) when one exists, else the position's own stop is
the original. When the original cannot be known, or makes no sense (on
the wrong side of the entry, non-positive), R is UNAVAILABLE - not a
guess.

Partial closes: the paper book has none; realised P/L over the initial
size is the whole trade either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")


@dataclass(frozen=True)
class RMultiple:
    status: str  # AVAILABLE | UNAVAILABLE
    reason: str | None
    initial_stop_loss: Decimal | None = None
    initial_risk_pct: Decimal | None = None
    r_gross: Decimal | None = None
    r_net: Decimal | None = None
    return_pct: Decimal | None = None
    risk_usdt: Decimal | None = None

    def as_dict(self) -> dict:
        return {k: (None if v is None else str(v)) for k, v in self.__dict__.items()}


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def initial_stop_loss(
    position: Position, authority_decisions: list[dict]
) -> tuple[Decimal | None, str | None]:
    """The stop the trade was OPENED with. A decision that tightened the
    stop without recording the old one makes the original unknowable."""
    moved = sorted(
        (d for d in authority_decisions
         if d.get("decision_type") == "TIGHTEN_SL" and d.get("intervention_applied")),
        key=lambda d: str(d.get("decided_at")),
    )
    if not moved:
        return position.stop_loss, None
    original = _decimal(moved[0].get("old_sl"))
    if original is None:
        return None, "INITIAL_SL_OVERWRITTEN_WITHOUT_RECORD"
    return original, None


def _sign(direction: str) -> Decimal:
    return Decimal("-1") if direction == "SHORT" else Decimal("1")


def compute_r(
    position: Position,
    initial_sl: Decimal | None,
    pnl_usdt: Decimal | None,
    exit_price: Decimal | None = None,
    entry_price: Decimal | None = None,
    fee_pct: Decimal | None = None,
) -> RMultiple:
    """R for one trade. `entry_price`/`exit_price` default to the paper
    fills; pass exchange fills when the outcome comes from the exchange
    (then net R uses the configured `fee_pct`, the same fee model the
    paper book is charged with).

    The DENOMINATOR is always the PLANNED risk: the distance from the
    planned (paper) entry to the initial stop. Measuring it from an
    exchange fill instead would reward a late fill: on 2026-09-25 a LIVE
    fill 5% above plan left a 0.9% stop distance and turned a +16.6%
    trade into +18 R. The numerator is the real return from whichever
    entry actually filled."""
    if initial_sl is None:
        return RMultiple("UNAVAILABLE", "MISSING_INITIAL_SL")
    planned_entry = position.simulated_fill_entry
    entry = entry_price if entry_price is not None else planned_entry
    exit_ = exit_price if exit_price is not None else position.simulated_fill_exit
    if (
        entry is None or entry <= _ZERO or planned_entry is None or planned_entry <= _ZERO
        or initial_sl <= _ZERO
    ):
        return RMultiple("UNAVAILABLE", "INVALID_PRICES", initial_stop_loss=initial_sl)
    sign = _sign(position.direction)
    risk_pct = sign * (planned_entry - initial_sl) / planned_entry
    if risk_pct <= _ZERO:
        return RMultiple("UNAVAILABLE", "INVALID_SL_SIDE", initial_stop_loss=initial_sl)
    if exit_ is None:
        return RMultiple("UNAVAILABLE", "MISSING_EXIT", initial_stop_loss=initial_sl,
                         initial_risk_pct=risk_pct)
    return_pct = sign * (exit_ - entry) / entry
    r_gross = return_pct / risk_pct
    risk_usdt = position.size * risk_pct
    if pnl_usdt is not None and risk_usdt > _ZERO:
        r_net = pnl_usdt / risk_usdt
    elif fee_pct is not None:
        r_net = (return_pct - fee_pct) / risk_pct
    else:
        r_net = None
    return RMultiple(
        status="AVAILABLE" if r_net is not None else "UNAVAILABLE",
        reason=None if r_net is not None else "NET_RESULT_UNKNOWN",
        initial_stop_loss=initial_sl,
        initial_risk_pct=risk_pct,
        r_gross=r_gross,
        r_net=r_net,
        return_pct=return_pct,
        risk_usdt=risk_usdt if risk_usdt > _ZERO else None,
    )


def live_usdt_outcome(
    live_execution: dict | None, r: RMultiple | None, direction: str, fee_pct: Decimal | None
) -> dict | None:
    """USDT for a LIVE trade at its OWN exchange size, never the paper size.

    `RMultiple.risk_usdt` is `position.size x risk %` - the PAPER size,
    which is what the paper counterfactual deltas it divides are measured
    in, so it must stay that way. A LIVE execution has its own, fixed size
    (margin x leverage, `live_execution.yaml`; 100 USDT notional before
    2026-09-26, 1 000 USDT after), recorded per row - so a book that
    spans the change mixes both, and only the row's own quantity is
    right. Edge comparisons stay in R / %; this is reporting only.

    Real fees/funding are taken when the row has them; LIVE closes do not
    record them today, so fees fall back to the same `fee_pct` model R's
    net already uses (flagged MODELLED) and unknown funding counts as 0
    (flagged UNKNOWN)."""
    if live_execution is None:
        return None
    qty = _decimal(live_execution.get("entry_quantity"))
    entry = _decimal(live_execution.get("exchange_fill_entry"))
    exit_ = _decimal(live_execution.get("exchange_fill_exit"))
    notional = (
        qty * entry if qty is not None and entry is not None and qty > _ZERO and entry > _ZERO
        else _decimal(live_execution.get("notional_usdt"))
    )
    out: dict = {
        "margin_usdt": _decimal(live_execution.get("margin_usdt")),
        "leverage": _decimal(live_execution.get("leverage")),
        "entry_quantity": qty,
        "notional_usdt": notional,
        "planned_risk_usdt": (
            notional * r.initial_risk_pct
            if notional is not None and r is not None and r.initial_risk_pct is not None
            else None
        ),
        "gross_pnl_usdt": None,
        "fees_usdt": None,
        "fees_source": None,
        "funding_usdt": None,
        "funding_source": None,
        "net_pnl_usdt": None,
    }
    if qty is None or qty <= _ZERO or entry is None or exit_ is None or notional is None:
        return out
    gross = _sign(direction) * (exit_ - entry) * qty
    fees = _decimal(live_execution.get("realized_fees_usdt"))
    if fees is not None:
        out["fees_source"] = "EXCHANGE"
    elif fee_pct is not None:
        fees, out["fees_source"] = notional * fee_pct, "MODELLED"
    funding = _decimal(live_execution.get("realized_funding_usdt"))
    out["funding_source"] = "EXCHANGE" if funding is not None else "UNKNOWN"
    out.update(
        gross_pnl_usdt=gross,
        fees_usdt=fees,
        funding_usdt=funding,
        net_pnl_usdt=(
            gross - fees + (funding if funding is not None else _ZERO)
            if fees is not None else None
        ),
    )
    return out
