"""The GODFATHER Intelligence report.

Requirement 14's closing list, as one callable: what data each component
used, what sample sizes exist, which patterns were found, which
conclusions are robust and which are only noise, and which decisions
GODFATHER could later improve.

Everything here is a read. `build_report` opens nothing, writes nothing
and decides nothing - it renders what the analysis tables already say.
Which is also why it is the right place to be blunt: a report that
rounds "we have 11 samples" up to a conclusion is worse than no report,
so every section states its own sample size and every claim that has not
cleared significance testing is labelled as not having cleared it.
"""

from __future__ import annotations

import json
from decimal import Decimal

from crypto_trading.godfather.counterfactual import (
    aggregate_policy_performance,
    assess_policy_significance,
    common_scorable_positions,
)
from crypto_trading.godfather.objective import TradeOutcome, evaluate_objective
from crypto_trading.godfather.prediction_error import summarise_prediction_errors
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _outcomes(repo: Repository, investigations: list[dict]) -> list[TradeOutcome]:
    outcomes: list[TradeOutcome] = []
    for row in investigations:
        pnl = _decimal(row.get("realized_pnl_usdt"))
        if pnl is None:
            continue
        detail = _detail(row)
        after = detail.get("after", {})
        before = detail.get("before", {})
        position = repo.get_position(str(row["position_id"]))
        outcomes.append(
            TradeOutcome(
                pnl=pnl,
                fees=_decimal(after.get("fees_usdt")) or _ZERO,
                funding=_decimal(after.get("funding_usdt")) or _ZERO,
                mfe_pnl=_decimal(detail.get("during", {}).get("mfe_pnl_usdt")),
                mae_pnl=_decimal(detail.get("during", {}).get("mae_pnl_usdt")),
                notional=(position.size if position is not None else _ZERO),
                hold_minutes=after.get("hold_minutes"),
                entry_slippage_pct=_decimal(before.get("entry_slippage_pct")),
                exit_slippage_pct=_decimal(after.get("exit_slippage_pct")),
            )
        )
    return outcomes


