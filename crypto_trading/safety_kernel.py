"""Deterministic Safety Kernel (P0, 2026-09-28).

Sits between every decision layer and the exchange:

    GODFATHER -> Guardian Authority -> SAFETY KERNEL -> exchange -> verification

Pure functions, no I/O, no AI. Two entry points:

- `size_entry` - before a LIVE entry: the largest quantity, never above the
  caller's own, whose WORST-CASE loss (stop distance + stop slippage +
  round-trip fees, at a conservatively high entry estimate) fits the
  per-trade, per-group, portfolio and notional budgets of CURRENT equity,
  and a stop far enough from the estimated liquidation price.
  Result: APPROVE (unchanged), REDUCE (smaller) or REJECT (nothing).
- `check_stop_move` - before any stop replacement (Profit Protection,
  Guardian Authority): a LONG stop may only move up, and never to within the
  liquidation buffer.

The kernel can only make a trade smaller or refuse it. It never raises
leverage, size, a stop distance or a limit, and has no exceptions for
"high confidence". Limits: config/safety_kernel.yaml (SafetyKernelConfig).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from functools import lru_cache

from crypto_trading.config.loader import SafetyKernelConfig

_ZERO = Decimal("0")


@dataclass(frozen=True)
class Exposure:
    symbol: str
    group: str
    risk_usdt: Decimal
    notional_usdt: Decimal


@dataclass(frozen=True)
class EntryDecision:
    action: str  # "APPROVE" | "REDUCE" | "REJECT"
    quantity: Decimal
    risk_usdt: Decimal
    reasons: list[str] = field(default_factory=list)
    binding_limits: list[str] = field(default_factory=list)
    equity: Decimal | None = None
    estimated_entry: Decimal | None = None
    liquidation_price: Decimal | None = None
    group: str | None = None
    per_trade_budget_usdt: Decimal | None = None
    portfolio_risk_before: Decimal = _ZERO
    portfolio_risk_after: Decimal = _ZERO
    group_risk_before: Decimal = _ZERO
    notional_before: Decimal = _ZERO
    notional_after: Decimal = _ZERO

    def as_log(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def group_for(symbol: str, limits: SafetyKernelConfig) -> str:
    if symbol in limits.symbol_groups:
        return limits.symbol_groups[symbol]
    for prefix, group in limits.group_prefixes.items():
        if symbol.startswith(prefix):
            return group
    return limits.default_group


def worst_case_risk_usdt(
    quantity: Decimal, entry: Decimal, stop: Decimal, limits: SafetyKernelConfig
) -> Decimal:
    """Loss if the stop fills `stop_slippage_buffer_pct` below its price,
    plus round-trip fees. The price part is never negative (a stop above
    entry still leaves the cost part at risk)."""
    price_part = quantity * (entry - stop * (1 - limits.stop_slippage_buffer_pct))
    return max(price_part, _ZERO) + quantity * entry * limits.round_trip_fee_pct


def exposure_for_open_position(
    symbol: str, quantity: Decimal, entry: Decimal, current_stop: Decimal,
    limits: SafetyKernelConfig,
) -> Exposure:
    return Exposure(
        symbol, group_for(symbol, limits),
        worst_case_risk_usdt(quantity, entry, current_stop, limits), quantity * entry,
    )


def estimated_liquidation_price(entry: Decimal, leverage: int, mmr: Decimal) -> Decimal:
    """Isolated LONG: entry x (1 - 1/leverage + maintenance margin rate)."""
    return entry * (1 - Decimal(1) / Decimal(leverage) + mmr)


def _liquidation_too_close(
    entry: Decimal, stop: Decimal, leverage: int, limits: SafetyKernelConfig
) -> tuple[bool, Decimal]:
    liquidation = estimated_liquidation_price(entry, leverage, limits.maintenance_margin_rate)
    return (stop - liquidation) / entry < limits.min_liquidation_buffer_pct, liquidation


def _floor(quantity: Decimal, precision: int) -> Decimal:
    if quantity <= 0:
        return _ZERO
    return quantity.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def size_entry(
    *,
    symbol: str,
    equity: Decimal | None,
    last_price: Decimal,
    stop_loss: Decimal,
    target: Decimal,
    leverage: int,
    base_quantity: Decimal,
    quantity_precision: int,
    min_notional: Decimal,
    open_exposures: list[Exposure],
    limits: SafetyKernelConfig,
) -> EntryDecision:
    group = group_for(symbol, limits)

    def reject(*reasons: str, **extra) -> EntryDecision:
        return EntryDecision("REJECT", _ZERO, _ZERO, list(reasons), group=group, equity=equity, **extra)

    if equity is None or equity <= 0:
        return reject("EQUITY_UNKNOWN")
    entry = last_price * (1 + limits.entry_price_buffer_pct)
    if stop_loss >= last_price:  # already at/through the stop
        return reject("STOP_NOT_BELOW_ENTRY", estimated_entry=entry)
    if target <= last_price:
        return reject("TARGET_NOT_ABOVE_ENTRY", estimated_entry=entry)
    too_close, liquidation = _liquidation_too_close(entry, stop_loss, leverage, limits)
    if too_close:
        return reject("LIQUIDATION_TOO_CLOSE", estimated_entry=entry, liquidation_price=liquidation)

    unit_risk = worst_case_risk_usdt(Decimal(1), entry, stop_loss, limits)
    portfolio_before = sum((e.risk_usdt for e in open_exposures), _ZERO)
    group_before = sum((e.risk_usdt for e in open_exposures if e.group == group), _ZERO)
    notional_before = sum((e.notional_usdt for e in open_exposures), _ZERO)
    per_trade = min(equity * limits.max_risk_per_trade_pct, limits.max_risk_per_trade_usdt)
    bounds = {
        "BASE_SIZE": base_quantity,
        "PER_TRADE_RISK_CAP": per_trade / unit_risk,
        "PORTFOLIO_RISK_CAP": max(equity * limits.max_portfolio_risk_pct - portfolio_before, _ZERO) / unit_risk,
        "GROUP_RISK_CAP": max(equity * limits.max_group_risk_pct - group_before, _ZERO) / unit_risk,
        "TOTAL_NOTIONAL_CAP": max(equity * limits.max_total_notional_multiple - notional_before, _ZERO) / entry,
    }
    raw = min(bounds.values())
    quantity = _floor(raw, quantity_precision)
    binding = [name for name, bound in bounds.items() if name != "BASE_SIZE" and bound == raw]
    context = dict(
        estimated_entry=entry, liquidation_price=liquidation, per_trade_budget_usdt=per_trade,
        portfolio_risk_before=portfolio_before, group_risk_before=group_before,
        notional_before=notional_before,
    )
    if quantity <= 0:
        return reject(*(binding or ["NO_RISK_BUDGET"]), binding_limits=binding, **context)
    if quantity * entry < min_notional:
        return reject("BELOW_EXCHANGE_MINIMUM_AFTER_RISK_SIZING", binding_limits=binding, **context)
    risk = unit_risk * quantity
    return EntryDecision(
        "APPROVE" if quantity >= base_quantity else "REDUCE",
        quantity, risk, [], binding if quantity < base_quantity else [],
        equity=equity, group=group,
        portfolio_risk_after=portfolio_before + risk, notional_after=notional_before + quantity * entry,
        **context,
    )


@lru_cache(maxsize=1)
def configured_limits() -> SafetyKernelConfig:
    from crypto_trading.config.loader import get_settings

    return get_settings().safety


def _positive(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def live_stop_move_violations(
    live_execution: dict | None, old_stop: object, new_stop: object,
    limits: SafetyKernelConfig | None = None,
) -> list[str]:
    """check_stop_move for a real LIVE position, from its live_executions
    row (exchange entry fill and leverage). Unknown entry fails closed."""
    entry = _positive((live_execution or {}).get("exchange_fill_entry"))
    if entry is None:
        return ["ENTRY_UNKNOWN"]
    leverage = _positive((live_execution or {}).get("leverage")) or Decimal("10")
    return check_stop_move(
        entry, _positive(old_stop), _positive(new_stop), int(leverage),
        limits or configured_limits(),
    )


def check_stop_move(
    entry: Decimal, old_stop: Decimal | None, new_stop: Decimal | None, leverage: int,
    limits: SafetyKernelConfig,
) -> list[str]:
    """Empty list = allowed. A LONG stop may only move UP (never further from
    entry) and never to within the liquidation buffer."""
    if new_stop is None or new_stop <= 0:
        return ["INVALID_STOP"]
    if old_stop is not None and new_stop < old_stop:
        return ["STOP_LOOSENING_FORBIDDEN"]
    too_close, _ = _liquidation_too_close(entry, new_stop, leverage, limits)
    return ["LIQUIDATION_TOO_CLOSE"] if too_close else []
