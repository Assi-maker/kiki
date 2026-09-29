"""Pre-AI risk feasibility - SHADOW ONLY (2026-09-29).

Question: "if this candidate later got the normal full 100 USDT x 10
position, is there any realistic chance it fits the CURRENT Safety Kernel?"

Asked BEFORE the AI chain, so the Risk Agent's stop is not known yet. The
answer is therefore never an exact risk number:

- method "bound" (the only one that may say INFEASIBLE): the stop distance
  is bounded by [lower, upper] from the 30m ATR - the same resolution
  discovery uses - with quantiles of stop/ATR30 FROZEN on TRAIN, and the
  lower bound never below the tightest stop the Risk Agent has ever set in
  TRAIN. INFEASIBLE only if even the tightest plausible stop cannot fit (or
  a structural rule already blocks: max positions, symbol active);
  FEASIBLE only if even the widest plausible stop fits; else UNKNOWN.
- method "estimate" (logged next to it, never authoritative): a point
  estimate (TRAIN median ratio x ATR30), clearly labelled as an estimate.

This module decides nothing. It never blocks a candidate, changes the AI
flow, the Gate, the Safety Kernel, sizing or execution; it imports only the
Safety Kernel's pure arithmetic. UNKNOWN is never a reject.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from crypto_trading.config.loader import SafetyKernelConfig
from crypto_trading.safety_kernel import (
    Exposure,
    _liquidation_too_close,
    group_for,
    worst_case_risk_usdt,
)

CALIBRATION_PATH = Path(__file__).resolve().parents[1] / "config" / "pre_ai_feasibility.json"
FEASIBLE, INFEASIBLE, UNKNOWN = "true", "false", "unknown"


@dataclass(frozen=True)
class Calibration:
    timeframe: str
    atr_bars: int
    ratio_lower: float        # q01 of stop_pct / atr30_pct on TRAIN
    ratio_median: float       # q50 - point estimate only
    ratio_upper: float        # q99
    stop_floor_pct: float     # tightest Risk Agent stop seen in TRAIN
    frozen_on: str

    @classmethod
    def load(cls, path: Path = CALIBRATION_PATH) -> Calibration:
        return cls(**json.loads(path.read_text(encoding="utf-8"))["calibration"])


def atr_pct(bars) -> float | None:
    """Mean high-low range in % of close over CLOSED bars (caller passes
    only bars that closed before the signal)."""
    if not bars:
        return None
    return sum((float(b.h) - float(b.l)) / float(b.c) for b in bars) / len(bars) * 100


def assess(
    *, symbol: str, price: float, atr30_pct: float | None, equity: Decimal | None,
    open_exposures: list[Exposure], open_symbols: set[str], max_positions: int,
    margin_usdt: Decimal, leverage: int, limits: SafetyKernelConfig, calib: Calibration,
) -> dict:
    base = {"symbol": symbol, "price": price, "atr30_pct": atr30_pct,
            "equity": str(equity) if equity is not None else None, "method": "bound"}
    if len(open_exposures) >= max_positions:
        return {**base, "pre_ai_feasible": INFEASIBLE, "reason": "MAX_POSITIONS"}
    if symbol in open_symbols:
        return {**base, "pre_ai_feasible": INFEASIBLE, "reason": "ONE_PER_SYMBOL"}
    if equity is None or equity <= 0 or not price or price <= 0:
        return {**base, "pre_ai_feasible": UNKNOWN, "reason": "EQUITY_OR_PRICE_UNKNOWN"}
    last = Decimal(str(price))
    quantity = (margin_usdt * leverage / last).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    entry = last * (1 + limits.entry_price_buffer_pct)
    group = group_for(symbol, limits)
    port_room = equity * limits.max_portfolio_risk_pct - sum((e.risk_usdt for e in open_exposures), Decimal(0))
    grp_room = equity * limits.max_group_risk_pct - sum(
        (e.risk_usdt for e in open_exposures if e.group == group), Decimal(0))
    budget = min(port_room, grp_room)
    out = {**base, "estimated_notional": float(quantity * last), "group": group,
           "available_portfolio_budget": float(port_room), "available_group_budget": float(grp_room)}
    if atr30_pct is None or atr30_pct <= 0:
        return {**out, "pre_ai_feasible": UNKNOWN, "reason": "ATR_UNKNOWN"}

    def risk_at(stop_pct: float) -> Decimal:
        stop = last * (1 - Decimal(str(stop_pct)) / 100)
        return worst_case_risk_usdt(quantity, entry, stop, limits)

    lower = max(calib.ratio_lower * atr30_pct, calib.stop_floor_pct)
    upper = max(calib.ratio_upper * atr30_pct, lower)
    point = max(calib.ratio_median * atr30_pct, calib.stop_floor_pct)
    risk_lo, risk_hi, risk_pt = risk_at(lower), risk_at(upper), risk_at(point)
    out.update({
        "estimated_stop_pct_interval": [round(lower, 3), round(upper, 3)],
        "estimated_worst_case_risk_interval": [float(risk_lo), float(risk_hi)],
        "shadow_estimate": {"method": "estimate", "stop_pct": round(point, 3), "worst_case_risk": float(risk_pt),
                            "fits": bool(risk_pt <= budget)},
    })
    liq_lo, _ = _liquidation_too_close(entry, last * (1 - Decimal(str(lower)) / 100), leverage, limits)
    if liq_lo:
        return {**out, "pre_ai_feasible": INFEASIBLE, "reason": "LIQUIDATION_EVEN_AT_TIGHTEST_STOP"}
    if risk_lo > budget:
        reason = "GROUP_BUDGET" if grp_room <= port_room else "PORTFOLIO_BUDGET"
        return {**out, "pre_ai_feasible": INFEASIBLE, "reason": f"{reason}_EVEN_AT_TIGHTEST_STOP"}
    liq_hi, _ = _liquidation_too_close(entry, last * (1 - Decimal(str(upper)) / 100), leverage, limits)
    if risk_hi <= budget and not liq_hi:
        return {**out, "pre_ai_feasible": FEASIBLE, "reason": "FITS_EVEN_AT_WIDEST_STOP"}
    return {**out, "pre_ai_feasible": UNKNOWN, "reason": "DEPENDS_ON_RISK_AGENT_STOP"}