def _detail(row: dict) -> dict:
    try:
        parsed = json.loads(row.get("detail_json") or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_report(repo: Repository) -> dict:
    """The whole picture, as structured data."""
    investigations = repo.find_godfather_trade_investigations()
    audits = repo.find_godfather_decision_audits()
    counterfactuals = repo.find_godfather_counterfactuals()
    patterns = repo.find_godfather_experience_patterns()
    prediction_errors = repo.find_godfather_prediction_errors()
    entry_quality = repo.find_godfather_entry_quality_assessments()

    # Two different questions, two different sample sets - conflating
    # them is what made the first version of this report empty whenever
    # one policy happened to be unscorable everywhere.
    #
    #   "Does policy P improve the trades P can act on?" is a WITHIN-policy
    #   question, and its correct sample is P's own scorable set.
    #
    #   "Is P better than Q?" is a BETWEEN-policy question, and its
    #   correct sample is the intersection - otherwise the policy that
    #   quietly opted out of the hard trades wins by default.
    common = common_scorable_positions(counterfactuals)
    policy_effect = aggregate_policy_performance(counterfactuals)
    policy_significance = assess_policy_significance(counterfactuals)
    comparable_effect = aggregate_policy_performance(counterfactuals, restrict_to=common)

    outcomes = _outcomes(repo, investigations)
    objective = evaluate_objective(outcomes)

    component_scores: dict[str, dict[str, int]] = {}
    conflict_counts: dict[str, int] = {}
    missing_counts: dict[str, int] = {}
    for audit in audits:
        for component in json.loads(audit.get("components_json") or "[]"):
            bucket = component_scores.setdefault(
                str(component.get("component")),
                {"RIGHT": 0, "WRONG": 0, "UNSCORABLE": 0},
            )
            verdict = str(component.get("verdict"))
            if verdict in bucket:
                bucket[verdict] += 1
        for code in json.loads(audit.get("conflicts_json") or "[]"):
            conflict_counts[str(code)] = conflict_counts.get(str(code), 0) + 1
        for code in json.loads(audit.get("missing_information_json") or "[]"):
            missing_counts[str(code)] = missing_counts.get(str(code), 0) + 1

    pnl_by_candidate = {
        str(row["candidate_id"]): _decimal(row.get("realized_pnl_usdt"))
        for row in investigations
        if row.get("realized_pnl_usdt") is not None
    }

    return {
        "coverage": {
            "investigations": len(investigations),
            "with_scorable_pnl": len(outcomes),
            "decision_audits": len(audits),
            "counterfactual_rows": len(counterfactuals),
            "positions_comparable_across_all_policies": len(common),
            "experience_patterns": len(patterns),
            "prediction_errors": len(prediction_errors),
            "entry_quality_assessments": len(entry_quality),
            "still_pending_investigation": (
                repo.count_closed_positions_pending_godfather_investigation()
            ),
        },
        "objective": {
            "trade_count": objective.trade_count,
            "net_pnl_usdt": str(objective.net_pnl_usdt),
            "expectancy_usdt": _str_or_none(objective.expectancy_usdt),
            "profit_factor": _str_or_none(objective.profit_factor),
            "win_rate": objective.win_rate,
            "max_drawdown_usdt": str(objective.max_drawdown_usdt),
            "avg_loss_usdt": _str_or_none(objective.avg_loss_usdt),
            "worst_loss_usdt": _str_or_none(objective.worst_loss_usdt),
            "loss_severity_ratio": _str_or_none(objective.loss_severity_ratio),
            "mfe_capture_ratio": _str_or_none(objective.mfe_capture_ratio),
            "avg_mae_usdt": _str_or_none(objective.avg_mae_usdt),
            "capital_efficiency_usdt_per_1k_hour": _str_or_none(
                objective.capital_efficiency_usdt_per_1k_hour
            ),
            "total_costs_usdt": str(objective.total_costs_usdt),
            "cost_share_of_gross": _str_or_none(objective.cost_share_of_gross),
            "avg_abs_slippage_pct": _str_or_none(objective.avg_abs_slippage_pct),
            "turnover_notional_usdt": str(objective.turnover_notional_usdt),
            "composite_score": objective.composite_score,
        },
        "classification_breakdown": _count_by(investigations, "classification"),
        "entry_verdicts": _count_by(investigations, "entry_verdict"),
        "management_verdicts": _count_by(investigations, "management_verdict"),
        "fault_domains": _count_by(audits, "fault_domain"),
        "component_scoreboard": component_scores,
        "pre_entry_conflicts": dict(
            sorted(conflict_counts.items(), key=lambda kv: -kv[1])
        ),
        "missing_information": dict(sorted(missing_counts.items(), key=lambda kv: -kv[1])),
        "counterfactual_policies": {
            policy: {
                "positions_scored": data["n"],
                "times_triggered": data["triggered"],
                "total_delta_usdt": str(data["total_delta_usdt"]),
                "delta_on_winning_trades_usdt": str(data["winner_delta_usdt"]),
                "delta_on_losing_trades_usdt": str(data["loser_delta_usdt"]),
                "trades_improved": data["improved"],
                "trades_worsened": data["worsened"],
                "damages_winners": Decimal(str(data["winner_delta_usdt"])) < _ZERO,
                # The raw totals above are descriptive only. This is the
                # part that says whether any of it is real.
                "significance": policy_significance.get(policy),
            }
            for policy, data in sorted(
                policy_effect.items(),
                key=lambda kv: Decimal(str(kv[1]["total_delta_usdt"])),
                reverse=True,
            )
        },
        "counterfactual_comparison": {
            "comparable_positions": len(common),
            "note": (
                "Cross-policy ranking uses ONLY positions every policy could score. "
                "A policy that cannot be evaluated on the fastest-resolving trades "
                "would otherwise be ranked on an easier book than the rest."
            ),
            "ranking": [
                {
                    "policy": policy,
                    "total_delta_usdt": str(data["total_delta_usdt"]),
                    "delta_on_winning_trades_usdt": str(data["winner_delta_usdt"]),
                }
                for policy, data in sorted(
                    comparable_effect.items(),
                    key=lambda kv: Decimal(str(kv[1]["total_delta_usdt"])),
                    reverse=True,
                )
            ],
        },
        "experience_memory": {
            "by_edge_class": _count_by(patterns, "edge_class"),
            "robust_conclusions": [
                _pattern_summary(row)
                for row in patterns
                if str(row.get("edge_class")) in ("EDGE", "FAILURE_PATTERN")
            ],
            "not_yet_conclusive": [
                _pattern_summary(row)
                for row in patterns
                if str(row.get("edge_class"))
                in ("WEAK_EDGE", "REGIME_DEPENDENT", "DECAYING_EDGE")
            ],
            "insufficient_data_count": sum(
                1 for row in patterns if str(row.get("edge_class")) == "INSUFFICIENT_DATA"
            ),
            "noise_count": sum(1 for row in patterns if str(row.get("edge_class")) == "NOISE"),
            "largest_sample_size": max(
                (int(row.get("sample_size") or 0) for row in patterns), default=0
            ),
        },
        "prediction_errors": summarise_prediction_errors(prediction_errors),
        "entry_quality_backtest": _entry_quality_backtest(entry_quality, pnl_by_candidate),
        "avoidable_losses": _avoidable_losses(investigations),
    }


def _str_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def money(value: object) -> str:
    """Four decimal places for DISPLAY only.

    Stored values keep full Decimal precision; this exists purely so a
    report does not print `0E-30` or thirty significant digits of a USDT
    amount, which is unreadable and implies a precision the underlying
    measurement does not have.
    """
    if value is None:
        return "n/a"
    try:
        return f"{Decimal(str(value)):.4f}"
    except (ValueError, ArithmeticError):
        return str(value)


def _pattern_summary(row: dict) -> dict:
    return {
        "pattern_id": row.get("pattern_id"),
        "edge_class": row.get("edge_class"),
        "sample_size": row.get("sample_size"),
        "win_rate": row.get("win_rate"),
        "baseline_win_rate": row.get("baseline_win_rate"),
        "expectancy_usdt": row.get("expectancy_usdt"),
        "lift_expectancy_usdt": row.get("lift_expectancy_usdt"),
        "p_value": row.get("p_value"),
        "fdr_significant": bool(row.get("fdr_significant")),
        "survived_walk_forward": bool(row.get("survived_walk_forward")),
        "confidence": row.get("confidence"),
    }


def _entry_quality_backtest(
    assessments: list[dict], pnl_by_candidate: dict[str, Decimal | None]
) -> dict:
    """What the advisory entry filter would have done to the real book.

    Uses the stored rows directly rather than re-running the layer, so
    this reflects the verdicts that were actually recorded - a filter
    evaluated against a freshly recomputed version of itself proves
    nothing.
    """
    buckets: dict[str, dict] = {}
    for row in assessments:
        pnl = pnl_by_candidate.get(str(row.get("candidate_id")))
        if pnl is None:
            continue
        bucket = buckets.setdefault(
            str(row.get("verdict")), {"n": 0, "total_pnl_usdt": _ZERO, "wins": 0}
        )
        bucket["n"] += 1
        bucket["total_pnl_usdt"] += pnl
        if pnl > _ZERO:
            bucket["wins"] += 1
    traded = buckets.get("TRADE", {"n": 0, "total_pnl_usdt": _ZERO, "wins": 0})
    blocked = sum(
        (data["total_pnl_usdt"] for verdict, data in buckets.items() if verdict != "TRADE"),
        _ZERO,
    )
    return {
        "by_verdict": {
            verdict: {
                "n": data["n"],
                "total_pnl_usdt": str(data["total_pnl_usdt"]),
                "win_rate": (data["wins"] / data["n"]) if data["n"] else None,
            }
            for verdict, data in sorted(buckets.items())
        },
        "pnl_if_only_verdict_trade_taken_usdt": str(traded["total_pnl_usdt"]),
        "pnl_removed_by_filtering_usdt": str(blocked),
        "note": (
            "Advisory only (enforced=0 on every row). A filter is worth enabling only "
            "if it removes net-negative P/L; removing net-positive P/L means it would "
            "have made the system worse."
        ),
    }


def _avoidable_losses(investigations: list[dict]) -> dict:
    """How much of the realised damage a validated alternative could have
    prevented - counted only where the winner-damage cross-check passed."""
    by_policy: dict[str, dict] = {}
    total = _ZERO
    unavoidable = 0
    for row in investigations:
        policy = row.get("best_alternative_policy")
        improvement = _decimal(row.get("avoidable_loss_usdt"))
        if policy is None or improvement is None:
            unavoidable += 1
            continue
        bucket = by_policy.setdefault(str(policy), {"n": 0, "total_usdt": _ZERO})
        bucket["n"] += 1
        bucket["total_usdt"] += improvement
        total += improvement
    return {
        "total_improvement_available_usdt": str(total),
        "trades_with_no_better_alternative": unavoidable,
        "by_policy": {
            policy: {"n": data["n"], "total_usdt": str(data["total_usdt"])}
            for policy, data in sorted(
                by_policy.items(),
                key=lambda kv: Decimal(str(kv[1]["total_usdt"])),
                reverse=True,
            )
        },
    }


def render_text(report: dict) -> str:
    """A compact human-readable rendering, for a terminal or a Telegram
    message. Deliberately lossy - `build_report` stays the source of
    truth and this is the reading view."""
    lines: list[str] = []
    coverage = report["coverage"]
    lines.append("GODFATHER INTELLIGENCE REPORT")
    lines.append("=" * 60)
    lines.append(
        f"investigations={coverage['investigations']} "
        f"scorable={coverage['with_scorable_pnl']} "
        f"pending={coverage['still_pending_investigation']}"
    )
    objective = report["objective"]
    win_rate = objective["win_rate"]
    win_rate_text = "n/a" if win_rate is None else f"{win_rate * 100:.1f}%"
    lines.append(
        f"net P/L {money(objective['net_pnl_usdt'])} USDT over "
        f"{objective['trade_count']} trades | win rate {win_rate_text} | "
        f"profit factor {money(objective['profit_factor'])} | "
        f"expectancy {money(objective['expectancy_usdt'])} | "
        f"max drawdown {money(objective['max_drawdown_usdt'])} | "
        f"MFE capture {money(objective['mfe_capture_ratio'])}"
    )
    lines.append("")
    lines.append("-- classification --")
    for name, count in report["classification_breakdown"].items():
        lines.append(f"  {name:32s} {count}")
    lines.append("")
    lines.append("-- component scoreboard (RIGHT/WRONG/UNSCORABLE) --")
    for name, scores in report["component_scoreboard"].items():
        lines.append(
            f"  {name:22s} {scores['RIGHT']:4d} / {scores['WRONG']:4d} / "
            f"{scores['UNSCORABLE']:4d}"
        )
    lines.append("")
    lines.append("-- counterfactual policies (common comparable subset) --")
    for policy, data in report["counterfactual_policies"].items():
        significance = data.get("significance") or {}
        verdict = significance.get("verdict", "BASELINE")
        lines.append(
            f"  {policy:28s} total {money(data['total_delta_usdt']):>11s} "
            f"win {money(data['delta_on_winning_trades_usdt']):>11s} "
            f"lose {money(data['delta_on_losing_trades_usdt']):>11s}  {verdict}"
        )
    lines.append("")
    memory = report["experience_memory"]
    lines.append("-- experience memory --")
    for edge_class, count in memory["by_edge_class"].items():
        lines.append(f"  {edge_class:20s} {count}")
    lines.append(
        f"  robust conclusions: {len(memory['robust_conclusions'])}; "
        f"largest sample: {memory['largest_sample_size']}"
    )
    return "\n".join(lines)
