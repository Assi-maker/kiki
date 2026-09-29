"""Counterfactual risk-policy replay (2026-09-29) - read-only, never LIVE.

Question: of the CONFIRMED opportunities, which would each risk policy have
opened, and what would that have done to expectancy, drawdown and
simultaneous risk?

Opportunity = a CONFIRMED candidate with the Risk Agent's own stop/target.
Decision time = the real wall-clock time of its last QA call (AI_CALL_MADE
event; the candidates' own gate timestamps carry the cycle-clock bug).
Entry = open of the first 1m bar at/after decision + 1 min (LIVE fills
~0.5 min after CONFIRMED). Exit = the LIVE rules: exchange SL/TP, Profit
Protection moves the stop to break-even at +1 %, 6 h time limit; costs =
0.10 % round trip + 0.15 % stop slippage.

Every policy decision at time t uses only what is known at t: open
positions, their CURRENT stop (break-even only once the bar that triggered
it has closed) and the equity realised so far. The Safety Kernel's own
functions do the risk arithmetic, so policy A is exactly the LIVE kernel.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from crypto_trading.config.loader import SafetyKernelConfig
from crypto_trading.safety_kernel import (
    exposure_for_open_position,
    group_for,
    size_entry,
    worst_case_risk_usdt,
)
from crypto_trading.shadow.evaluation import Bar

FEE_RT = 0.001
STOP_SLIP = 0.0015
PP_TRIGGER = 0.01          # Profit Protection: stop -> entry at +1 %
HORIZON = timedelta(hours=6)
MARGIN = Decimal("100")
LEVERAGE = 10
MAX_POSITIONS = 4


@dataclass
class Opportunity:
    candidate_id: str
    symbol: str
    signal_at: datetime
    decided_at: datetime
    stop: float
    target: float
    features: dict = field(default_factory=dict)


@dataclass
class Path:
    """Where one unit (1 coin) of the trade goes - size-independent."""
    entry_at: datetime
    entry: float
    exit_at: datetime
    exit: float
    reason: str               # TP | SL | BE | TIME
    be_at: datetime | None    # when the break-even stop became active
    mfe_pct: float
    mae_pct: float


def simulate_path(bars: list[Bar], opp: Opportunity) -> Path | None:
    after = [b for b in bars if b.t >= opp.decided_at + timedelta(minutes=1)]
    if not after:
        return None
    entry, start = after[0].o, after[0].t
    if not (opp.stop < entry < opp.target):
        return None  # the kernel would reject it structurally
    stop, reason, be_at = opp.stop, "SL", None
    hi = lo = entry
    for bar in after:
        if bar.t >= start + HORIZON:
            break
        hi, lo = max(hi, bar.h), min(lo, bar.l)
        if bar.l <= stop:
            return Path(start, entry, bar.t, stop * (1 - STOP_SLIP), reason, be_at,
                        (hi / entry - 1) * 100, (lo / entry - 1) * 100)
        if bar.h >= opp.target:
            return Path(start, entry, bar.t, opp.target, "TP", be_at,
                        (hi / entry - 1) * 100, (lo / entry - 1) * 100)
        if be_at is None and bar.h >= entry * (1 + PP_TRIGGER):
            stop, reason, be_at = entry, "BE", bar.t + timedelta(minutes=1)  # active from next bar
    last = [b for b in after if b.t < start + HORIZON][-1]
    return Path(start, entry, last.t + timedelta(minutes=1), last.c, "TIME", be_at,
                (hi / entry - 1) * 100, (lo / entry - 1) * 100)


def pnl_usdt(path: Path, quantity: float) -> float:
    return quantity * (path.exit - path.entry) - quantity * path.entry * FEE_RT


# ---------------------------------------------------------------- policies

@dataclass(frozen=True)
class Policy:
    name: str
    portfolio_cap: Decimal
    group_cap: Decimal
    mode: str                          # FIXED | ALLOC | FIXED_RISK | QUALITY
    risk_per_trade: Decimal = Decimal("0")   # FIXED_RISK / QUALITY: share of equity
    min_notional: Decimal = Decimal("50")    # ALLOC/FIXED_RISK: smaller positions are skipped
    quality_weight: object = None            # QUALITY: callable(features) -> weight


def _limits(policy: Policy) -> SafetyKernelConfig:
    return SafetyKernelConfig(max_portfolio_risk_pct=policy.portfolio_cap,
                              max_group_risk_pct=policy.group_cap)


@dataclass
class OpenTrade:
    opp: Opportunity
    path: Path
    quantity: Decimal
    risk_usdt: float


def decide(policy: Policy, opp: Opportunity, path: Path, equity: Decimal,
           open_trades: list[OpenTrade], now: datetime) -> tuple[Decimal, str]:
    """(quantity, reason). quantity 0 = rejected. Uses only state at `now`."""
    limits = _limits(policy)
    if len(open_trades) >= MAX_POSITIONS:
        return Decimal(0), "MAX_POSITIONS"
    if any(t.opp.symbol == opp.symbol for t in open_trades):
        return Decimal(0), "ONE_PER_SYMBOL"
    exposures = []
    for t in open_trades:
        current_stop = t.path.entry if (t.path.be_at is not None and t.path.be_at <= now) else t.opp.stop
        exposures.append(exposure_for_open_position(
            t.opp.symbol, t.quantity, Decimal(str(t.path.entry)), Decimal(str(current_stop)), limits))
    last = Decimal(str(path.entry))
    base = (MARGIN * LEVERAGE / last).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    decision = size_entry(
        symbol=opp.symbol, equity=equity, last_price=last, stop_loss=Decimal(str(opp.stop)),
        target=Decimal(str(opp.target)), leverage=LEVERAGE, base_quantity=base, quantity_precision=3,
        min_notional=Decimal("2"), open_exposures=exposures, limits=limits,
    )
    if policy.mode == "FIXED":
        if decision.action == "APPROVE":
            return decision.quantity, "APPROVE"
        return Decimal(0), "+".join(decision.reasons)
    if "LIQUIDATION_TOO_CLOSE" in decision.reasons:
        return Decimal(0), "LIQUIDATION_TOO_CLOSE"
    # ALLOC / FIXED_RISK / QUALITY: size inside the SAME hard caps, never above base
    entry_est = last * (1 + limits.entry_price_buffer_pct)
    unit = worst_case_risk_usdt(Decimal(1), entry_est, Decimal(str(opp.stop)), limits)
    port_room = equity * policy.portfolio_cap - sum(e.risk_usdt for e in exposures)
    group = group_for(opp.symbol, limits)
    grp_room = equity * policy.group_cap - sum(e.risk_usdt for e in exposures if e.group == group)
    budget = min(port_room, grp_room)
    if policy.mode in ("FIXED_RISK", "QUALITY"):
        weight = Decimal(str(policy.quality_weight(opp.features))) if policy.mode == "QUALITY" else Decimal(1)
        budget = min(budget, equity * policy.risk_per_trade * weight)
    if budget <= 0:
        return Decimal(0), "NO_RISK_BUDGET"
    qty = min(base, (budget / unit).quantize(Decimal("0.001"), rounding=ROUND_DOWN))
    if qty * last < policy.min_notional:
        return Decimal(0), "BELOW_MIN_NOTIONAL"
    return qty, "APPROVE" if qty == base else "ALLOCATED"


@dataclass
class Result:
    policy: str
    trades: list[dict]
    rejected: dict
    max_dd_usdt: float
    max_dd_pct: float
    worst_sim_risk_pct: float
    end_equity: float


def replay(policy: Policy, opps: list[tuple[Opportunity, Path]], equity0: float = 420.0) -> Result:
    equity = Decimal(str(equity0))
    open_trades: list[OpenTrade] = []
    trades, rejected = [], {}
    peak = equity0
    max_dd = max_dd_pct = worst_risk = 0.0
    for opp, path in sorted(opps, key=lambda x: x[1].entry_at):
        now = path.entry_at
        # close everything that exited before this decision (realise P/L in time order)
        for t in sorted([t for t in open_trades if t.path.exit_at <= now], key=lambda t: t.path.exit_at):
            open_trades.remove(t)
            equity += Decimal(str(pnl_usdt(t.path, float(t.quantity))))
            peak = max(peak, float(equity))
            max_dd = min(max_dd, float(equity) - peak)
            max_dd_pct = min(max_dd_pct, (float(equity) - peak) / peak * 100)
        qty, reason = decide(policy, opp, path, equity, open_trades, now)
        if qty <= 0:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        limits = _limits(policy)
        risk = float(worst_case_risk_usdt(qty, Decimal(str(path.entry)), Decimal(str(opp.stop)), limits))
        open_trades.append(OpenTrade(opp, path, qty, risk))
        sim_risk = 0.0
        for t in open_trades:
            cur = t.path.entry if (t.path.be_at is not None and t.path.be_at <= now) else t.opp.stop
            sim_risk += float(worst_case_risk_usdt(t.quantity, Decimal(str(t.path.entry)), Decimal(str(cur)), limits))
        worst_risk = max(worst_risk, sim_risk / float(equity) * 100)
        p = pnl_usdt(path, float(qty))
        trades.append({"candidate_id": opp.candidate_id, "symbol": opp.symbol, "entry_at": path.entry_at,
                       "notional": float(qty) * path.entry, "risk_usdt": risk, "pnl_usdt": p,
                       "r": p / risk if risk else 0.0, "reason": path.reason, "mfe_pct": path.mfe_pct,
                       "mae_pct": path.mae_pct, "allocation": reason})
    for t in sorted(open_trades, key=lambda t: t.path.exit_at):
        equity += Decimal(str(pnl_usdt(t.path, float(t.quantity))))
        peak = max(peak, float(equity))
        max_dd = min(max_dd, float(equity) - peak)
        max_dd_pct = min(max_dd_pct, (float(equity) - peak) / peak * 100)
    return Result(policy.name, trades, rejected, max_dd, max_dd_pct, worst_risk, float(equity))


def metrics(result: Result, days: float) -> dict:
    pnl = [t["pnl_usdt"] for t in result.trades]
    rs = [t["r"] for t in result.trades]
    gains, losses = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    return {
        "policy": result.policy, "trades": len(pnl), "per_day": round(len(pnl) / days, 2) if days else None,
        "total_pnl": round(sum(pnl), 2), "exp_usdt": round(st.mean(pnl), 3) if pnl else None,
        "exp_r": round(st.mean(rs), 3) if rs else None, "median_r": round(st.median(rs), 3) if rs else None,
        "win_rate": round(sum(p > 0 for p in pnl) / len(pnl), 3) if pnl else None,
        "pf": (round(gains / losses, 2) if losses else (math.inf if gains else None)),
        "max_dd_usdt": round(result.max_dd_usdt, 2), "max_dd_pct": round(result.max_dd_pct, 2),
        "worst_sim_risk_pct": round(result.worst_sim_risk_pct, 2),
        "avg_notional": round(st.mean(t["notional"] for t in result.trades), 1) if pnl else None,
        "mfe_pct": round(st.mean(t["mfe_pct"] for t in result.trades), 2) if pnl else None,
        "mae_pct": round(st.mean(t["mae_pct"] for t in result.trades), 2) if pnl else None,
        "rejected": dict(sorted(result.rejected.items(), key=lambda kv: -kv[1])),
        "end_equity": round(result.end_equity, 2),
    }
