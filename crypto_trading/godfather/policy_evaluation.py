"""Deep evaluation of ONE existing policy: `TIGHTEN_SL_AFTER_FAVORABLE`.

The policy is the break-even stop the system really runs: once a
position is +1% in favour, its stop is moved to the entry price
(`config/live_execution.yaml::profit_protection_threshold_pct`, executed
on the exchange by `paper_trading/live_profit_protection.py`). The first
GODFATHER sweep ranked it the worst of nine counterfactual policies. This
module answers the follow-up question properly: is the policy worse than
the original stop, how sure is that, and WHICH PART of it causes the
damage - or is there simply not enough data to say.

Diagnostic only. Nothing here changes, promotes or recommends-into-effect
any rule: the stored report carries `promotion_allowed = 0`, a value the
table's own CHECK constraint makes the only one it can hold.

**The comparison is paired, never pooled.** "Trades where the policy
fired lost money" mixes up two different things: the policy making a
trade worse, and the policy merely being applied to a bad trade. Every
effect below is therefore `policy outcome - no-policy outcome` for the
SAME trade over the SAME real price path. The naive pooled comparison is
reported alongside precisely so the difference is visible.

**No lookahead.** The stop in force at tick `i` is computed from
`points[:i]` only - a threshold touch observed at tick `i` takes effect
from tick `i + 1`, the same ordering the live mechanism and the PAPER
shadow experiment have (`profit_protection_experiment.advance_shadow`).
The tests prove it by mutating everything after a decision and asserting
the decision is unchanged.

**Pre-registered, not searched.** Exactly three stop policies are
evaluated, fixed before any of their results were computed:

* `TIGHTEN_SL_AFTER_FAVORABLE` - the live policy (break-even at +1.0%),
* `BREAKEVEN_AT_1_5PCT` - the one alternative threshold, frozen in
  `profit_protection_experiment.FROZEN_THRESHOLDS_PCT` on 2026-09-11,
* `PROFIT_LOCK_HALF_MFE` - the single profit-lock variant (from +1.0%,
  stop = entry + half the best excursion so far, ratcheting up only).

Everything else - the breakdowns by signal, regime, timing and so on - is
labelled exploratory and FDR-corrected as a family of its own.

**Two independent data sources.** The primary one is the per-tick price
path Guardian recorded (~97 s cadence). A second, completely independent
one is the forward-recorded PAPER shadow experiment
(`profit_protection_shadow_positions`), which applied the same break-even
rule in real time against 1-minute candle lows/highs. Ticks can miss an
intrabar wick; candles cannot - so when both agree the conclusion does
not rest on the tick resolution.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig, Settings
from crypto_trading.godfather import stats
from crypto_trading.godfather.costs import implied_funding_rate
from crypto_trading.godfather.entry_quality import assess_entry_quality
from crypto_trading.godfather.features import build_candidate_features
from crypto_trading.godfather.path import PathPoint, reconstruct_price_path
from crypto_trading.godfather.pipeline import _regime_for, _safe_candidate, _thresholds
from crypto_trading.godfather.stop_simulation import (
    MAX_UNOBSERVED_MINUTES,
    StopPolicy,
    StopSimulation,
    simulate_stop_policy,
)
from crypto_trading.godfather.stop_simulation import excursion as _excursion
from crypto_trading.godfather.thesis import (
    ThesisThresholds,
    build_thesis_features,
    classify_thesis_state,
)
from crypto_trading.paper_trading.execution import compute_pnl_or_none
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")

POLICY_UNDER_TEST = "TIGHTEN_SL_AFTER_FAVORABLE"

# Below this many activated trades no verdict other than INSUFFICIENT_DATA
# is given - the same floor Experience Memory applies to a pattern.
MIN_ACTIVATED_FOR_VERDICT = 30

# Per-cell floor for the exploratory breakdowns, and per-half floor for
# the out-of-sample replication check.
MIN_CELL = 8
MIN_HALF_CELL = 5

FDR_Q = 0.10



PRE_REGISTERED_POLICIES: tuple[StopPolicy, ...] = (
    StopPolicy(
        POLICY_UNDER_TEST,
        Decimal("0.01"),
        _ZERO,
        "policy_under_test",
        "live: live_execution.yaml profit_protection_threshold_pct = 0.01",
    ),
    StopPolicy(
        "BREAKEVEN_AT_1_5PCT",
        Decimal("0.015"),
        _ZERO,
        "threshold_alternative",
        "frozen 2026-09-11 in profit_protection_experiment.FROZEN_THRESHOLDS_PCT",
    ),
    StopPolicy(
        "PROFIT_LOCK_HALF_MFE",
        Decimal("0.01"),
        Decimal("0.5"),
        "profit_lock_alternative",
        "declared 2026-09-25 before its results were computed; the only variant",
    ),
)


def classify_effect(sim: StopSimulation, baseline_pnl: Decimal) -> str:
    """What the policy did to THIS trade - never what the trade did."""
    if not sim.activated:
        return "NOT_ACTIVATED"
    delta = sim.pnl_usdt - baseline_pnl
    if not sim.stopped or delta == _ZERO:
        return "NEUTRAL"
    if baseline_pnl > _ZERO:
        return "WINNER_STOPPED_EARLY" if delta < _ZERO else "WINNER_IMPROVED"
    return "LOSS_LIMITED" if delta > _ZERO else "LOSER_MADE_WORSE"


# ---------------------------------------------------------------------
# Strata. Every one is known at or before the intervention moment.
# ---------------------------------------------------------------------


def _bucket(value: float, edges: list[float], labels: list[str]) -> str:
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


def sl_distance_bucket(pct: float) -> str:
    return _bucket(pct, [3.5, 5.0], ["<3.5%", "3.5-5%", ">=5%"])


def activation_progress_bucket(progress: float) -> str:
    """How far toward the target the trade was when the stop moved - the
    "too early relative to the plan" axis."""
    return _bucket(progress, [0.2, 0.33], ["<0.20", "0.20-0.33", ">=0.33"])


def mfe_at_activation_bucket(pct: float) -> str:
    return _bucket(pct, [1.25, 1.75], ["<1.25%", "1.25-1.75%", ">=1.75%"])


def minutes_to_activation_bucket(minutes: float) -> str:
    return _bucket(minutes, [30.0, 120.0], ["<30m", "30-120m", ">=120m"])


# ---------------------------------------------------------------------
# Trade record (the per-trade answer to questions 1-12)
# ---------------------------------------------------------------------


@dataclass
class TradeEvaluation:
    position_id: str
    instrument: str
    opened_at: datetime
    closed_at: datetime | None
    entry_price: Decimal
    original_stop_loss: Decimal
    target: Decimal
    size: Decimal
    real_exit_reason: str | None
    actual_pnl_usdt: Decimal
    mfe_pct: Decimal
    mae_pct: Decimal
    sims: dict[str, StopSimulation]
    mfe_after_pct: Decimal | None
    mae_after_pct: Decimal | None
    final_return_pct: Decimal | None
    strata: dict[str, str] = field(default_factory=dict)

    def delta(self, policy: str, pessimistic: bool = False) -> Decimal:
        sim = self.sims[policy]
        pnl = sim.pnl_pessimistic_usdt if pessimistic else sim.pnl_usdt
        return pnl - self.actual_pnl_usdt

    def to_dict(self) -> dict:
        primary = self.sims[POLICY_UNDER_TEST]
        additional = None
        if self.mfe_after_pct is not None and primary.mfe_before_pct is not None:
            additional = max(_ZERO, self.mfe_after_pct - primary.mfe_before_pct)
        return {
            "position_id": self.position_id,
            "instrument": self.instrument,
            "entry_time": self.opened_at.isoformat(),
            "entry_price": str(self.entry_price),
            "original_stop_loss": str(self.original_stop_loss),
            "target": str(self.target),
            "real_exit_reason": self.real_exit_reason,
            "activated_at": primary.activation_at.isoformat() if primary.activation_at else None,
            "minutes_to_activation": primary.activation_minutes,
            "mfe_before_pct": _s(primary.mfe_before_pct),
            "mae_before_pct": _s(primary.mae_before_pct),
            "mfe_after_pct": _s(self.mfe_after_pct),
            "mae_after_pct": _s(self.mae_after_pct),
            "final_return_pct": _s(self.final_return_pct),
            "stopped_by_new_sl": primary.stopped,
            "stopped_at_minutes": primary.stop_minutes,
            "additional_mfe_after_pct": _s(additional),
            "actual_pnl_usdt": str(self.actual_pnl_usdt),
            "no_intervention_pnl_usdt": str(self.actual_pnl_usdt),
            "original_sl_pnl_usdt": str(self.actual_pnl_usdt),
            "breakeven_pnl_usdt": str(primary.pnl_usdt),
            "breakeven_pnl_pessimistic_usdt": str(primary.pnl_pessimistic_usdt),
            "breakeven_1_5pct_pnl_usdt": str(self.sims["BREAKEVEN_AT_1_5PCT"].pnl_usdt),
            "profit_lock_pnl_usdt": str(self.sims["PROFIT_LOCK_HALF_MFE"].pnl_usdt),
            "effect": (
                classify_effect(primary, self.actual_pnl_usdt) if primary.observable
                else "UNOBSERVABLE"
            ),
            "max_unobserved_minutes_armed": primary.max_unobserved_minutes_armed,
            "strata": dict(self.strata),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(round(value, 4))


def evaluate_trade(
    position: Position,
    points: list[PathPoint],
    risk_limits: RiskLimitsConfig,
    strata: dict[str, str] | None = None,
    policies: tuple[StopPolicy, ...] = PRE_REGISTERED_POLICIES,
    thresholds: ThesisThresholds | None = None,
) -> TradeEvaluation | None:
    """None when the real P/L is unknown (a LIVE-mirrored close with no
    PAPER exit data), there is no path, or the position had no exposure.

    Zero-size positions (exposure-blocked at entry - 41 of the first 146
    closes) carry P/L 0 under EVERY policy. Counted, they would enter the
    sample as dozens of perfectly "neutral" trades and pull every mean
    toward zero; they are excluded, not scored."""
    if position.size == _ZERO:
        return None
    actual = compute_pnl_or_none(position)
    if actual is None or not points or position.simulated_fill_entry == _ZERO:
        return None
    entry = position.simulated_fill_entry
    funding_rate = implied_funding_rate(position)
    sims = {
        policy.name: simulate_stop_policy(
            position, points, policy, actual, risk_limits, funding_rate
        )
        for policy in policies
    }
    excursions = [_excursion(p.price, entry) for p in points]
    if position.theoretical_exit is not None:
        excursions.append(_excursion(position.theoretical_exit, entry))

    primary = sims[POLICY_UNDER_TEST]
    mfe_after = mae_after = final = None
    if primary.activated and primary.activation_index is not None:
        after = [_excursion(p.price, entry) for p in points[primary.activation_index + 1 :]]
        if position.theoretical_exit is not None:
            after.append(_excursion(position.theoretical_exit, entry))
        if after:
            mfe_after = max(after) * _HUNDRED
            mae_after = min(after) * _HUNDRED
            final = after[-1] * _HUNDRED

    record_strata = dict(strata or {})
    sl_pct = (entry - position.stop_loss) / entry * _HUNDRED
    record_strata["sl_distance"] = sl_distance_bucket(float(sl_pct))
    if primary.activated:
        span = position.target - entry
        progress = (
            (primary.mfe_before_pct / _HUNDRED * entry) / span if span > _ZERO else _ZERO
        )
        record_strata["activation_progress_to_target"] = activation_progress_bucket(
            float(progress)
        )
        record_strata["mfe_at_activation"] = mfe_at_activation_bucket(
            float(primary.mfe_before_pct or _ZERO)
        )
        record_strata["minutes_to_activation"] = minutes_to_activation_bucket(
            float(primary.activation_minutes or 0.0)
        )
        if thresholds is not None and primary.activation_index is not None:
            # The thesis as it stood AT the intervention, from the prefix only:
            # does break-even hurt STRONG trades and help WEAKENING ones?
            features = build_thesis_features(
                position, points[: primary.activation_index + 1], thresholds.max_hold_hours
            )
            if features is not None:
                state, _reasons = classify_thesis_state(features, thresholds)
                record_strata["thesis_state_at_activation"] = state
                record_strata["momentum_decay_at_activation"] = _bucket(
                    features.factor("momentum_decay"), [0.3, 0.6], ["<0.3", "0.3-0.6", ">=0.6"]
                )

    return TradeEvaluation(
        position_id=position.position_id,
        instrument=position.instrument,
        opened_at=position.opened_at,
        closed_at=position.closed_at,
        entry_price=entry,
        original_stop_loss=position.stop_loss,
        target=position.target,
        size=position.size,
        real_exit_reason=position.exit_reason,
        actual_pnl_usdt=actual,
        mfe_pct=max(excursions) * _HUNDRED,
        mae_pct=min(excursions) * _HUNDRED,
        sims=sims,
        mfe_after_pct=mfe_after,
        mae_after_pct=mae_after,
        final_return_pct=final,
        strata=record_strata,
    )


# ---------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------


def scored(trades: list[TradeEvaluation], policy: str) -> list[TradeEvaluation]:
    """The trades a policy is judged on: it activated, and its moved stop
    was actually watched the whole time it was in force."""
    return [t for t in trades if t.sims[policy].activated and t.sims[policy].observable]


def _floats(values: list[Decimal]) -> list[float]:
    return [float(v) for v in values]


def _median(values: list[Decimal]) -> Decimal | None:
    return statistics.median(values) if values else None


def _mfe_capture(pnls: list[Decimal], trades: list[TradeEvaluation]) -> float | None:
    """Realised P/L as a fraction of the P/L available at the best point
    of each trade - the book-level ratio, so one tiny-MFE trade cannot
    dominate it the way a per-trade average would."""
    available = sum((t.size * max(_ZERO, t.mfe_pct) / _HUNDRED for t in trades), _ZERO)
    if available == _ZERO:
        return None
    return float(sum(pnls, _ZERO) / available)


def outcome_summary(pnls: list[Decimal], trades: list[TradeEvaluation]) -> dict:
    n = len(pnls)
    return {
        "n": n,
        "total_pnl_usdt": str(sum(pnls, _ZERO)),
        "mean_pnl_usdt": str(sum(pnls, _ZERO) / n) if n else None,
        "median_pnl_usdt": str(_median(pnls)) if n else None,
        "win_rate": (sum(1 for p in pnls if p > _ZERO) / n) if n else None,
        "mfe_capture": _mfe_capture(pnls, trades),
    }


def paired_test(deltas: list[Decimal]) -> dict:
    """Mean per-trade effect with a bootstrap CI, a sign-flip
    randomisation p-value on that mean (the primary p, fed to BH), and the
    exact sign test on improved-vs-worsened (secondary, as in the first
    sweep). Zeros are genuine "no effect" trades and stay in the mean."""
    values = _floats(deltas)
    ci = stats.bootstrap_mean_ci(values)
    improved = sum(1 for d in deltas if d > _ZERO)
    worsened = sum(1 for d in deltas if d < _ZERO)
    return {
        "n": len(deltas),
        "total_delta_usdt": str(sum(deltas, _ZERO)),
        "mean_delta_usdt": (str(sum(deltas, _ZERO) / len(deltas)) if deltas else None),
        "median_delta_usdt": str(_median(deltas)) if deltas else None,
        "ci_low_usdt": ci.lower if ci else None,
        "ci_high_usdt": ci.upper if ci else None,
        "improved": improved,
        "worsened": worsened,
        "neutral": len(deltas) - improved - worsened,
        "p_value": stats.sign_flip_p_value(values),
        "sign_test_p_value": stats.binomial_test_two_sided(improved, improved + worsened, 0.5),
    }


def _halves_by_cut(
    trades: list[TradeEvaluation], cut: datetime
) -> tuple[list[TradeEvaluation], list[TradeEvaluation]]:
    train = [t for t in trades if t.opened_at < cut]
    test = [t for t in trades if t.opened_at >= cut]
    return train, test


def _mean_or_none(values: list[Decimal]) -> float | None:
    return float(sum(values, _ZERO) / len(values)) if values else None


def verdict_for(test: dict, fdr_significant: bool, train_mean: float | None,
                test_mean: float | None) -> str:
    if test["n"] < MIN_ACTIVATED_FOR_VERDICT:
        return "INSUFFICIENT_DATA"
    ci_low, ci_high = test["ci_low_usdt"], test["ci_high_usdt"]
    both_halves = train_mean is not None and test_mean is not None
    if fdr_significant and ci_high is not None and ci_high < 0 and both_halves \
            and train_mean < 0 and test_mean < 0:
        return "ROBUST_HARM"
    if fdr_significant and ci_low is not None and ci_low > 0 and both_halves \
            and train_mean > 0 and test_mean > 0:
        return "ROBUST_IMPROVEMENT"
    return "NOISE"


def policy_block(trades: list[TradeEvaluation], policy: StopPolicy, cut: datetime) -> dict:
    """Everything the summary asks for, for one policy, over the trades
    where THAT policy activated (its own intervention set)."""
    active = scored(trades, policy.name)
    unobservable = [
        t for t in trades
        if t.sims[policy.name].activated and not t.sims[policy.name].observable
    ]
    baseline = [t.actual_pnl_usdt for t in active]
    with_policy = [t.sims[policy.name].pnl_usdt for t in active]
    deltas = [t.delta(policy.name) for t in active]
    effects: dict[str, int] = {}
    effect_delta: dict[str, Decimal] = {}
    for t in active:
        effect = classify_effect(t.sims[policy.name], t.actual_pnl_usdt)
        effects[effect] = effects.get(effect, 0) + 1
        effect_delta[effect] = effect_delta.get(effect, _ZERO) + t.delta(policy.name)
    train, test = _halves_by_cut(active, cut)
    stopped = [t for t in active if t.sims[policy.name].stopped]
    return {
        "policy": policy.name,
        "role": policy.role,
        "provenance": policy.provenance,
        "activation_pct": str(policy.activation_pct),
        "lock_fraction": str(policy.lock_fraction),
        "activated": len(active),
        "excluded_unobservable": len(unobservable),
        "stopped_by_new_sl": len(stopped),
        "baseline": outcome_summary(baseline, active),
        "with_policy": outcome_summary(with_policy, active),
        "baseline_mean_mae_pct": _mean_or_none([t.mae_pct for t in active]),
        "policy_mean_mae_pct": _mean_or_none(
            [t.sims[policy.name].mae_until_exit_pct for t in active]
        ),
        "effects": effects,
        "effect_delta_usdt": {k: str(v) for k, v in effect_delta.items()},
        "stopped_then_real_target": sum(
            1 for t in stopped if (t.real_exit_reason or "").lower() == "target"
        ),
        "paired": paired_test(deltas),
        "pessimistic_fill": paired_test([t.delta(policy.name, pessimistic=True) for t in active]),
        "train": {"n": len(train), "mean_delta_usdt": _mean_or_none(
            [t.delta(policy.name) for t in train])},
        "test": {"n": len(test), "mean_delta_usdt": _mean_or_none(
            [t.delta(policy.name) for t in test])},
    }


# ---------------------------------------------------------------------
# Independent replication: the forward-recorded PAPER shadow experiment
# ---------------------------------------------------------------------

_SHADOW_POLICY = {"0.010": POLICY_UNDER_TEST, "0.015": "BREAKEVEN_AT_1_5PCT"}


def shadow_replication(
    shadow_rows: list[dict], cut: datetime, excluded_position_ids: frozenset[str] = frozenset()
) -> dict[str, dict]:
    """Paired deltas the experiment itself recorded, in real time, per
    closed shadow whose threshold was reached. Rows whose threshold was
    never reached are used only as a fidelity check: their delta must be
    exactly zero, otherwise the shadow simulator disagrees with the real
    book for reasons that have nothing to do with the policy."""
    out: dict[str, dict] = {}
    for threshold, policy in _SHADOW_POLICY.items():
        rows = [
            r for r in shadow_rows
            if r.get("threshold_pct") == threshold and r.get("status") == "CLOSED"
            and r.get("pnl_difference") is not None
            and r.get("position_id") not in excluded_position_ids
        ]
        reached = sorted(
            (r for r in rows if r.get("threshold_reached")), key=lambda r: r["opened_at"]
        )
        untouched = [r for r in rows if not r.get("threshold_reached")]
        deltas = [Decimal(str(r["pnl_difference"])) for r in reached]
        train = [Decimal(str(r["pnl_difference"])) for r in reached
                 if datetime.fromisoformat(r["opened_at"]) < cut]
        test = [Decimal(str(r["pnl_difference"])) for r in reached
                if datetime.fromisoformat(r["opened_at"]) >= cut]
        baseline = [Decimal(str(r["hypothetical_baseline_pnl"])) for r in reached]
        shadow = [Decimal(str(r["shadow_realized_pnl"])) for r in reached]
        out[policy] = {
            "threshold_pct": threshold,
            "reached_and_scorable": len(reached),
            "not_reached_rows": len(untouched),
            "not_reached_nonzero_delta": sum(
                1 for r in untouched if Decimal(str(r["pnl_difference"])) != _ZERO
            ),
            "baseline_total_usdt": str(sum(baseline, _ZERO)),
            "shadow_total_usdt": str(sum(shadow, _ZERO)),
            "winners_stopped_early": sum(
                1 for b, d in zip(baseline, deltas, strict=True) if b > _ZERO and d < _ZERO
            ),
            "losses_limited": sum(
                1 for b, d in zip(baseline, deltas, strict=True) if b <= _ZERO and d > _ZERO
            ),
            "paired": paired_test(deltas),
            "train": {"n": len(train), "mean_delta_usdt": _mean_or_none(train)},
            "test": {"n": len(test), "mean_delta_usdt": _mean_or_none(test)},
            "position_ids": [r["position_id"] for r in reached],
        }
    return out


def cross_check_sources(trades: list[TradeEvaluation], replication: dict) -> dict:
    """Do the tick path and the candle shadow tell the same story, trade
    by trade, on the trades both cover?"""
    by_id = {t.position_id: t for t in trades}
    shadow_ids = set(replication.get(POLICY_UNDER_TEST, {}).get("position_ids", []))
    common = [by_id[pid] for pid in shadow_ids if pid in by_id]
    path_ids = {t.position_id for t in scored(trades, POLICY_UNDER_TEST)}
    return {
        "shadow_activated": len(shadow_ids),
        "path_activated_within_shadow_window": len(
            {t.position_id for t in common if t.sims[POLICY_UNDER_TEST].activated}
        ),
        "both_activated": len(shadow_ids & path_ids),
        "shadow_only": len(shadow_ids - path_ids),
    }


# ---------------------------------------------------------------------
# Exploratory breakdowns + out-of-sample replication of the diagnosis
# ---------------------------------------------------------------------

STRATA_DIMENSIONS = (
    "signal_type",
    "entry_quality",
    "regime",
    "sl_distance",
    "mfe_at_activation",
    "minutes_to_activation",
    "activation_progress_to_target",
    "thesis_state_at_activation",
    "momentum_decay_at_activation",
)


def breakdowns(trades: list[TradeEvaluation], policy: str) -> dict:
    active = scored(trades, policy)
    cells: list[tuple[str, str, list[Decimal], list[Decimal]]] = []
    for dimension in STRATA_DIMENSIONS:
        groups: dict[str, list[TradeEvaluation]] = {}
        for t in active:
            groups.setdefault(t.strata.get(dimension, "unknown"), []).append(t)
        for value, members in sorted(groups.items()):
            cells.append((
                dimension,
                value,
                [t.delta(policy) for t in members],
                [t.actual_pnl_usdt for t in members],
            ))
    testable = [c for c in cells if len(c[2]) >= MIN_CELL]
    p_values = [stats.sign_flip_p_value(_floats(c[2])) for c in testable]
    flags = dict(zip(
        [(c[0], c[1]) for c in testable], stats.benjamini_hochberg(p_values, FDR_Q),
        strict=True,
    ))
    out: dict[str, list[dict]] = {}
    for dimension, value, deltas, baseline in cells:
        ci = stats.bootstrap_mean_ci(_floats(deltas))
        enough = len(deltas) >= MIN_CELL
        significant = flags.get((dimension, value), False)
        out.setdefault(dimension, []).append({
            "value": value,
            "n": len(deltas),
            "baseline_mean_pnl_usdt": _mean_or_none(baseline),
            "mean_delta_usdt": _mean_or_none(deltas),
            "total_delta_usdt": str(sum(deltas, _ZERO)),
            "ci_low_usdt": ci.lower if ci else None,
            "ci_high_usdt": ci.upper if ci else None,
            "fdr_significant": significant,
            "status": ("INSUFFICIENT_DATA" if not enough
                       else "SIGNIFICANT" if significant else "NOISE"),
        })
    return {"family_size": len(testable), "fdr_q": FDR_Q, "cells": out}


def oos_diagnosis(trades: list[TradeEvaluation], policy: str, cut: datetime) -> dict:
    """The diagnostic is itself a search ("where is the harm?"), so it is
    held to a train/test split: the worst cell per dimension is PICKED on
    the chronologically earlier half and only then LOOKED UP on the later
    half. A diagnosis that does not survive that is not a diagnosis."""
    active = scored(trades, policy)
    train, test = _halves_by_cut(active, cut)
    result: dict[str, dict] = {}
    for dimension in STRATA_DIMENSIONS:
        def _groups(rows: list[TradeEvaluation], dim: str = dimension) -> dict:
            groups: dict[str, list[Decimal]] = {}
            for t in rows:
                groups.setdefault(t.strata.get(dim, "unknown"), []).append(t.delta(policy))
            return groups

        train_groups = {k: v for k, v in _groups(train).items() if len(v) >= MIN_HALF_CELL}
        if len(train_groups) < 2:
            result[dimension] = {"status": "INSUFFICIENT_DATA"}
            continue
        worst = min(train_groups, key=lambda k: sum(train_groups[k]) / len(train_groups[k]))
        test_groups = _groups(test)
        in_cell = test_groups.get(worst, [])
        rest = [d for k, v in test_groups.items() if k != worst for d in v]
        train_rest = [d for k, v in _groups(train).items() if k != worst for d in v]
        if len(in_cell) < MIN_HALF_CELL or len(rest) < MIN_HALF_CELL:
            result[dimension] = {
                "status": "INSUFFICIENT_DATA",
                "train_worst_cell": worst,
                "test_n_in_cell": len(in_cell),
            }
            continue
        test_gap = _mean_or_none(in_cell) - _mean_or_none(rest)
        train_gap = _mean_or_none(train_groups[worst]) - (_mean_or_none(train_rest) or 0.0)
        result[dimension] = {
            "status": "REPLICATED" if test_gap < 0 and train_gap < 0 else "NOT_REPLICATED",
            "train_worst_cell": worst,
            "train_mean_in_cell": _mean_or_none(train_groups[worst]),
            "train_gap_vs_rest": train_gap,
            "test_n_in_cell": len(in_cell),
            "test_mean_in_cell": _mean_or_none(in_cell),
            "test_mean_rest": _mean_or_none(rest),
            "test_gap_vs_rest": test_gap,
        }
    return result


# ---------------------------------------------------------------------
# LIVE: the real activations on the exchange
# ---------------------------------------------------------------------


def live_activations(rows: list[dict]) -> dict:
    """Every real LIVE activation, with the real exchange result next to
    the PAPER twin's (which never had its stop moved). Descriptive only:
    exchange fills and paper fills differ for reasons unrelated to the
    policy, and n is far below any verdict floor."""
    trades = []
    for r in rows:
        entry = _dec(r.get("exchange_fill_entry"))
        exit_ = _dec(r.get("exchange_fill_exit"))
        paper_entry = _dec(r.get("paper_entry"))
        paper_exit = _dec(r.get("paper_exit"))
        live_ret = ((exit_ - entry) / entry * _HUNDRED) if entry and exit_ else None
        paper_ret = (
            (paper_exit - paper_entry) / paper_entry * _HUNDRED
            if paper_entry and paper_exit else None
        )
        be = _dec(r.get("breakeven_price"))
        stopped_near_be = (
            exit_ is not None and be is not None
            and (r.get("live_exit_reason") or "").lower() == "stop_loss"
            and abs(exit_ - be) / be <= Decimal("0.005")
        )
        trades.append({
            "position_id": r["position_id"],
            "status": r.get("status"),
            "activated_at": r.get("claimed_at"),
            "breakeven_price": r.get("breakeven_price"),
            "live_exit_reason": r.get("live_exit_reason"),
            "live_return_pct": _s(live_ret),
            "paper_exit_reason": r.get("paper_exit_reason"),
            "paper_return_pct": _s(paper_ret),
            "live_stopped_near_breakeven": stopped_near_be,
        })
    comparable = [t for t in trades if t["live_return_pct"] and t["paper_return_pct"]]
    return {
        "n": len(trades),
        "verdict": "INSUFFICIENT_DATA",
        "stopped_near_breakeven": sum(1 for t in trades if t["live_stopped_near_breakeven"]),
        "stopped_near_breakeven_while_paper_hit_target": sum(
            1 for t in trades
            if t["live_stopped_near_breakeven"] and (t["paper_exit_reason"] or "") == "target"
        ),
        "comparable_with_paper": len(comparable),
        "mean_live_minus_paper_return_pct": _mean_or_none([
            Decimal(t["live_return_pct"]) - Decimal(t["paper_return_pct"]) for t in comparable
        ]),
        "trades": trades,
    }


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


# ---------------------------------------------------------------------
# The whole evaluation
# ---------------------------------------------------------------------


def chronological_cut(trades: list[TradeEvaluation]) -> datetime:
    """Median entry time of the scorable book: everything opened before
    it is TRAIN, everything at/after it is TEST. One calendar cut for all
    policies and both data sources, so they are split identically."""
    ordered = sorted(t.opened_at for t in trades)
    return ordered[len(ordered) // 2]


def evaluate_policy(
    trades: list[TradeEvaluation],
    shadow_rows: list[dict],
    live_rows: list[dict],
    now: datetime,
    run_id: str,
    excluded_position_ids: frozenset[str] = frozenset(),
) -> dict:
    if not trades:
        return {"policy": POLICY_UNDER_TEST, "verdict": "INSUFFICIENT_DATA",
                "reason": "no scorable trade with a real price path"}
    trades = sorted(trades, key=lambda t: t.opened_at)
    cut = chronological_cut(trades)

    blocks = {p.name: policy_block(trades, p, cut) for p in PRE_REGISTERED_POLICIES}
    replication = shadow_replication(shadow_rows, cut, excluded_position_ids)

    # The confirmatory family: every pre-registered policy on the path
    # data, plus both shadow replications. BH across all five.
    family: list[tuple[str, dict, dict]] = [
        (f"path:{name}", block["paired"], block) for name, block in blocks.items()
    ] + [
        (f"shadow:{name}", rep["paired"], rep) for name, rep in replication.items()
    ]
    flags = stats.benjamini_hochberg([test["p_value"] for _k, test, _b in family], FDR_Q)
    confirmatory = {}
    for (key, test, holder), significant in zip(family, flags, strict=True):
        verdict = verdict_for(
            test, significant, holder["train"]["mean_delta_usdt"],
            holder["test"]["mean_delta_usdt"],
        )
        holder["fdr_significant"] = significant
        holder["verdict"] = verdict
        confirmatory[key] = {"verdict": verdict, "fdr_significant": significant,
                             "p_value": test["p_value"], "n": test["n"],
                             "mean_delta_usdt": test["mean_delta_usdt"],
                             "ci": [test["ci_low_usdt"], test["ci_high_usdt"]]}

    naive = _naive_comparison(trades)
    threshold_vs_live = _paired_between(trades, "BREAKEVEN_AT_1_5PCT", POLICY_UNDER_TEST)
    lock_vs_live = _paired_between(trades, "PROFIT_LOCK_HALF_MFE", POLICY_UNDER_TEST)
    strata = breakdowns(trades, POLICY_UNDER_TEST)
    oos = oos_diagnosis(trades, POLICY_UNDER_TEST, cut)
    mechanism = _mechanism(trades)
    live = live_activations(live_rows)

    report = {
        "policy": POLICY_UNDER_TEST,
        "evaluated_at": now.isoformat(),
        "run_id": run_id,
        "promotion_allowed": False,
        "data": {
            "scorable_trades_with_path": len(trades),
            "first_entry": trades[0].opened_at.isoformat(),
            "last_entry": trades[-1].opened_at.isoformat(),
            "train_test_cut": cut.isoformat(),
            "shadow_rows": len(shadow_rows),
            "excluded_zero_size_positions": len(excluded_position_ids),
            "live_activation_rows": len(live_rows),
        },
        "policies": blocks,
        "shadow_replication": {k: {kk: vv for kk, vv in v.items() if kk != "position_ids"}
                               for k, v in replication.items()},
        "source_cross_check": cross_check_sources(trades, replication),
        "confirmatory": confirmatory,
        "naive_vs_paired": naive,
        "threshold_1_5_vs_live": threshold_vs_live,
        "profit_lock_vs_live": lock_vs_live,
        "mechanism": mechanism,
        "breakdowns": strata,
        "oos_diagnosis": oos,
        "live_activations": live,
        "trades": [t.to_dict() for t in trades if t.sims[POLICY_UNDER_TEST].activated],
        "max_unobserved_minutes": MAX_UNOBSERVED_MINUTES,
    }
    report["answers"] = answer_questions(report)
    report["verdict"] = report["answers"]["overall_verdict"]
    report["confidence"] = report["answers"]["confidence"]
    return report


def _naive_comparison(trades: list[TradeEvaluation]) -> dict:
    """The misleading comparison, shown so its bias is visible: trades
    that ever reach +1% are better trades to begin with. Comparing them
    to trades that never did measures the TRADE, not the POLICY."""
    active = [t.actual_pnl_usdt for t in scored(trades, POLICY_UNDER_TEST)]
    idle = [t.actual_pnl_usdt for t in trades if not t.sims[POLICY_UNDER_TEST].activated]
    deltas = [t.delta(POLICY_UNDER_TEST) for t in scored(trades, POLICY_UNDER_TEST)]
    return {
        "activated_trades_mean_real_pnl_usdt": _mean_or_none(active),
        "never_activated_trades_mean_real_pnl_usdt": _mean_or_none(idle),
        "selection_effect_usdt": (
            None if not active or not idle
            else _mean_or_none(active) - _mean_or_none(idle)
        ),
        "paired_policy_effect_mean_usdt": _mean_or_none(deltas),
    }


def _paired_between(trades: list[TradeEvaluation], a: str, b: str) -> dict:
    """Policy a minus policy b on the union of trades where either one
    activated (on the others both equal the real outcome)."""
    rows = [
        t for t in trades
        if (t.sims[a].activated or t.sims[b].activated)
        and t.sims[a].observable and t.sims[b].observable
    ]
    return paired_test([t.sims[a].pnl_usdt - t.sims[b].pnl_usdt for t in rows])


def _mechanism(trades: list[TradeEvaluation]) -> dict:
    """Which PART of the policy does the damage."""
    active = scored(trades, POLICY_UNDER_TEST)
    stopped = [t for t in active if t.sims[POLICY_UNDER_TEST].stopped]
    by_effect: dict[str, list[Decimal]] = {}
    for t in active:
        by_effect.setdefault(
            classify_effect(t.sims[POLICY_UNDER_TEST], t.actual_pnl_usdt), []
        ).append(t.delta(POLICY_UNDER_TEST))
    retraced = [t for t in active if t.mae_after_pct is not None and t.mae_after_pct <= _ZERO]
    maes_after = [t.mae_after_pct for t in active if t.mae_after_pct is not None]
    recovered = [
        t for t in stopped
        if t.mfe_after_pct is not None and t.sims[POLICY_UNDER_TEST].mfe_before_pct is not None
        and t.mfe_after_pct > t.sims[POLICY_UNDER_TEST].mfe_before_pct
    ]
    return {
        "activated": len(active),
        "revisited_entry_after_activation": len(retraced),
        "stopped": len(stopped),
        "stopped_then_made_new_high": len(recovered),
        "stopped_then_real_target": sum(
            1 for t in stopped if (t.real_exit_reason or "").lower() == "target"
        ),
        "delta_by_effect_usdt": {k: str(sum(v, _ZERO)) for k, v in by_effect.items()},
        "count_by_effect": {k: len(v) for k, v in by_effect.items()},
        "median_mae_after_activation_pct": (
            float(statistics.median(maes_after)) if maes_after else None
        ),
        "median_sl_distance_pct": (
            float(statistics.median(
                [(t.entry_price - t.original_stop_loss) / t.entry_price * _HUNDRED
                 for t in active]))
            if active else None
        ),
    }


def _point_direction(values: list[float | None]) -> str:
    known = [v for v in values if v is not None]
    if not known:
        return "UNKNOWN"
    if all(v < 0 for v in known):
        return "NEGATIVE"
    if all(v > 0 for v in known):
        return "POSITIVE"
    return "MIXED"


def _float_or_none(value: object) -> float | None:
    return None if value is None else float(value)


def answer_questions(report: dict) -> dict:
    """The five candidate explanations, each answered from the numbers
    above by a fixed rule - not by reading the report and deciding.

    Four answers are possible and they mean different things:
    SUPPORTED (the evidence clears the bar), NOT_SUPPORTED (the evidence
    points against it), NOISE (enough data, no distinguishable effect)
    and INSUFFICIENT_DATA (too few samples to test it at all). A claim
    that failed to clear the bar is never reported as NOT_SUPPORTED merely
    because it failed - that would turn "unproven" into "disproven".
    """
    primary = report["policies"][POLICY_UNDER_TEST]
    shadow = report["shadow_replication"].get(POLICY_UNDER_TEST, {})
    path_verdict = primary["verdict"]
    shadow_verdict = shadow.get("verdict", "INSUFFICIENT_DATA")
    pessimistic = primary["pessimistic_fill"]
    pessimistic_agrees = (
        pessimistic["ci_high_usdt"] is not None and pessimistic["ci_high_usdt"] < 0
    ) if path_verdict == "ROBUST_HARM" else None

    verdicts = {path_verdict, shadow_verdict}
    if verdicts == {"ROBUST_HARM"}:
        overall = "WORSE_THAN_BASELINE"
        confidence = "HIGH" if pessimistic_agrees else "MODERATE"
    elif "ROBUST_HARM" in verdicts and not verdicts & {"ROBUST_IMPROVEMENT"}:
        overall = "WORSE_THAN_BASELINE"
        confidence = "MODERATE"
    elif verdicts == {"ROBUST_IMPROVEMENT"}:
        overall = "BETTER_THAN_BASELINE"
        confidence = "HIGH"
    elif verdicts <= {"INSUFFICIENT_DATA"}:
        overall = "INSUFFICIENT_DATA"
        confidence = "NONE"
    else:
        overall = "NOISE"
        confidence = "LOW"
    harm = overall == "WORSE_THAN_BASELINE"
    insufficient = overall == "INSUFFICIENT_DATA"

    # Where do the point estimates point, across both sources and both
    # fill assumptions? Reported, never used to upgrade a verdict.
    direction = _point_direction([
        _float_or_none(primary["paired"]["mean_delta_usdt"]),
        _float_or_none(pessimistic["mean_delta_usdt"]),
        _float_or_none(shadow.get("paired", {}).get("mean_delta_usdt")),
    ])

    # 1. Generally bad: robust harm that is not confined to a minority of
    # the breakdown cells.
    cells = [c for dim in report["breakdowns"]["cells"].values() for c in dim
             if c["n"] >= MIN_CELL and c["mean_delta_usdt"] is not None]
    negative_share = (sum(1 for c in cells if c["mean_delta_usdt"] < 0) / len(cells)
                      if cells else None)
    if insufficient:
        q1 = "INSUFFICIENT_DATA"
    elif harm:
        q1 = "SUPPORTED" if (negative_share or 0) >= 0.75 else "NOT_SUPPORTED"
    elif overall == "BETTER_THAN_BASELINE":
        q1 = "NOT_SUPPORTED"
    else:
        q1 = "NOISE"

    # 2./3. Timing and threshold. The only pre-registered handle on both is
    # the 1.5% variant: activating later must beat 1.0% on the same trades
    # (paired, CI excluding zero). "Too early" additionally needs the
    # early-activation cells to be the harmed ones out of sample.
    oos = report["oos_diagnosis"]
    later = report["threshold_1_5_vs_live"]
    later_known = later["n"] >= MIN_ACTIVATED_FOR_VERDICT
    later_helps = later["ci_low_usdt"] is not None and later["ci_low_usdt"] > 0
    later_hurts = later["ci_high_usdt"] is not None and later["ci_high_usdt"] < 0
    early_cells = {"minutes_to_activation": "<30m", "activation_progress_to_target": "<0.20"}
    timing_statuses = [oos.get(dim, {}).get("status") for dim in early_cells]
    timing_known = any(st in ("REPLICATED", "NOT_REPLICATED") for st in timing_statuses)
    early_replicated = any(
        oos.get(dim, {}).get("status") == "REPLICATED"
        and oos.get(dim, {}).get("train_worst_cell") == cell
        for dim, cell in early_cells.items()
    )
    if not later_known or not timing_known:
        q2 = "INSUFFICIENT_DATA"
    elif early_replicated and later_helps:
        q2 = "SUPPORTED"
    elif later_hurts or not early_replicated:
        q2 = "NOT_SUPPORTED"
    else:
        q2 = "NOISE"

    alt = report["policies"]["BREAKEVEN_AT_1_5PCT"]
    if not later_known:
        q3 = "INSUFFICIENT_DATA"
    elif later_helps and alt["verdict"] != "ROBUST_HARM":
        q3 = "SUPPORTED"
    elif later_hurts:
        q3 = "NOT_SUPPORTED"
    else:
        q3 = "NOISE"

    # 4. Regime-dependent: a regime cell FDR-significant in the full sample
    # AND the train-picked worst regime replicating out of sample.
    regime_cells = report["breakdowns"]["cells"].get("regime", [])
    regime_sig = any(c["fdr_significant"] for c in regime_cells)
    regime_oos = oos.get("regime", {}).get("status")
    if regime_oos in (None, "INSUFFICIENT_DATA"):
        q4 = "INSUFFICIENT_DATA"
    elif regime_sig and regime_oos == "REPLICATED":
        q4 = "SUPPORTED"
    elif regime_oos == "NOT_REPLICATED":
        q4 = "NOT_SUPPORTED"
    else:
        q4 = "NOISE"

    # 5. Actually good, only looks bad on a small sample. Tenable only if
    # the estimates lean positive; when every estimate on both sources is
    # negative there is no evidence of "good" to be hidden by the sample.
    if insufficient:
        q5 = "INSUFFICIENT_DATA"
    elif harm or overall == "BETTER_THAN_BASELINE" or direction == "NEGATIVE":
        q5 = "NOT_SUPPORTED"
    elif direction == "POSITIVE":
        q5 = "SUPPORTED"
    else:
        q5 = "NOISE"

    alternatives = {
        name: block["verdict"] for name, block in report["policies"].items()
        if name != POLICY_UNDER_TEST
    }
    robust_alternative = [
        name for name, verdict in alternatives.items() if verdict == "ROBUST_IMPROVEMENT"
    ]
    damage = primary["effect_delta_usdt"]
    return {
        "overall_verdict": overall,
        "confidence": confidence,
        "direction_of_estimates": direction,
        "path_verdict": path_verdict,
        "shadow_verdict": shadow_verdict,
        "pessimistic_fill_agrees": pessimistic_agrees,
        "q1_generally_bad": q1,
        "q1_negative_cell_share": negative_share,
        "q2_activates_too_early": q2,
        "q3_threshold_wrong": q3,
        "q4_regime_dependent": q4,
        "q5_good_but_small_sample": q5,
        "cause": {
            "winner_stopped_early_usdt": damage.get("WINNER_STOPPED_EARLY", "0"),
            "loss_limited_usdt": damage.get("LOSS_LIMITED", "0"),
            "loser_made_worse_usdt": damage.get("LOSER_MADE_WORSE", "0"),
            "winner_improved_usdt": damage.get("WINNER_IMPROVED", "0"),
        },
        "alternatives": alternatives,
        "robust_alternative": robust_alternative or None,
        "change_live_rules": False,
    }


def evaluation_id(policy: str, run_id: str) -> str:
    return hashlib.sha256(f"{policy}:{run_id}".encode()).hexdigest()


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------


def _money(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.2f}"


def _pct(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.1%}"


def _num(value: object, digits: int = 2) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def render_markdown(report: dict) -> str:
    if "policies" not in report:
        return f"# {POLICY_UNDER_TEST}\n\n{report.get('verdict')}: {report.get('reason')}\n"
    a = report["answers"]
    d = report["data"]
    lines: list[str] = []
    w: Callable[[str], None] = lines.append
    w(f"# Policy evaluation: `{POLICY_UNDER_TEST}`")
    w("")
    w(f"Evaluated {report['evaluated_at']} (run `{report['run_id']}`). Diagnostic only - "
      "**promotion_allowed = false, no live rule was changed.**")
    w("")
    w(f"**Verdict: {a['overall_verdict']}** (confidence {a['confidence']}; "
      f"path data {a['path_verdict']}, candle shadow {a['shadow_verdict']}, "
      f"pessimistic fills agree: {a['pessimistic_fill_agrees']}). Direction of the point "
      f"estimates across both sources and both fill assumptions: "
      f"**{a['direction_of_estimates']}**.")
    w("")
    w(f"Data: {d['scorable_trades_with_path']} closed trades with a real P/L and a real "
      f"price path, entries {d['first_entry'][:16]} .. {d['last_entry'][:16]}; "
      f"train/test cut at {d['train_test_cut'][:16]}; {d['shadow_rows']} shadow rows; "
      f"{d.get('excluded_zero_size_positions', 0)} zero-size (no exposure) positions excluded "
      "from both sources; "
      f"{d['live_activation_rows']} real LIVE activations.")
    w("")
    w("## Answers")
    w("")
    w("| Question | Answer |")
    w("|---|---|")
    w(f"| 1. Policy generally bad | {a['q1_generally_bad']} "
      f"(negative in {_pct(a['q1_negative_cell_share'])} of testable cells) |")
    w(f"| 2. Activates too early | {a['q2_activates_too_early']} |")
    w(f"| 3. Threshold wrong | {a['q3_threshold_wrong']} |")
    w(f"| 4. Only works in some regimes | {a['q4_regime_dependent']} |")
    w(f"| 5. Good, but looks bad on a small sample | {a['q5_good_but_small_sample']} |")
    w(f"| Robust alternative | {a['robust_alternative'] or 'none'} |")
    w("")
    c = a["cause"]
    w(f"Where the money goes (path data, sum of per-trade deltas): winners stopped early "
      f"{_money(c['winner_stopped_early_usdt'])} USDT, losses limited "
      f"{_money(c['loss_limited_usdt'])} USDT, losers made worse "
      f"{_money(c['loser_made_worse_usdt'])} USDT, winners improved "
      f"{_money(c['winner_improved_usdt'])} USDT.")
    w("")

    w("## Policy level (each policy over the trades where it activated)")
    w("")
    w("| | " + " | ".join(report["policies"]) + " |")
    w("|---|" + "---|" * len(report["policies"]))
    blocks = list(report["policies"].values())

    def row(label: str, fn: Callable[[dict], str]) -> None:
        w(f"| {label} | " + " | ".join(fn(b) for b in blocks) + " |")

    row("activated & observable / stopped by new SL",
        lambda b: f"{b['activated']} / {b['stopped_by_new_sl']}")
    row(f"excluded: stop armed across a >{MAX_UNOBSERVED_MINUTES:.0f} min hole",
        lambda b: str(b["excluded_unobservable"]))
    row("baseline total P/L", lambda b: _money(b["baseline"]["total_pnl_usdt"]))
    row("policy total P/L", lambda b: _money(b["with_policy"]["total_pnl_usdt"]))
    row("baseline mean / median", lambda b: f"{_money(b['baseline']['mean_pnl_usdt'])} / "
        f"{_money(b['baseline']['median_pnl_usdt'])}")
    row("policy mean / median", lambda b: f"{_money(b['with_policy']['mean_pnl_usdt'])} / "
        f"{_money(b['with_policy']['median_pnl_usdt'])}")
    row("win rate baseline -> policy", lambda b: f"{_pct(b['baseline']['win_rate'])} -> "
        f"{_pct(b['with_policy']['win_rate'])}")
    row("MFE capture baseline -> policy", lambda b: f"{_num(b['baseline']['mfe_capture'])} -> "
        f"{_num(b['with_policy']['mfe_capture'])}")
    row("mean MAE % baseline -> policy", lambda b: f"{_num(b['baseline_mean_mae_pct'])} -> "
        f"{_num(b['policy_mean_mae_pct'])}")
    row("winners stopped early", lambda b: str(b["effects"].get("WINNER_STOPPED_EARLY", 0)))
    row("losses limited", lambda b: str(b["effects"].get("LOSS_LIMITED", 0)))
    row("neutral (stop never hit)", lambda b: str(b["effects"].get("NEUTRAL", 0)))
    row("stopped, real trade hit target", lambda b: str(b["stopped_then_real_target"]))
    row("uplift total (policy - baseline)", lambda b: _money(b["paired"]["total_delta_usdt"]))
    row("mean uplift [95% bootstrap CI]", lambda b: f"{_money(b['paired']['mean_delta_usdt'])} "
        f"[{_money(b['paired']['ci_low_usdt'])}, {_money(b['paired']['ci_high_usdt'])}]")
    row("pessimistic-fill mean uplift", lambda b: _money(b["pessimistic_fill"]["mean_delta_usdt"]))
    row("p (sign-flip) / sign test", lambda b: f"{b['paired']['p_value']:.4f} / "
        f"{b['paired']['sign_test_p_value']:.4f}")
    row("train mean uplift (n)", lambda b: f"{_money(b['train']['mean_delta_usdt'])} "
        f"({b['train']['n']})")
    row("test mean uplift (n)", lambda b: f"{_money(b['test']['mean_delta_usdt'])} "
        f"({b['test']['n']})")
    row("BH significant (q=0.10, family of 5)", lambda b: str(b["fdr_significant"]))
    row("verdict", lambda b: f"**{b['verdict']}**")
    w("")

    w("## Independent replication: forward-recorded PAPER shadow (1-minute candles)")
    w("")
    w("| | n | baseline total | shadow total | uplift mean [CI] | winners stopped early "
      "| losses limited | train / test mean | verdict |")
    w("|---|---|---|---|---|---|---|---|---|")
    for name, rep in report["shadow_replication"].items():
        p = rep["paired"]
        w(f"| {name} (+{float(rep['threshold_pct']):.1%}) | {p['n']} | "
          f"{_money(rep['baseline_total_usdt'])} | {_money(rep['shadow_total_usdt'])} | "
          f"{_money(p['mean_delta_usdt'])} [{_money(p['ci_low_usdt'])}, "
          f"{_money(p['ci_high_usdt'])}] | {rep['winners_stopped_early']} | "
          f"{rep['losses_limited']} | {_money(rep['train']['mean_delta_usdt'])} / "
          f"{_money(rep['test']['mean_delta_usdt'])} | **{rep['verdict']}** |")
    w("")
    for name, rep in report["shadow_replication"].items():
        w(f"Fidelity {name}: {rep['not_reached_rows']} never-activated shadow rows, "
          f"{rep['not_reached_nonzero_delta']} with a non-zero delta (must be 0).")
    xc = report["source_cross_check"]
    w("")
    w(f"Source overlap: shadow activated {xc['shadow_activated']}, of which the tick path also "
      f"activated {xc['both_activated']} ({xc['shadow_only']} only visible in candles - "
      "the wick a ~97 s tick misses).")
    w("")

    n = report["naive_vs_paired"]
    w("## Policy effect vs. trade selection")
    w("")
    w(f"Naive: activated trades averaged {_money(n['activated_trades_mean_real_pnl_usdt'])} USDT "
      f"real P/L vs {_money(n['never_activated_trades_mean_real_pnl_usdt'])} for trades that "
      f"never reached +1% - a selection effect of {_money(n['selection_effect_usdt'])} USDT "
      "that says the policy is applied to BETTER trades, nothing about the policy. "
      "Paired, same trade with vs without the policy: "
      f"{_money(n['paired_policy_effect_mean_usdt'])} USDT per activated trade.")
    w("")

    m = report["mechanism"]
    w("## Mechanism")
    w("")
    w(f"Of {m['activated']} activated trades, {m['revisited_entry_after_activation']} came back "
      f"to the entry price at some point afterwards; {m['stopped']} were stopped by the new SL, "
      f"{m['stopped_then_made_new_high']} of those later exceeded their pre-intervention high, "
      f"and {m['stopped_then_real_target']} went on to hit the real target. Median original SL "
      f"distance {_num(m['median_sl_distance_pct'])}%, so a +1% trigger sits at roughly "
      f"{_num(1 / m['median_sl_distance_pct'] if m['median_sl_distance_pct'] else None)} R - "
      "inside ordinary noise for these instruments.")
    w("")
    for label, key in (("1.5% vs live 1.0%", "threshold_1_5_vs_live"),
                       ("profit lock vs live", "profit_lock_vs_live")):
        p = report[key]
        w(f"- Paired {label}: n={p['n']}, mean {_money(p['mean_delta_usdt'])} "
          f"[{_money(p['ci_low_usdt'])}, {_money(p['ci_high_usdt'])}], p={p['p_value']:.4f} "
          "(descriptive, not in the confirmatory family).")
    w("")

    w("## Exploratory breakdowns (live policy, BH across "
      f"{report['breakdowns']['family_size']} cells with n >= {MIN_CELL})")
    w("")
    w("| dimension | value | n | baseline mean | uplift mean [CI] | status |")
    w("|---|---|---|---|---|---|")
    for dim, cells in report["breakdowns"]["cells"].items():
        for cell in cells:
            w(f"| {dim} | {cell['value']} | {cell['n']} | "
              f"{_money(cell['baseline_mean_pnl_usdt'])} | {_money(cell['mean_delta_usdt'])} "
              f"[{_money(cell['ci_low_usdt'])}, {_money(cell['ci_high_usdt'])}] | "
              f"{cell['status']} |")
    w("")
    w("## Out-of-sample check of the diagnosis (worst cell picked on TRAIN, checked on TEST)")
    w("")
    w("| dimension | train-worst cell | train mean | test mean in cell | test mean rest | status |")
    w("|---|---|---|---|---|---|")
    for dim, res in report["oos_diagnosis"].items():
        w(f"| {dim} | {res.get('train_worst_cell', '-')} | "
          f"{_money(res.get('train_mean_in_cell'))} | {_money(res.get('test_mean_in_cell'))} | "
          f"{_money(res.get('test_mean_rest'))} | {res['status']} |")
    w("")

    live = report["live_activations"]
    w("## Real LIVE activations (exchange) - descriptive, "
      f"{live['verdict']}")
    w("")
    w(f"{live['n']} real activations; {live['stopped_near_breakeven']} ended as a stop at "
      f"break-even, {live['stopped_near_breakeven_while_paper_hit_target']} of them while the "
      f"PAPER twin (original SL) hit its target. Mean live-minus-paper return on the "
      f"{live['comparable_with_paper']} comparable trades: "
      f"{_num(live['mean_live_minus_paper_return_pct'])} percentage points.")
    w("")
    w("| position | PP status | live exit | live % | paper exit | paper % |")
    w("|---|---|---|---|---|---|")
    for t in live["trades"]:
        w(f"| {t['position_id'][:10]} | {t['status']} | {t['live_exit_reason']} | "
          f"{t['live_return_pct']} | {t['paper_exit_reason']} | {t['paper_return_pct']} |")
    w("")

    w("## Per-trade record (live policy; every activated trade)")
    w("")
    w("| entry | instrument | activated after | MFE before | MAE before | stopped | "
      "MFE after | MAE after | real exit | actual = original SL | break-even | BE 1.5% "
      "| profit lock | effect |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for t in report["trades"]:
        w(f"| {t['entry_time'][:16]} @ {float(t['entry_price']):.6g} | {t['instrument']} | "
          f"{_num(t['minutes_to_activation'], 0)}m | {_num(t['mfe_before_pct'])}% | "
          f"{_num(t['mae_before_pct'])}% | {'yes' if t['stopped_by_new_sl'] else 'no'} | "
          f"{_num(t['mfe_after_pct'])}% | {_num(t['mae_after_pct'])}% | "
          f"{t['real_exit_reason']} | "
          f"{_money(t['actual_pnl_usdt'])} | {_money(t['breakeven_pnl_usdt'])} | "
          f"{_money(t['breakeven_1_5pct_pnl_usdt'])} | {_money(t['profit_lock_pnl_usdt'])} | "
          f"{t['effect']} |")
    w("")
    w("Columns 9 (no intervention) and 11 (original SL) of the request are the same number "
      "by construction: the PAPER book never moved its stop, so its real outcome IS the "
      "original-SL outcome.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------
# Runner: real history in, one stored report out
# ---------------------------------------------------------------------


def _strata_for(repo: Repository, settings: Settings, position: Position,
                observations: list[dict]) -> dict[str, str]:
    """Signal type, entry quality and regime - all known before entry.
    Entry quality is the advisory score WITHOUT history or audit input,
    so it cannot smuggle in anything learned from this same book."""
    regime = _regime_for(observations)
    candidate = _safe_candidate(repo, position.candidate_id)
    strata = {"regime": regime}
    if candidate is None:
        return strata
    opportunity_screen = repo.get_assessment_payload(position.candidate_id, "opportunity_screen")
    features = build_candidate_features(candidate, opportunity_screen, position.opened_at, regime)
    strata["signal_type"] = str(features.get("trigger_reasons_key", "unknown"))
    assessment = assess_entry_quality(
        candidate=candidate,
        features=features,
        conflicts=[],
        experience_patterns=[],
        planned_entry=position.simulated_fill_entry,
        stop_loss=position.stop_loss,
        target=position.target,
        size=position.size,
        risk_limits=settings.risk_limits,
        now=position.opened_at,
        run_id="policy_evaluation",
        regime_compatible=(None if regime == "unknown" else regime in ("btc_strong", "btc_ok")),
    )
    strata["entry_quality"] = assessment.verdict
    return strata


def _live_rows(repo: Repository) -> list[dict]:
    rows = []
    for pp in repo.find_all_live_profit_protection():
        execution = repo.get_live_execution(pp["position_id"]) or {}
        position = repo.get_position(pp["position_id"])
        rows.append({
            **pp,
            "exchange_fill_entry": execution.get("exchange_fill_entry"),
            "exchange_fill_exit": execution.get("exchange_fill_exit"),
            "live_exit_reason": execution.get("exit_reason"),
            "paper_entry": position.simulated_fill_entry if position else None,
            "paper_exit": position.simulated_fill_exit if position else None,
            "paper_exit_reason": position.exit_reason if position else None,
        })
    return rows


def run_policy_evaluation(
    repo: Repository, settings: Settings, now: datetime, run_id: str, persist: bool = True
) -> dict:
    trades: list[TradeEvaluation] = []
    zero_size: set[str] = set()
    for position in repo.find_closed_positions():
        if position.size == _ZERO:
            zero_size.add(position.position_id)
            continue
        observations = repo.find_guardian_observations_for_position(position.position_id)
        points = reconstruct_price_path(position, observations)
        if not points:
            continue
        record = evaluate_trade(
            position, points, settings.risk_limits,
            strata=_strata_for(repo, settings, position, observations),
            thresholds=_thresholds(settings),
        )
        if record is not None:
            trades.append(record)
    report = evaluate_policy(
        trades, repo.find_all_profit_protection_shadows(), _live_rows(repo), now, run_id,
        excluded_position_ids=frozenset(zero_size),
    )
    if persist:
        primary = report.get("policies", {}).get(POLICY_UNDER_TEST, {})
        repo.save_godfather_policy_evaluation(
            evaluation_id=evaluation_id(POLICY_UNDER_TEST, run_id),
            policy=POLICY_UNDER_TEST,
            evaluated_at=now,
            verdict=report["verdict"],
            confidence=report.get("confidence", "NONE"),
            activated_trades=primary.get("activated", 0),
            mean_uplift_usdt=primary.get("paired", {}).get("mean_delta_usdt"),
            report=report,
            run_id=run_id,
        )
    return report


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    from datetime import UTC
    from pathlib import Path

    from crypto_trading.config.loader import get_settings
    from crypto_trading.logging import new_run_id
    from crypto_trading.storage.repository import SQLiteRepository

    parser = argparse.ArgumentParser(description=f"Evaluate {POLICY_UNDER_TEST} on real history")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    settings = get_settings()
    repo = SQLiteRepository(settings.db_path)
    report = run_policy_evaluation(
        repo, settings, datetime.now(UTC), new_run_id(), persist=not args.no_persist
    )
    text = render_markdown(report)
    if args.markdown:
        args.markdown.write_text(text, encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
