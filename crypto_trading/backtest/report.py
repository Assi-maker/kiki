"""Tier 1 statistics report: median P/L, bootstrap CI, baseline-parity
cross-check, and the per-position audit table, layered on top of the
already-shipped `profit_protection_report.build_report()` (reused
verbatim - never copied/modified). See
.superpowers/sdd/2026-09-12-profit-protection-tier1-historical-replay/
task-6-brief.md."""

from __future__ import annotations

import random
from decimal import Decimal

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _shadow_id,
)
from crypto_trading.performance.profit_protection_report import build_report
from crypto_trading.storage.repository import Repository

_PER_POSITION_COLUMNS = sorted([
    "position_id", "instrument", "entry", "threshold", "threshold_reached",
    "mfe", "mae", "baseline_exit", "baseline_pnl", "shadow_exit", "shadow_pnl",
    "pnl_difference",
])


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _bootstrap_ci(
    values: list[Decimal],
    resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int | None = None,
) -> tuple[Decimal, Decimal] | None:
    """Percentile bootstrap on the sample mean. Pure Python, no numpy -
    resamples `values` WITH replacement `resamples` times, computes the
    mean each time, returns the (2.5th, 97.5th) percentile of that
    distribution for a 95% CI. `seed` makes a specific call reproducible
    for tests; the real report run uses no seed (system entropy) since
    the underlying DATA is already fixed/cached (Task 3) - only the
    resampling order varies run to run, which is expected and standard
    for a bootstrap, not a determinism violation of the replay itself."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(resamples):
        sample = rng.choices(values, k=n)
        means.append(sum(sample, Decimal("0")) / n)
    means.sort()
    lower_idx = int((1 - confidence) / 2 * resamples)
    upper_idx = int((1 + confidence) / 2 * resamples) - 1
    return means[lower_idx], means[upper_idx]


def _baseline_parity_mismatches(repo: Repository, targets: list[BacktestTarget]) -> list[dict]:
    mismatches = []
    for target in targets:
        if target.original_status != "CLOSED":
            continue
        replayed = repo.get_position(target.position_id)
        if replayed is None or replayed.status != "CLOSED":
            continue  # not in this repo (train vs test split) or didn't close in the replay window
        if replayed.exit_reason != target.original_exit_reason:
            mismatches.append({
                "position_id": target.position_id,
                "replayed_exit_reason": replayed.exit_reason,
                "production_exit_reason": target.original_exit_reason,
            })
    return mismatches


def _per_position_rows(repo: Repository, targets: list[BacktestTarget]) -> list[dict]:
    rows = []
    for target in targets:
        replayed = repo.get_position(target.position_id)
        if replayed is None:
            continue
        for threshold_pct in FROZEN_THRESHOLDS_PCT:
            shadow_id = _shadow_id(target.position_id, threshold_pct)
            shadow = repo.get_profit_protection_shadow(shadow_id)
            if shadow is None:
                continue
            rows.append({
                "position_id": target.position_id,
                "instrument": target.instrument,
                "entry": str(target.entry_price),
                "threshold": str(threshold_pct),
                "threshold_reached": bool(shadow["threshold_reached"]),
                "mfe": shadow["mfe"],
                "mae": shadow["mae"],
                "baseline_exit": replayed.exit_reason,
                "baseline_pnl": (
                    str(_baseline_pnl(replayed)) if replayed.status == "CLOSED" else None
                ),
                "shadow_exit": shadow["exit_reason"],
                "shadow_pnl": shadow["shadow_realized_pnl"],
                "pnl_difference": shadow["pnl_difference"],
            })
    return rows


def _baseline_pnl(position) -> Decimal:
    return compute_pnl(position)


def _split_report_with_extras(repo: Repository, targets: list[BacktestTarget]) -> dict:
    base = build_report(repo)
    for block in base["per_threshold"].values():
        shadow_pnls = [
            Decimal(t["profit_protection_hypothetical_pnl"]) for t in block["trades"]
            if t["profit_protection_hypothetical_pnl"] is not None
            and t["reach_classification"] != "blocked_by_exposure"
        ]
        baseline_pnls = [
            Decimal(t["baseline_actual_pnl"]) for t in block["trades"]
            if t["baseline_actual_pnl"] is not None
            and t["reach_classification"] != "blocked_by_exposure"
        ]
        pnl_diffs = [
            Decimal(t["pnl_difference"]) for t in block["trades"]
            if t["pnl_difference"] is not None
            and t["reach_classification"] != "blocked_by_exposure"
        ]
        # Paired subset (final whole-branch review, Critical Fix 2). The
        # two lists above mirror `build_report()`'s own (reused,
        # unmodified) `shadow_total_pnl_usdt`/`baseline_total_pnl_usdt`:
        # independently filtered, never paired by row. Under any
        # right-censoring - a shadow that closed while its baseline is
        # still open - that compares two DIFFERENT samples, and the sign
        # of the headline number becomes a sampling artifact. Real-run
        # evidence: train@1.0% unpaired read "shadow 201.54 vs baseline
        # 219.50" (PP looks 17.96 USDT worse); the same 24 PAIRED trades
        # read "shadow 236.57 vs baseline 219.50" (PP is 17.07 USDT
        # better). These fields are strictly ADDITIVE - every existing
        # field above keeps its old, unpaired value so nothing that
        # already consumes this report changes meaning. The existing
        # `pnl_diffs`/bootstrap CI is already correctly paired by
        # construction (a trade's `pnl_difference` is only non-null once
        # both sides are known) and is deliberately left untouched.
        paired = [
            t for t in block["trades"]
            if t["baseline_actual_pnl"] is not None
            and t["profit_protection_hypothetical_pnl"] is not None
            and t["reach_classification"] != "blocked_by_exposure"
        ]
        paired_shadow_pnls = [Decimal(t["profit_protection_hypothetical_pnl"]) for t in paired]
        paired_baseline_pnls = [Decimal(t["baseline_actual_pnl"]) for t in paired]
        paired_shadow_median = _median(paired_shadow_pnls)
        paired_baseline_median = _median(paired_baseline_pnls)
        block["n_paired"] = len(paired)
        block["n_baseline_pending"] = sum(
            1 for t in block["trades"]
            if t["profit_protection_hypothetical_pnl"] is not None
            and t["baseline_actual_pnl"] is None
            and t["reach_classification"] != "blocked_by_exposure"
        )
        block["paired_shadow_total_pnl_usdt"] = str(sum(paired_shadow_pnls, Decimal("0")))
        block["paired_baseline_total_pnl_usdt"] = str(sum(paired_baseline_pnls, Decimal("0")))
        block["paired_shadow_median_pnl_usdt"] = (
            str(paired_shadow_median) if paired_shadow_median is not None else None
        )
        block["paired_baseline_median_pnl_usdt"] = (
            str(paired_baseline_median) if paired_baseline_median is not None else None
        )

        shadow_median = _median(shadow_pnls)
        baseline_median = _median(baseline_pnls)
        block["shadow_median_pnl_usdt"] = str(shadow_median) if shadow_median is not None else None
        block["baseline_median_pnl_usdt"] = (
            str(baseline_median) if baseline_median is not None else None
        )
        ci = _bootstrap_ci(pnl_diffs)
        block["pnl_difference_95pct_bootstrap_ci"] = (
            [str(ci[0]), str(ci[1])] if ci is not None else None
        )
    return base


def build_tier1_report(
    train_repo: Repository, test_repo: Repository, source_repo: Repository,
    targets: list[BacktestTarget],
) -> dict:
    """`source_repo` is accepted for interface symmetry with `replay_
    position` and future extension (e.g. cross-checking against source
    Guardian history) but this function itself never re-splits targets by
    date - Task 7 decides train-vs-test BEFORE calling replay_position,
    so train_repo/test_repo already only ever contain the positions
    routed into them at replay time; this function only reports on
    whatever each repo already holds."""
    train_mismatches = _baseline_parity_mismatches(train_repo, targets)
    test_mismatches = _baseline_parity_mismatches(test_repo, targets)
    train_rows = _per_position_rows(train_repo, targets)
    test_rows = _per_position_rows(test_repo, targets)
    return {
        "train": _split_report_with_extras(train_repo, targets),
        "test": _split_report_with_extras(test_repo, targets),
        "baseline_parity_mismatches": train_mismatches + test_mismatches,
        "per_position_table": train_rows + test_rows,
        "per_position_table_columns": _PER_POSITION_COLUMNS,
    }
