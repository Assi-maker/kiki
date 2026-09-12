"""Read-only Profit Protection experiment report (2026-09-11). See
docs/superpowers/specs/2026-09-11-profit-protection-experiment-design.md.

Never writes to the DB, never started by run.py - run manually:
`python -m crypto_trading.performance.profit_protection_report`.

Spec G9 / plan correction: +1.0% and +1.5% are frozen, pre-registered
hypotheses. This report never selects a winner or recommends promotion to
production - see the fixed `note` field, always present regardless of
data."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.paper_trading.profit_protection_experiment import FROZEN_THRESHOLDS_PCT
from crypto_trading.performance.metrics import (
    compute_expectancy,
    compute_profit_factor,
    compute_win_rate,
)
from crypto_trading.storage.repository import Repository, SQLiteRepository

_BREAKEVEN_BAND_PCT = Decimal("0.003")

_NOTE = (
    "Pre-registered hypotheses under test: +1.0% and +1.5%. This report "
    "never selects a winner or recommends promotion to production - that "
    "is a separate, later, explicit human decision."
)


def _classify_reach(row: dict, position_size: Decimal) -> str:
    """Reporting-only classification (spec §7.2, G9 addendum) - never
    feeds back into simulation, PnL, or any comparison."""
    if not row["threshold_reached"]:
        return "never_reached_threshold"
    if row["hypothetical_baseline_pnl"] is None:
        return "reached_threshold_baseline_pending"
    baseline_pnl = Decimal(row["hypothetical_baseline_pnl"])
    if baseline_pnl < 0:
        return "reached_threshold_baseline_loss"
    if abs(baseline_pnl / position_size) <= _BREAKEVEN_BAND_PCT:
        return "reached_threshold_baseline_approx_breakeven"
    if row["hypothetical_baseline_exit_reason"] == "target":
        return "reached_threshold_baseline_big_winner"
    return "reached_threshold_baseline_moderate_gain"


def _conversion_ratio(row: dict, position_size: Decimal, entry_price: Decimal) -> Decimal | None:
    """realized_pnl_pct / mfe_pct - dimensionless, comparable across
    instruments (corrected definition, see spec commit 5f3b7a9)."""
    mfe = Decimal(row["mfe"])
    if mfe <= 0:
        return None
    mfe_pct = mfe / entry_price
    realized_pnl_pct = Decimal(row["shadow_realized_pnl"]) / position_size
    return realized_pnl_pct / mfe_pct


def _sample_sizes(rows: list[dict]) -> dict:
    n_closed = len(rows)
    n_reached = sum(1 for r in rows if r["threshold_reached"])
    n_baseline_losses = sum(
        1 for r in rows
        if r["threshold_reached"] and r["hypothetical_baseline_pnl"] is not None
        and Decimal(r["hypothetical_baseline_pnl"]) < 0
    )
    n_baseline_target_winners = sum(
        1 for r in rows
        if r["threshold_reached"] and r["hypothetical_baseline_exit_reason"] == "target"
        and r["hypothetical_baseline_pnl"] is not None
        and Decimal(r["hypothetical_baseline_pnl"]) > 0
    )
    n_shadow_winners = sum(
        1 for r in rows
        if r["shadow_realized_pnl"] is not None and Decimal(r["shadow_realized_pnl"]) > 0
    )
    n_shadow_losses = sum(
        1 for r in rows
        if r["shadow_realized_pnl"] is not None and Decimal(r["shadow_realized_pnl"]) < 0
    )
    return {
        "n_closed": n_closed,
        "n_reached_threshold": n_reached,
        "n_not_reached": n_closed - n_reached,
        "n_baseline_losses_after_threshold": n_baseline_losses,
        "n_baseline_target_winners_after_threshold": n_baseline_target_winners,
        "n_shadow_winners": n_shadow_winners,
        "n_shadow_losses": n_shadow_losses,
    }


def _max_drawdown(pnls_in_order: list[Decimal]) -> Decimal | None:
    if not pnls_in_order:
        return None
    running = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for pnl in pnls_in_order:
        running += pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    return max_dd


def _outcome_label(row: dict) -> str:
    if row["pnl_difference"] is None:
        return "pending"
    diff = Decimal(row["pnl_difference"])
    baseline_pnl = Decimal(row["hypothetical_baseline_pnl"])
    shadow_pnl = Decimal(row["shadow_realized_pnl"])
    if diff == 0:
        return "protection_no_change"
    if baseline_pnl < 0 and shadow_pnl >= 0:
        return "protection_saved_a_loss"
    if row["hypothetical_baseline_exit_reason"] == "target" and diff < 0:
        return "protection_clipped_a_winner"
    return "protection_improved_other" if diff > 0 else "protection_worsened_other"


def _stats_block(rows: list[dict], repo: Repository) -> dict:
    closed = [r for r in rows if r["status"] == "CLOSED"]
    sample_sizes = _sample_sizes(closed)

    shadow_pnls, baseline_pnls, ratios, ratios_excluded = [], [], [], 0
    trades = []
    reach_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}

    for row in closed:
        real_position = repo.get_position(row["position_id"])
        position_size = real_position.size if real_position is not None else Decimal("1")
        entry_price = Decimal(row["entry_price"])

        bucket = _classify_reach(row, position_size)
        reach_counts[bucket] = reach_counts.get(bucket, 0) + 1

        if row["shadow_realized_pnl"] is not None:
            shadow_pnls.append(Decimal(row["shadow_realized_pnl"]))
        if row["hypothetical_baseline_pnl"] is not None:
            baseline_pnls.append(Decimal(row["hypothetical_baseline_pnl"]))

        ratio = _conversion_ratio(row, position_size, entry_price)
        if ratio is None:
            ratios_excluded += 1
        else:
            ratios.append(ratio)

        outcome = _outcome_label(row)
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

        trades.append({
            "position_id": row["position_id"],
            "baseline_actual_exit_reason": row["hypothetical_baseline_exit_reason"],
            "baseline_actual_pnl": row["hypothetical_baseline_pnl"],
            "profit_protection_hypothetical_exit_reason": row["exit_reason"],
            "profit_protection_hypothetical_pnl": row["shadow_realized_pnl"],
            "pnl_difference": row["pnl_difference"],
            "outcome_label": outcome,
            "reach_classification": bucket,
        })

    improved = sum(1 for r in closed if r["pnl_difference"] is not None and Decimal(r["pnl_difference"]) > 0)
    worsened = sum(1 for r in closed if r["pnl_difference"] is not None and Decimal(r["pnl_difference"]) < 0)
    loss_saved = sum(
        1 for r in closed
        if r["hypothetical_baseline_pnl"] is not None and r["shadow_realized_pnl"] is not None
        and Decimal(r["hypothetical_baseline_pnl"]) < 0 and Decimal(r["shadow_realized_pnl"]) >= 0
    )
    winner_clipped = sum(
        1 for r in closed
        if r["hypothetical_baseline_exit_reason"] == "target"
        and r["exit_reason"] != "target"
        and r["pnl_difference"] is not None and Decimal(r["pnl_difference"]) < 0
    )

    return {
        "sample_sizes": sample_sizes,
        "reach_classification_counts": reach_counts,
        "profit_protection_improved_pl": {"count": improved, "total_usdt": str(sum(
            (Decimal(r["pnl_difference"]) for r in closed if r["pnl_difference"] is not None
             and Decimal(r["pnl_difference"]) > 0), Decimal("0")))},
        "profit_protection_worsened_pl": {"count": worsened, "total_usdt": str(sum(
            (Decimal(r["pnl_difference"]) for r in closed if r["pnl_difference"] is not None
             and Decimal(r["pnl_difference"]) < 0), Decimal("0")))},
        "loss_saved_count": loss_saved,
        "large_winner_clipped_count": winner_clipped,
        "outcome_label_counts": outcome_counts,
        "shadow_total_pnl_usdt": str(sum(shadow_pnls, Decimal("0"))),
        "shadow_win_rate": str(compute_win_rate(shadow_pnls)) if compute_win_rate(shadow_pnls) is not None else None,
        "shadow_expectancy_usdt": str(compute_expectancy(shadow_pnls)) if compute_expectancy(shadow_pnls) is not None else None,
        "shadow_profit_factor": str(compute_profit_factor(shadow_pnls)) if compute_profit_factor(shadow_pnls) is not None else None,
        "shadow_max_drawdown_usdt": str(_max_drawdown(shadow_pnls)) if _max_drawdown(shadow_pnls) is not None else None,
        "baseline_total_pnl_usdt": str(sum(baseline_pnls, Decimal("0"))),
        "baseline_win_rate": str(compute_win_rate(baseline_pnls)) if compute_win_rate(baseline_pnls) is not None else None,
        "baseline_expectancy_usdt": str(compute_expectancy(baseline_pnls)) if compute_expectancy(baseline_pnls) is not None else None,
        "conversion_ratio_avg": str(sum(ratios, Decimal("0")) / len(ratios)) if ratios else None,
        "conversion_ratio_excluded_count": ratios_excluded,
        "trades": trades,
    }


def build_report(repo: Repository) -> dict:
    all_rows = repo.find_all_profit_protection_shadows()
    per_threshold = {}
    for threshold_pct in FROZEN_THRESHOLDS_PCT:
        key = str(threshold_pct)
        rows_for_threshold = [r for r in all_rows if r["threshold_pct"] == key]
        rows_for_threshold.sort(key=lambda r: r["opened_at"])
        midpoint = len(rows_for_threshold) // 2
        per_threshold[key] = {
            **_stats_block(rows_for_threshold, repo),
            "chronological_split": {
                "first_half": _stats_block(rows_for_threshold[:midpoint], repo),
                "second_half": _stats_block(rows_for_threshold[midpoint:], repo),
            },
        }

    combined_closed = [r for r in all_rows if r["status"] == "CLOSED"]
    combined = {
        "note_on_combined": (
            "Row-level counts pooled across both thresholds - a single "
            "real position that produced two closed shadow rows (one per "
            "threshold) contributes two rows here, this is not a "
            "deduplicated position count."
        ),
        "sample_sizes": _sample_sizes(combined_closed),
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "note": _NOTE,
        "per_threshold": per_threshold,
        "combined": combined,
    }


def main() -> None:
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    report = build_report(repo)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
