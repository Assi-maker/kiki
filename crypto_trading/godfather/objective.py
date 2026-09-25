"""The optimization objective - what "better" is allowed to mean.

Requirement 10, and the reason it matters: a system that optimises win
rate will learn to take tiny profits and hold large losses, because that
maximises the fraction of trades that close green while destroying the
account. Every metric below exists to make that trade visible and
unprofitable to the optimiser.

`composite_score` is deliberately NOT a black box. It is a weighted sum
of five normalised terms, each with an explicit sign and an explicit
reason, and `evaluate_objective` returns every raw component beside it -
so any ranking this produces can be argued with on its components rather
than accepted on its number. Nothing in this module deploys anything;
`research.py`-style promotion is a separate, gated decision, and the
user's requirement 9 stands: robustness across periods and regimes, with
realistic costs, is required BEFORE any score is allowed to matter.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal("0")


@dataclass(frozen=True)
class TradeOutcome:
    """The minimum needed to score a book of trades. Built by
    `pipeline.py` from investigations; every field is measured, none
    estimated."""

    pnl: Decimal
    fees: Decimal
    funding: Decimal
    mfe_pnl: Decimal | None
    mae_pnl: Decimal | None
    notional: Decimal
    hold_minutes: float | None
    entry_slippage_pct: Decimal | None
    exit_slippage_pct: Decimal | None
    prediction_correct: bool | None = None


@dataclass(frozen=True)
class ObjectiveMetrics:
    trade_count: int
    net_pnl_usdt: Decimal
    expectancy_usdt: Decimal | None
    profit_factor: Decimal | None
    win_rate: float | None
    max_drawdown_usdt: Decimal
    avg_loss_usdt: Decimal | None
    worst_loss_usdt: Decimal | None
    loss_severity_ratio: Decimal | None
    mfe_capture_ratio: Decimal | None
    avg_mae_usdt: Decimal | None
    prediction_accuracy: float | None
    capital_efficiency_usdt_per_1k_hour: Decimal | None
    total_costs_usdt: Decimal
    cost_share_of_gross: Decimal | None
    avg_abs_slippage_pct: Decimal | None
    turnover_notional_usdt: Decimal
    composite_score: float


def _max_drawdown(pnls: list[Decimal]) -> Decimal:
    """Peak-to-trough of the cumulative P/L curve, in trade order.

    Drawdown on the realised equity curve rather than on marked-to-market
    equity: the system has no continuous account-equity series, and
    inventing one would be a worse answer than a well-defined narrower
    one.
    """
    peak = _ZERO
    cumulative = _ZERO
    worst = _ZERO
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return -worst


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return (numerator / denominator) if denominator != _ZERO else None


def evaluate_objective(outcomes: list[TradeOutcome]) -> ObjectiveMetrics:
    """Every requirement-10 metric, computed over one book of trades."""
    if not outcomes:
        return ObjectiveMetrics(
            trade_count=0,
            net_pnl_usdt=_ZERO,
            expectancy_usdt=None,
            profit_factor=None,
            win_rate=None,
            max_drawdown_usdt=_ZERO,
            avg_loss_usdt=None,
            worst_loss_usdt=None,
            loss_severity_ratio=None,
            mfe_capture_ratio=None,
            avg_mae_usdt=None,
            prediction_accuracy=None,
            capital_efficiency_usdt_per_1k_hour=None,
            total_costs_usdt=_ZERO,
            cost_share_of_gross=None,
            avg_abs_slippage_pct=None,
            turnover_notional_usdt=_ZERO,
            composite_score=0.0,
        )

    pnls = [o.pnl for o in outcomes]
    wins = [p for p in pnls if p > _ZERO]
    losses = [p for p in pnls if p < _ZERO]
    net = sum(pnls, _ZERO)
    gross_profit = sum(wins, _ZERO)
    gross_loss = -sum(losses, _ZERO)
    costs = sum((o.fees + o.funding for o in outcomes), _ZERO)
    turnover = sum((o.notional for o in outcomes), _ZERO)

    avg_win = _safe_div(gross_profit, Decimal(len(wins))) if wins else None
    avg_loss = _safe_div(gross_loss, Decimal(len(losses))) if losses else None
    loss_severity = (
        (avg_loss / avg_win) if (avg_win and avg_loss and avg_win != _ZERO) else None
    )

    captured = [
        (o.pnl / o.mfe_pnl)
        for o in outcomes
        if o.mfe_pnl is not None and o.mfe_pnl > _ZERO
    ]
    mfe_capture = (
        sum(captured, _ZERO) / Decimal(len(captured)) if captured else None
    )
    maes = [o.mae_pnl for o in outcomes if o.mae_pnl is not None]
    avg_mae = sum(maes, _ZERO) / Decimal(len(maes)) if maes else None

    exposure_1k_hours = sum(
        (
            (o.notional / Decimal("1000")) * Decimal(str((o.hold_minutes or 0.0) / 60))
            for o in outcomes
        ),
        _ZERO,
    )
    slippages = [
        abs(value)
        for o in outcomes
        for value in (o.entry_slippage_pct, o.exit_slippage_pct)
        if value is not None
    ]
    scored = [o.prediction_correct for o in outcomes if o.prediction_correct is not None]

    metrics_without_score = {
        "trade_count": len(outcomes),
        "net_pnl_usdt": net,
        "expectancy_usdt": net / Decimal(len(outcomes)),
        "profit_factor": _safe_div(gross_profit, gross_loss),
        "win_rate": len(wins) / len(outcomes),
        "max_drawdown_usdt": _max_drawdown(pnls),
        "avg_loss_usdt": avg_loss,
        "worst_loss_usdt": (-min(losses)) if losses else None,
        "loss_severity_ratio": loss_severity,
        "mfe_capture_ratio": mfe_capture,
        "avg_mae_usdt": avg_mae,
        "prediction_accuracy": (
            sum(1 for s in scored if s) / len(scored) if scored else None
        ),
        "capital_efficiency_usdt_per_1k_hour": _safe_div(net, exposure_1k_hours),
        "total_costs_usdt": costs,
        "cost_share_of_gross": _safe_div(costs, gross_profit + gross_loss),
        "avg_abs_slippage_pct": (
            sum(slippages, _ZERO) / Decimal(len(slippages)) if slippages else None
        ),
        "turnover_notional_usdt": turnover,
    }
    return ObjectiveMetrics(
        **metrics_without_score,
        composite_score=composite_score(metrics_without_score),
    )


# Weights. They encode a policy, so they are stated openly rather than
# tuned quietly:
#   net P/L is the objective, everything else is a guard on HOW it was
#   earned. Drawdown and loss severity are penalties because the failure
#   mode we are defending against (many small wins, rare huge losses)
#   looks excellent on net P/L alone until the day it does not. MFE
#   capture rewards converting the moves the entries already found -
#   which is precisely the gap the 2026-09-25 counterfactual sweep found
#   in the live book. Cost share penalises churn: a strategy that needs
#   more turnover to earn the same money is a worse strategy.
_W_NET = 1.0
_W_DRAWDOWN = 0.5
_W_LOSS_SEVERITY = 0.3
_W_MFE_CAPTURE = 0.2
_W_COST_SHARE = 0.2


def composite_score(metrics: dict) -> float:
    """A single comparable number, from normalised components.

    Normalisation is per-trade so books of different sizes can be
    compared: net P/L and drawdown are divided by trade count, the two
    ratios are already scale-free. The result is NOT a P/L in USDT and
    must never be reported as one.
    """
    trade_count = int(metrics.get("trade_count") or 0)
    if trade_count == 0:
        return 0.0
    net = float(metrics.get("net_pnl_usdt") or 0) / trade_count
    drawdown = float(metrics.get("max_drawdown_usdt") or 0) / trade_count
    severity = metrics.get("loss_severity_ratio")
    capture = metrics.get("mfe_capture_ratio")
    cost_share = metrics.get("cost_share_of_gross")

    score = _W_NET * net
    score -= _W_DRAWDOWN * drawdown
    if severity is not None:
        # 1.0 is the neutral point: average loss equal to average win.
        score -= _W_LOSS_SEVERITY * max(0.0, float(severity) - 1.0)
    if capture is not None:
        score += _W_MFE_CAPTURE * float(capture)
    if cost_share is not None:
        score -= _W_COST_SHARE * float(cost_share)
    return round(score, 6)


def compare_books(baseline: ObjectiveMetrics, candidate: ObjectiveMetrics) -> dict:
    """Baseline-versus-candidate on every requirement-10 dimension.

    `objectively_better` is intentionally demanding: a candidate must
    improve net P/L AND not worsen drawdown AND not worsen loss severity.
    A change that raises P/L by taking more risk is not an improvement to
    this system, it is a different risk appetite - and requirement 9 is
    explicit that nothing deploys merely because a number went up.
    """
    def _delta(attribute: str):
        base = getattr(baseline, attribute)
        cand = getattr(candidate, attribute)
        if base is None or cand is None:
            return None
        return cand - base

    net_delta = _delta("net_pnl_usdt")
    drawdown_delta = _delta("max_drawdown_usdt")
    severity_delta = _delta("loss_severity_ratio")
    better = (
        net_delta is not None
        and net_delta > _ZERO
        and (drawdown_delta is None or drawdown_delta <= _ZERO)
        and (severity_delta is None or severity_delta <= _ZERO)
    )
    return {
        "net_pnl_delta_usdt": str(net_delta) if net_delta is not None else None,
        "max_drawdown_delta_usdt": (
            str(drawdown_delta) if drawdown_delta is not None else None
        ),
        "loss_severity_delta": str(severity_delta) if severity_delta is not None else None,
        "composite_score_delta": round(
            candidate.composite_score - baseline.composite_score, 6
        ),
        "objectively_better": better,
    }
