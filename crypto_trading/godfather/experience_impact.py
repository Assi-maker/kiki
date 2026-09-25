"""Does experience actually change what GODFATHER decides? Measured, not assumed.

Two controlled replays over the real history, each run twice - once with
the Experience Memory that existed at the moment of the decision, once
with an empty memory - so the ONLY difference between the runs is the
experience:

* **Entry**: every confirmed signal is scored by the advisory Entry
  Quality + cohort selection (`supervisor.build_entry_signals`) with
  as-of patterns (trades closed before the signal's day) vs none.
* **Position**: every scorable trade is replayed through the full
  position decision (THESIS_POLICY) with the MFE model as of its entry vs
  without one.

Changed decisions are listed with the real outcome of the trade, so
"experience changed 3 verdicts and those trades made -40 USDT" is a
measurable statement. Nothing here writes a trading table or changes a
live rule; with `persist` it restates Experience Memory only.

Also renders the Fas 2 report: coverage, learned experience, evidence
quality, management and prediction experience, impact.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.godfather import stats
from crypto_trading.godfather.book import TradeContext
from crypto_trading.godfather.costs import implied_funding_rate
from crypto_trading.godfather.counterfactual import simulate_position_policy
from crypto_trading.godfather.experience import ExperienceSample
from crypto_trading.godfather.experience_builder import (
    experience_config,
    run_experience_backfill,
)
from crypto_trading.godfather.mfe_model import MfeModel, observations_for_trade
from crypto_trading.godfather.pipeline import _thresholds
from crypto_trading.godfather.supervisor import PatternsAsOf, build_entry_signals
from crypto_trading.schemas.godfather import ExperiencePattern
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")
_REGIME_FAMILIES = (
    "btc_regime_bucket", "volatility_bucket", "funding_bucket", "entry_rsi_bucket",
    "volume_zscore_bucket", "trigger_reasons_key",
)


def measure_entry_impact(
    repo: Repository,
    settings: Settings,
    book: list[TradeContext],
    samples: list[ExperienceSample],
    now: datetime,
    run_id: str,
) -> dict:
    config = experience_config(settings)
    with_memory = build_entry_signals(
        repo, settings, book, PatternsAsOf(samples, config), now, run_id, persist=False
    )
    without = {
        s.candidate_id: s
        for s in build_entry_signals(
            repo, settings, book, lambda _moment: [], now, run_id, persist=False
        )
    }
    evidence_verdicts: dict[str, int] = {}
    changed: list[dict] = []
    similar = 0
    for signal in with_memory:
        experience = (signal.assessment.detail or {}).get("experience") or {}
        verdict = experience.get("verdict", "UNKNOWN_SITUATION")
        evidence_verdicts[verdict] = evidence_verdicts.get(verdict, 0) + 1
        if (experience.get("similar_cases") or {}).get("sample_size"):
            similar += 1
        baseline = without.get(signal.candidate_id)
        if baseline is None:
            continue
        if (
            baseline.selection_verdict != signal.selection_verdict
            or baseline.absolute_verdict != signal.absolute_verdict
        ):
            changed.append({
                "candidate_id": signal.candidate_id,
                "instrument": signal.instrument,
                "baseline_decision": baseline.selection_verdict,
                "godfather_decision": signal.selection_verdict,
                "evidence": verdict,
                "signed_weight": experience.get("signed_weight"),
                "actual_pnl_usdt": (
                    None if signal.realized_pnl is None else str(signal.realized_pnl)
                ),
            })
    return {
        "signals": len(with_memory),
        "evidence_verdicts": evidence_verdicts,
        "signals_with_reliable_similar_cases": similar,
        "decisions_changed": len(changed),
        "changed": changed,
        "changed_with_outcome_pnl_usdt": str(sum(
            (Decimal(c["actual_pnl_usdt"]) for c in changed if c["actual_pnl_usdt"]), _ZERO
        )),
    }


def measure_position_impact(book: list[TradeContext], settings: Settings) -> dict:
    thresholds = _thresholds(settings)
    scorable = [t for t in book if t.scorable]
    model = MfeModel([o for t in scorable for o in observations_for_trade(t.position, t.points)])
    changed = 0
    deltas: list[float] = []
    compared = 0
    for trade in scorable:
        funding = implied_funding_rate(trade.position)
        with_memory, _ = simulate_position_policy(
            trade.position, trade.points, thresholds, model.as_of(trade.position.opened_at),
            trade.pnl, settings.risk_limits, funding,
        )
        without, _ = simulate_position_policy(
            trade.position, trade.points, thresholds, None, trade.pnl, settings.risk_limits,
            funding,
        )
        if with_memory is None or without is None:
            continue
        compared += 1
        if with_memory != without:
            changed += 1
            deltas.append(float(with_memory - without))
    return {
        "trades_compared": compared,
        "decisions_changed_outcome": changed,
        "mean_effect_of_experience_usdt": stats.mean(deltas),
        "total_effect_of_experience_usdt": sum(deltas) if deltas else 0.0,
    }


def _detail(pattern: ExperiencePattern) -> dict:
    return pattern.detail or {}


def coverage(book: list[TradeContext], samples: list[ExperienceSample], repo: Repository) -> dict:
    counterfactuals = repo.find_godfather_counterfactuals()
    return {
        "closed_positions": len(book),
        "usable_trades": len(samples),
        "zero_size_unavailable": sum(1 for t in book if t.position.size == _ZERO),
        "unknown_pnl_unavailable": sum(
            1 for t in book if t.position.size != _ZERO and t.pnl is None
        ),
        "with_price_path": sum(1 for s in samples if s.profile.get("has_path")),
        "with_mfe_mae": sum(1 for s in samples if s.mfe_pct is not None),
        "with_entry_features": sum(1 for s in samples if s.features),
        "entry_features_invalid_timestamp": sum(1 for t in book if not t.features_valid),
        "with_entry_success_observed": sum(
            1 for s in samples if s.profile.get("entry_success") is not None
        ),
        "with_management_observations": sum(
            1 for s in samples if s.profile.get("counterfactual_delta")
        ),
        "live_executed": sum(1 for s in samples if s.live),
        "counterfactual_rows": len(counterfactuals),
        "counterfactual_rows_observed": sum(
            1 for r in counterfactuals
            if json.loads(r["detail_json"]).get("observation_status", "OBSERVED") == "OBSERVED"
        ),
        "prediction_errors": len(repo.find_godfather_prediction_errors()),
    }


def learned(patterns: list[ExperiencePattern]) -> dict:
    classified = [p for p in patterns if p.edge_class not in ("NOISE", "INSUFFICIENT_DATA")]
    entry_classes: dict[str, int] = {}
    for p in patterns:
        cls = (_detail(p).get("entry_quality") or {}).get("class", "INSUFFICIENT_DATA")
        entry_classes[cls] = entry_classes.get(cls, 0) + 1
    regime_rows = []
    for p in sorted(patterns, key=lambda p: (p.pattern_family, p.pattern_key)):
        if p.pattern_family not in _REGIME_FAMILIES:
            continue
        profile = _detail(p).get("profile") or {}
        regime_rows.append({
            "pattern": p.pattern_key,
            "n": p.sample_size,
            "expectancy_usdt": None if p.expectancy_usdt is None else float(p.expectancy_usdt),
            "win_rate": p.win_rate,
            "mfe_p50": (profile.get("mfe_pct") or {}).get("p50"),
            "mae_p50": (profile.get("mae_pct") or {}).get("p50"),
            "entry_success_rate": profile.get("entry_success_rate"),
            "edge_class": p.edge_class,
            "entry_class": (_detail(p).get("entry_quality") or {}).get("class"),
            "oos_n": (_detail(p).get("evidence") or {}).get("oos_n"),
        })
    return {
        "patterns": len(patterns),
        "by_edge_class": _count(p.edge_class for p in patterns),
        "entry_quality_classes": entry_classes,
        "classified": [
            {"pattern": p.pattern_key, "n": p.sample_size, "class": p.edge_class,
             "confidence": p.confidence}
            for p in classified
        ],
        "regime_and_feature_profiles": regime_rows,
    }


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def evidence_quality(patterns: list[ExperiencePattern]) -> dict:
    testable = [p for p in patterns if p.sample_size >= 30]
    return {
        "patterns_with_n_ge_30": len(testable),
        "patterns_with_confidence_gt_0": sum(1 for p in patterns if p.confidence > 0),
        "patterns_with_oos_n_ge_10": sum(
            1 for p in patterns if (_detail(p).get("evidence") or {}).get("oos_n", 0) >= 10
        ),
        "patterns_surviving_walk_forward": sum(1 for p in patterns if p.survived_walk_forward),
        "patterns_with_live_evidence": sum(
            1 for p in patterns if (_detail(p).get("evidence") or {}).get("live_n", 0) > 0
        ),
        "sample_size_distribution": sorted(p.sample_size for p in patterns),
    }


def management_experience(samples: list[ExperienceSample]) -> dict:
    """Exit experience, and entry quality kept apart from management
    quality: a trade can have a good entry (+1% before -1%) and still
    lose money, or a bad entry that an exit happened to rescue."""
    by_exit: dict[str, list[ExperienceSample]] = {}
    for s in samples:
        by_exit.setdefault(str(s.profile.get("exit_reason")), []).append(s)
    exits = {}
    for reason, group in sorted(by_exit.items()):
        mfes = [float(s.mfe_pct) for s in group if s.mfe_pct is not None]
        maes = [float(s.mae_pct) for s in group if s.mae_pct is not None]
        exits[reason] = {
            "n": len(group),
            "mean_pnl_usdt": stats.mean([float(s.pnl) for s in group]),
            "median_mfe_pct": sorted(mfes)[len(mfes) // 2] if mfes else None,
            "median_mae_pct": sorted(maes)[len(maes) // 2] if maes else None,
        }
    cross = {"good_entry_won": 0, "good_entry_lost": 0, "bad_entry_won": 0,
             "bad_entry_lost": 0, "entry_unobservable": 0}
    for s in samples:
        entry = s.profile.get("entry_success")
        if entry is None:
            cross["entry_unobservable"] += 1
        else:
            cross[f"{'good' if entry else 'bad'}_entry_{'won' if s.win else 'lost'}"] += 1
    return {"by_exit_reason": exits, "entry_vs_management": cross}


def prediction_experience(samples: list[ExperienceSample]) -> dict:
    by_regime: dict[str, list[float]] = {}
    for s in samples:
        if s.profile.get("forecast_error") is not None:
            by_regime.setdefault(s.regime, []).append(float(s.profile["forecast_error"]))
    everything = [v for values in by_regime.values() for v in values]
    return {
        "forecast_calibration_error_mean": stats.mean(everything),
        "forecast_n": len(everything),
        "by_regime": {
            regime: {"n": len(values), "mean": stats.mean(values)}
            for regime, values in sorted(by_regime.items())
        },
    }


def run_fas2(
    repo: Repository, settings: Settings, now: datetime, run_id: str, persist: bool = True
) -> dict:
    book, samples, patterns, cleanup = run_experience_backfill(
        repo, settings, now, run_id, persist
    )
    return {
        "run_id": run_id,
        "evaluated_at": now.isoformat(),
        "ai_calls": 0,
        "cleanup": cleanup,
        "coverage": coverage(book, samples, repo),
        "learned": learned(patterns),
        "evidence_quality": evidence_quality(patterns),
        "management": management_experience(samples),
        "prediction": prediction_experience(samples),
        "impact": {
            "entry": measure_entry_impact(repo, settings, book, samples, now, run_id),
            "position": measure_position_impact(book, settings),
        },
    }


def _f(value: object, digits: int = 2) -> str:
    return "n/a" if value is None else f"{float(value):+.{digits}f}"


def _pct(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.0%}"


def render_markdown(report: dict) -> str:
    c = report["coverage"]
    lines: list[str] = []
    w = lines.append
    w("# GODFATHER Fas 2: experience backfill")
    w("")
    w(f"Run `{report['run_id']}` at {report['evaluated_at']}. AI calls: {report['ai_calls']}. "
      f"Zero-size prediction-error rows removed: "
      f"{report['cleanup']['zero_size_prediction_errors_removed']}.")
    w("")
    w("## Experience coverage")
    w("")
    for key, value in c.items():
        w(f"- {key.replace('_', ' ')}: {value}")
    w("")
    le = report["learned"]
    w("## Learned experience")
    w("")
    w(f"{le['patterns']} patterns. P/L classes: {le['by_edge_class']}. Entry-quality classes "
      f"(+1% before -1%, independent of exits): {le['entry_quality_classes']}.")
    w("")
    if le["classified"]:
        w("Classified patterns: " + "; ".join(
            f"{p['pattern']} {p['class']} (n={p['n']}, conf {p['confidence']:.2f})"
            for p in le["classified"]
        ))
    else:
        w("**No pattern is EDGE, WEAK_EDGE, REGIME_DEPENDENT, DECAYING_EDGE or FAILURE_PATTERN.**")
    w("")
    w("| pattern | n | expectancy | win rate | MFE p50 % | MAE p50 % | entry success | P/L class "
      "| entry class | OOS n |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for r in le["regime_and_feature_profiles"]:
        w(f"| {r['pattern']} | {r['n']} | {_f(r['expectancy_usdt'])} | {_pct(r['win_rate'])} | "
          f"{_f(r['mfe_p50'])} | {_f(r['mae_p50'])} | {_pct(r['entry_success_rate'])} | "
          f"{r['edge_class']} | {r['entry_class']} | {r['oos_n']} |")
    w("")
    eq = report["evidence_quality"]
    w("## Evidence quality")
    w("")
    for key, value in eq.items():
        w(f"- {key.replace('_', ' ')}: {value}")
    w("")
    m = report["management"]
    w("## Exit / management experience")
    w("")
    w("| exit | n | mean P/L | median MFE % | median MAE % |")
    w("|---|---|---|---|---|")
    for reason, row in m["by_exit_reason"].items():
        w(f"| {reason} | {row['n']} | {_f(row['mean_pnl_usdt'])} | {_f(row['median_mfe_pct'])} "
          f"| {_f(row['median_mae_pct'])} |")
    w("")
    w(f"Entry vs management: {m['entry_vs_management']}.")
    w("")
    pr = report["prediction"]
    w("## Prediction experience")
    w("")
    w(f"Forecast calibration error (share of probability mass NOT on the realised scenario): "
      f"mean {_f(pr['forecast_calibration_error_mean'], 3)} over {pr['forecast_n']} trades; by "
      "regime: " + ", ".join(
          f"{r} {_f(v['mean'], 3)} (n={v['n']})" for r, v in pr["by_regime"].items()
      ) + ".")
    w("")
    ie = report["impact"]["entry"]
    ip = report["impact"]["position"]
    w("## GODFATHER impact (historical replay, as-of memory vs empty memory)")
    w("")
    w(f"- Entry: {ie['signals']} signals; evidence verdicts {ie['evidence_verdicts']}; "
      f"{ie['signals_with_reliable_similar_cases']} had a reliable similar-case profile "
      f"(n >= 30); **{ie['decisions_changed']} decisions changed** "
      f"(real P/L of changed trades {_f(ie['changed_with_outcome_pnl_usdt'])} USDT).")
    for row in ie["changed"][:20]:
        w(f"  - {row['instrument']}: {row['baseline_decision']} -> {row['godfather_decision']} "
          f"({row['evidence']}, weight {_f(row['signed_weight'])}), actual "
          f"{_f(row['actual_pnl_usdt'])}")
    w(f"- Position: {ip['trades_compared']} trades replayed with and without the MFE history; "
      f"**{ip['decisions_changed_outcome']} outcomes changed**, total effect "
      f"{_f(ip['total_effect_of_experience_usdt'])} USDT (mean "
      f"{_f(ip['mean_effect_of_experience_usdt'])}).")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    import argparse
    from datetime import UTC
    from pathlib import Path

    from crypto_trading.config.loader import get_settings
    from crypto_trading.logging import new_run_id
    from crypto_trading.storage.repository import SQLiteRepository

    parser = argparse.ArgumentParser(description="GODFATHER Fas 2 experience backfill")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path)
    report = run_fas2(repo, settings, datetime.now(UTC), new_run_id(), not args.no_persist)
    text = render_markdown(report)
    if args.markdown:
        args.markdown.write_text(text, encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
