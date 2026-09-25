"""GODFATHER supervisor sweep: entry + position management, evidence first.

One pass over the whole real history that turns the pieces into
decisions GODFATHER could make, and into evidence about whether it
should:

1. **Book.** Every closed position, with its real P/L, real price path,
   candidate, screen and gate decision. Zero-size positions and unknown
   P/L are carried as UNAVAILABLE, never as neutral trades.
2. **MFE model** (`mfe_model`) from every closed trade.
3. **Counterfactuals** (`counterfactual`) for every scorable trade, with
   the MFE model restricted to trades that closed before the trade
   OPENED. Rows are restated (engine version 2) so no row lingers with
   old semantics.
4. **Entry selection** (`entry_selection`) for every CONFIRMED signal, with
   Experience Memory as it stood at the start of the signal's day.
5. **Portfolio** (`portfolio`): themes, concurrent correlation, cohort
   outcome dependence, advisory concentration cap.
6. **Policy registry** (`policy_registry`): every policy gated, BH across
   all of them, statuses and transitions persisted.

Writes only to GODFATHER tables. Makes no AI call. Changes no live rule.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.godfather import stats
from crypto_trading.godfather.auditor import detect_conflicts
from crypto_trading.godfather.counterfactual import ENGINE_VERSION, run_counterfactuals
from crypto_trading.godfather.entry_quality import assess_entry_quality
from crypto_trading.godfather.entry_selection import (
    EntrySignal,
    assign_selection,
    evaluate_selection,
)
from crypto_trading.godfather.experience import (
    ExperienceConfig,
    ExperienceSample,
    build_experience_memory,
)
from crypto_trading.godfather.features import build_candidate_features
from crypto_trading.godfather.mfe_model import MfeModel, observations_for_trade
from crypto_trading.godfather.path import PathPoint, compute_path_metrics, reconstruct_price_path
from crypto_trading.godfather.pipeline import _regime_for, _safe_candidate, _thresholds
from crypto_trading.godfather.policy_registry import (
    PolicyEvidence,
    evaluate_registry,
    rollback_check,
    transitions,
)
from crypto_trading.godfather.portfolio import (
    assign_portfolio_verdicts,
    cohort_outcome_dependence,
    concurrent_return_correlation,
    evaluate_diversification,
    exposure_profile,
    theme_of,
)
from crypto_trading.paper_trading.execution import compute_pnl_or_none
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.godfather import CounterfactualResult
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")

# Counterfactual policies that are NOT candidate policies: the reference
# rows, and "never trade" (it scores the whole book, not a decision).
_NOT_POLICIES = {"BASELINE", "NO_INTERVENTION", "REJECT_ENTRY"}
_ENTRY_SIDE = {"DELAY_ENTRY_30M", "DELAY_ENTRY_60M"}
_LIVE_TODAY = {"TIGHTEN_SL_AFTER_FAVORABLE"}

POLICY_DESCRIPTIONS = {
    "TIGHTEN_SL_AFTER_FAVORABLE": "LIVE Profit Protection: SL to break-even at +1%",
    "PROFIT_LOCK_HALF_MFE": "from +1%, SL = entry + half the best excursion (ratchet)",
    "THESIS_TIGHTEN": "SL to break-even only when the thesis says TIGHTEN_SL",
    "THESIS_POLICY": "full position decision: thesis (B) + MFE-history profit protection (A)",
    "EXIT_ON_THESIS_INVALID": "exit when the thesis is INVALID/EXIT (early exit)",
    "EXIT_ON_THESIS_WEAKENING": "exit when the thesis is WEAKENING or worse",
    "REDUCE_ON_WEAKENING": "halve the position when the thesis weakens",
    "SAFE_TP_AT_HALF_TARGET": "take profit at half the target distance",
    "DELAY_ENTRY_30M": "enter 30 minutes later at the observed price",
    "DELAY_ENTRY_60M": "enter 60 minutes later at the observed price",
    "ENTRY_SELECTION_TOP_HALF": "take only the better half of each confirmed cohort",
    "PORTFOLIO_THEME_CAP": "skip a TAKE when its theme already holds 2 open/selected positions",
}


@dataclass
class TradeContext:
    position: Position
    candidate: Candidate | None
    opportunity_screen: dict | None
    gate_decision: dict | None
    observations: list[dict]
    points: list[PathPoint]
    pnl: Decimal | None
    regime: str
    features: dict = field(default_factory=dict)

    @property
    def scorable(self) -> bool:
        return self.pnl is not None and self.position.size != _ZERO and bool(self.points)


def load_book(repo: Repository) -> list[TradeContext]:
    book: list[TradeContext] = []
    for position in repo.find_closed_positions():
        observations = repo.find_guardian_observations_for_position(position.position_id)
        candidate = _safe_candidate(repo, position.candidate_id)
        screen = repo.get_assessment_payload(position.candidate_id, "opportunity_screen")
        regime = _regime_for(observations)
        book.append(TradeContext(
            position=position,
            candidate=candidate,
            opportunity_screen=screen,
            gate_decision=repo.get_gate_decision(position.candidate_id),
            observations=observations,
            points=reconstruct_price_path(position, observations),
            pnl=None if position.size == _ZERO else compute_pnl_or_none(position),
            regime=regime,
            features=build_candidate_features(candidate, screen, position.opened_at, regime),
        ))
    book.sort(key=lambda t: t.position.opened_at)
    return book


def experience_samples(book: list[TradeContext], settings: Settings) -> list[ExperienceSample]:
    samples: list[ExperienceSample] = []
    for trade in book:
        if not trade.scorable or trade.position.closed_at is None:
            continue
        metrics = compute_path_metrics(
            trade.position, trade.points,
            settings.guardian.watch_decay_threshold, settings.guardian.exit_decay_threshold,
        )
        samples.append(ExperienceSample(
            position_id=trade.position.position_id,
            closed_at=trade.position.closed_at,
            pnl=trade.pnl,
            mfe_pct=metrics.mfe_pct,
            mae_pct=metrics.mae_pct,
            minutes_to_mfe=metrics.minutes_to_mfe,
            regime=trade.regime,
            features=trade.features,
        ))
    return samples


class PatternsAsOf:
    """Experience Memory as it stood at the start of a UTC day, built only
    from trades closed before that instant. Cached per day - the sweep is
    deterministic, so every signal on a day sees the same memory."""

    def __init__(self, samples: list[ExperienceSample], config: ExperienceConfig) -> None:
        self._samples = samples
        self._config = config
        self._cache: dict[str, list[dict]] = {}

    def __call__(self, moment: datetime) -> list[dict]:
        day_start = datetime.combine(moment.date(), time(0), tzinfo=moment.tzinfo)
        key = day_start.isoformat()
        if key not in self._cache:
            prior = [s for s in self._samples if s.closed_at < day_start]
            patterns = build_experience_memory(prior, day_start, "as_of", self._config)
            self._cache[key] = [p.model_dump() for p in patterns]
        return self._cache[key]


def _decimal_or_none(value: object) -> Decimal | None:
    """Agent-suggested levels are free text; unparseable means unknown."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        return None
    return parsed if parsed.is_finite() else None


def _parse(moment: object) -> datetime | None:
    if isinstance(moment, datetime):
        return moment
    try:
        return datetime.fromisoformat(str(moment))
    except ValueError:
        return None


def build_entry_signals(
    repo: Repository,
    settings: Settings,
    book: list[TradeContext],
    patterns_as_of: Callable[[datetime], list[dict]],
    now: datetime,
    run_id: str,
    persist: bool,
) -> list[EntrySignal]:
    by_candidate = {t.position.candidate_id: t for t in book}
    signals: list[EntrySignal] = []
    for row in repo.find_confirmed_gate_decisions():
        decided_at = _parse(row["evaluated_at"])
        candidate = _safe_candidate(repo, row["candidate_id"])
        if candidate is None or decided_at is None:
            continue
        trade = by_candidate.get(candidate.candidate_id)
        screen = repo.get_assessment_payload(candidate.candidate_id, "opportunity_screen")
        regime = trade.regime if trade is not None else "unknown"
        features = build_candidate_features(candidate, screen, decided_at, regime)
        planned_entry = (
            trade.position.simulated_fill_entry if trade is not None
            else candidate.reference_price
        )
        risk = candidate.risk
        stop = trade.position.stop_loss if trade is not None else (
            _decimal_or_none(risk.suggested_stop_loss) if risk else None
        )
        target = trade.position.target if trade is not None else (
            _decimal_or_none(risk.suggested_target) if risk else None
        )
        if planned_entry is None or stop is None or target is None:
            continue
        size = trade.position.size if trade is not None else Decimal("0")
        assessment = assess_entry_quality(
            candidate=candidate,
            features=features,
            conflicts=detect_conflicts(candidate, {"decision": "CONFIRMED"}),
            experience_patterns=patterns_as_of(decided_at),
            planned_entry=planned_entry,
            stop_loss=stop,
            target=target,
            size=size,
            risk_limits=settings.risk_limits,
            now=decided_at,
            run_id=run_id,
            regime_compatible=(None if regime == "unknown" else regime in ("btc_strong", "btc_ok")),
        )
        signals.append(EntrySignal(
            candidate_id=candidate.candidate_id,
            instrument=candidate.instrument,
            discovery_run_id=candidate.discovery_run_id,
            decided_at=decided_at,
            quality_score=assessment.quality_score,
            absolute_verdict=assessment.verdict,
            candidate_score=float(candidate.evidence_record.candidate_score),
            theme=theme_of(candidate.instrument),
            position_id=trade.position.position_id if trade is not None else None,
            opened_at=trade.position.opened_at if trade is not None else None,
            closed_at=trade.position.closed_at if trade is not None else None,
            realized_pnl=trade.pnl if trade is not None and trade.scorable else None,
            regime=regime,
            reason_codes=assessment.reason_codes,
            assessment=assessment,
        ))
    assign_selection(signals)
    open_themes = _open_themes_at(book, signals)
    assign_portfolio_verdicts(signals, open_themes)
    if persist:
        for signal in signals:
            assessment = signal.assessment
            detail = {
                **assessment.detail,
                "as_of": "experience memory built from trades closed before the signal's day",
                "cohort_size": signal.cohort_size,
                "cohort_rank": signal.cohort_rank,
                "absolute_verdict": signal.absolute_verdict,
                "selection_verdict": {"TRADE": "TAKE"}.get(
                    signal.selection_verdict, signal.selection_verdict
                ),
                "portfolio_verdict": signal.portfolio_verdict,
                "theme": signal.theme,
                "supervisor_run_id": run_id,
            }
            repo.upsert_godfather_entry_quality(assessment.model_copy(update={
                "verdict": signal.selection_verdict,
                "detail": detail,
                "run_id": run_id,
                "enforced": False,
            }))
    return signals


def _open_themes_at(book: list[TradeContext], signals: list[EntrySignal]) -> dict[str, list[str]]:
    """Themes of positions OPEN at each signal's decision time - known
    at that moment, no lookahead."""
    out: dict[str, list[str]] = {}
    for signal in signals:
        out[signal.candidate_id] = [
            theme_of(t.position.instrument)
            for t in book
            if t.position.opened_at < signal.decided_at
            and (t.position.closed_at is None or t.position.closed_at > signal.decided_at)
            and t.position.size != _ZERO
        ]
    return out


def run_book_counterfactuals(
    book: list[TradeContext],
    settings: Settings,
    mfe_model: MfeModel,
    now: datetime,
    run_id: str,
) -> dict[str, list[CounterfactualResult]]:
    thresholds = _thresholds(settings)
    out: dict[str, list[CounterfactualResult]] = {}
    for trade in book:
        if not trade.scorable:
            continue
        out[trade.position.position_id] = run_counterfactuals(
            trade.position, trade.points, settings.risk_limits, thresholds, now, run_id,
            mfe_model=mfe_model.as_of(trade.position.opened_at),
        )
    return out


def _regime_cells(pairs: list[tuple[str, float]]) -> list[dict]:
    cells: dict[str, list[float]] = {}
    for regime, value in pairs:
        cells.setdefault(regime, []).append(value)
    out = []
    for regime, values in sorted(cells.items()):
        ci = stats.bootstrap_mean_ci(values)
        out.append({
            "regime": regime,
            "n": len(values),
            "mean": stats.mean(values),
            "ci_low": ci.lower if ci else None,
            "ci_high": ci.upper if ci else None,
        })
    return out


def position_policy_evidence(
    counterfactuals: dict[str, list[CounterfactualResult]],
    book: list[TradeContext],
    cut: datetime,
) -> list[PolicyEvidence]:
    """One `PolicyEvidence` per counterfactual policy, over the trades
    where it ACTED and was OBSERVED. Trades it never touched are not
    padded in as zeros - that would inflate n and shrink every CI."""
    by_id = {t.position.position_id: t for t in book}
    rows_by_policy: dict[str, list[CounterfactualResult]] = {}
    for results in counterfactuals.values():
        for row in results:
            rows_by_policy.setdefault(row.policy, []).append(row)

    evidences: list[PolicyEvidence] = []
    for policy, rows in sorted(rows_by_policy.items()):
        if policy in _NOT_POLICIES:
            continue
        acted = [r for r in rows if r.triggered or r.detail.get("observation_status") != "OBSERVED"]
        observed = [
            r for r in acted
            if r.detail.get("observation_status") == "OBSERVED" and r.delta_pnl_usdt is not None
        ]
        observed.sort(key=lambda r: by_id[r.position_id].position.opened_at)
        deltas = [float(r.delta_pnl_usdt) for r in observed]
        ci = stats.bootstrap_mean_ci(deltas)
        train = [float(r.delta_pnl_usdt) for r in observed
                 if by_id[r.position_id].position.opened_at < cut]
        test = [float(r.delta_pnl_usdt) for r in observed
                if by_id[r.position_id].position.opened_at >= cut]
        pessimistic = [
            float(Decimal(r.detail["pessimistic_pnl_usdt"]) - r.actual_pnl_usdt)
            for r in observed if r.detail.get("pessimistic_pnl_usdt") is not None
        ]
        actual_wins = sum(1 for r in observed if r.actual_pnl_usdt > _ZERO)
        sim_wins = sum(1 for r in observed if r.simulated_pnl_usdt > _ZERO)
        evidences.append(PolicyEvidence(
            policy_id=policy,
            kind="ENTRY" if policy in _ENTRY_SIDE else "POSITION",
            description=POLICY_DESCRIPTIONS.get(policy, policy),
            live_today=policy in _LIVE_TODAY,
            n=len(deltas),
            mean_effect=stats.mean(deltas),
            ci_low=ci.lower if ci else None,
            ci_high=ci.upper if ci else None,
            p_value=stats.sign_flip_p_value(deltas),
            train_mean=stats.mean(train),
            test_mean=stats.mean(test),
            walk_forward_block_means=[stats.mean(b) for b in stats.sequential_blocks(deltas, 4)],
            costs_included=True,
            pessimistic_mean=stats.mean(pessimistic),
            expectancy_change=stats.mean(deltas),
            win_rate_change=((sim_wins - actual_wins) / len(observed)) if observed else None,
            regime_cells=_regime_cells([
                (by_id[r.position_id].regime, float(r.delta_pnl_usdt)) for r in observed
            ]),
            unobservable=sum(1 for r in acted
                             if r.detail.get("observation_status") == "UNOBSERVABLE"),
            unavailable=sum(1 for r in rows
                            if r.detail.get("observation_status") == "UNAVAILABLE"),
        ))
    return evidences


def entry_policy_evidence(selection: dict, diversification: dict, signals: list[EntrySignal],
                          cut: datetime) -> list[PolicyEvidence]:
    within = selection["within_cohort"]
    values = within["values"]
    regime_by_run: dict[str, str] = {}
    for s in signals:
        regime_by_run.setdefault(s.discovery_run_id, s.regime)
    selection_evidence = PolicyEvidence(
        policy_id="ENTRY_SELECTION_TOP_HALF",
        kind="ENTRY",
        description=POLICY_DESCRIPTIONS["ENTRY_SELECTION_TOP_HALF"],
        live_today=False,
        n=len(values),
        mean_effect=within["mean_effect_usdt"],
        ci_low=within["ci_low_usdt"],
        ci_high=within["ci_high_usdt"],
        p_value=within["p_value"],
        train_mean=within["train_mean_usdt"],
        test_mean=within["test_mean_usdt"],
        walk_forward_block_means=within["walk_forward_block_means"],
        costs_included=True,
        expectancy_change=within["mean_effect_usdt"],
        notes=[
            "effect = mean real P/L of a cohort's TAKE signals minus its other signals, "
            "same moment and market",
            f"{selection['unavailable_outcomes']} confirmed signals were never opened: "
            "UNAVAILABLE outcome",
        ],
    )
    # The concentration cap removes trades, and in a book that loses money
    # overall ANY removal looks good. So the cap is judged against the
    # trades it kept from the same TAKE pool - never against zero - and n
    # is the smaller of the two groups: a comparison against two kept
    # trades is not a comparison.
    skipped = [s for s in signals if s.scored and s.portfolio_verdict == "SKIP_CONCENTRATION"]
    kept = [s for s in signals if s.scored and s.portfolio_verdict == "KEEP"]
    diff = diversification["skipped_minus_kept"]
    kept_minus_skipped = None if diff["mean_diff_usdt"] is None else -diff["mean_diff_usdt"]

    def _half_diff(before: bool) -> float | None:
        k = [float(s.realized_pnl) for s in kept if (s.decided_at < cut) == before]
        sk = [float(s.realized_pnl) for s in skipped if (s.decided_at < cut) == before]
        return (sum(k) / len(k) - sum(sk) / len(sk)) if k and sk else None

    portfolio_evidence = PolicyEvidence(
        policy_id="PORTFOLIO_THEME_CAP",
        kind="PORTFOLIO",
        description=POLICY_DESCRIPTIONS["PORTFOLIO_THEME_CAP"],
        live_today=False,
        n=min(len(kept), len(skipped)),
        mean_effect=kept_minus_skipped,
        ci_low=None if diff["ci_high_usdt"] is None else -diff["ci_high_usdt"],
        ci_high=None if diff["ci_low_usdt"] is None else -diff["ci_low_usdt"],
        p_value=diff["p_value"],
        train_mean=_half_diff(True),
        test_mean=_half_diff(False),
        walk_forward_block_means=[],
        costs_included=True,
        expectancy_change=kept_minus_skipped,
        notes=[
            f"kept {len(kept)} vs skipped {len(skipped)} scored TAKE signals",
            "only subtracts trades; never raises max positions or capital limits",
        ],
    )
    return [selection_evidence, portfolio_evidence]


def _chronological_cut(book: list[TradeContext]) -> datetime:
    scorable = [t.position.opened_at for t in book if t.scorable]
    scorable.sort()
    return scorable[len(scorable) // 2]


def _position_policy_summary(counterfactuals: dict[str, list[CounterfactualResult]]) -> dict:
    summary: dict[str, dict] = {}
    for results in counterfactuals.values():
        for row in results:
            bucket = summary.setdefault(row.policy, {
                "rows": 0, "acted": 0, "observed": 0, "unobservable": 0, "unavailable": 0,
            })
            bucket["rows"] += 1
            bucket["acted"] += 1 if row.triggered else 0
            status = row.detail.get("observation_status")
            if status == "UNOBSERVABLE":
                bucket["unobservable"] += 1
            elif status == "UNAVAILABLE":
                bucket["unavailable"] += 1
            else:
                bucket["observed"] += 1
    return summary


def _thesis_action_mix(counterfactuals: dict[str, list[CounterfactualResult]]) -> dict:
    mix: dict[str, int] = {}
    for results in counterfactuals.values():
        for row in results:
            if row.policy == "THESIS_POLICY":
                for action, count in row.detail.get("actions", {}).items():
                    mix[action] = mix.get(action, 0) + count
    return mix


def run_supervisor_sweep(
    repo: Repository, settings: Settings, now: datetime, run_id: str, persist: bool = True
) -> dict:
    book = load_book(repo)
    scorable = [t for t in book if t.scorable]
    if len(scorable) < 2:
        return {"status": "INSUFFICIENT_DATA", "scorable_trades": len(scorable)}
    cut = _chronological_cut(book)

    mfe_model = MfeModel(
        [o for t in scorable for o in observations_for_trade(t.position, t.points)]
    )
    counterfactuals = run_book_counterfactuals(book, settings, mfe_model, now, run_id)
    if persist:
        for results in counterfactuals.values():
            for row in results:
                repo.replace_godfather_counterfactual(row)

    config = ExperienceConfig(
        min_sample_size=settings.godfather.experience_min_sample_size,
        min_support=settings.godfather.experience_min_support,
        fdr_q=settings.godfather.experience_fdr_q,
    )
    samples = experience_samples(book, settings)
    patterns_now = build_experience_memory(samples, now, run_id, config)
    signals = build_entry_signals(
        repo, settings, book, PatternsAsOf(samples, config), now, run_id, persist
    )
    selection = evaluate_selection(signals, cut)
    diversification = evaluate_diversification(signals, cut)

    evidences = position_policy_evidence(counterfactuals, book, cut) + entry_policy_evidence(
        selection, diversification, signals, cut
    )
    current = {row["policy_id"]: row["status"] for row in repo.find_godfather_policies()}
    registry = evaluate_registry(
        evidences, current, settings.godfather.policy_promotion_enabled,
        settings.godfather.experience_fdr_q,
    )
    for row in registry:
        # Forward (post-promotion) evidence only exists once a policy has
        # been CANARY/PROMOTED; none has, so this is inert today - but the
        # path is exercised on every sweep, not only in tests.
        rolled = rollback_check(row["status"], [])
        if rolled is not None:
            row["status"] = rolled
    changes = transitions(registry, now)
    if persist:
        for row in registry:
            repo.upsert_godfather_policy(row, now, run_id)
        for change in changes:
            repo.save_godfather_policy_transition(change, run_id)

    paths = [
        (t.position.position_id, t.position.instrument, t.position.opened_at, t.points)
        for t in scorable
    ]
    edge_counts: dict[str, int] = {}
    for pattern in patterns_now:
        edge_counts[pattern.edge_class] = edge_counts.get(pattern.edge_class, 0) + 1

    return {
        "run_id": run_id,
        "evaluated_at": now.isoformat(),
        "engine_version": ENGINE_VERSION,
        "data": {
            "closed_positions": len(book),
            "scorable": len(scorable),
            "zero_size_excluded": sum(1 for t in book if t.position.size == _ZERO),
            "unknown_pnl": sum(
                1 for t in book if t.position.size != _ZERO and t.pnl is None
            ),
            "no_path": sum(1 for t in book if t.pnl is not None and not t.points),
            "confirmed_signals": len(signals),
            "train_test_cut": cut.isoformat(),
        },
        "mfe_model": mfe_model.summary(),
        "counterfactual_coverage": _position_policy_summary(counterfactuals),
        "thesis_policy_action_mix": _thesis_action_mix(counterfactuals),
        "entry_selection": {k: v for k, v in selection.items() if k != "within_cohort"}
        | {"within_cohort": {k: v for k, v in selection["within_cohort"].items()
                             if k not in ("values", "moments")}},
        "portfolio": {
            "diversification": diversification,
            "cohort_outcome_dependence": cohort_outcome_dependence(signals),
            "concurrent_return_correlation": concurrent_return_correlation(paths),
            "exposure": exposure_profile([
                (t.position.opened_at, t.position.closed_at, t.position.size,
                 theme_of(t.position.instrument))
                for t in scorable
            ]),
        },
        "experience_memory": {"samples": len(samples), "by_edge_class": edge_counts},
        "registry": registry,
        "transitions": changes,
        "promotion_enabled": settings.godfather.policy_promotion_enabled,
        "ai_calls": 0,
    }


def sweep_is_due(repo: Repository, settings: Settings, now: datetime) -> bool:
    rows = repo.find_godfather_policies()
    if not rows:
        return True
    last = max(datetime.fromisoformat(r["updated_at"]) for r in rows)
    return (now - last).total_seconds() >= settings.godfather.supervisor_sweep_interval_hours * 3600


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


# ---------------------------------------------------------------------
# Rendering + CLI
# ---------------------------------------------------------------------


def _m(value: object, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.{digits}f}"


def _p(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.1%}"


def render_markdown(report: dict) -> str:
    if "registry" not in report:
        return f"# GODFATHER supervisor sweep\n\n{report}\n"
    d = report["data"]
    lines: list[str] = []
    w = lines.append
    w("# GODFATHER supervisor sweep")
    w("")
    w(f"Run `{report['run_id']}` at {report['evaluated_at']}. Counterfactual engine "
      f"v{report['engine_version']}. AI calls: {report['ai_calls']}. Policy promotion "
      f"enabled: {report['promotion_enabled']}. **No live rule is changed by this sweep.**")
    w("")
    w(f"Data: {d['closed_positions']} closed positions; {d['scorable']} scorable (real P/L, "
      f"exposure, price path); {d['zero_size_excluded']} zero-size and {d['unknown_pnl']} "
      f"unknown-P/L positions UNAVAILABLE; {d['no_path']} without a price path; "
      f"{d['confirmed_signals']} confirmed signals; train/test cut {d['train_test_cut'][:16]}.")
    w("")
    w("## Policy registry")
    w("")
    w("| policy | kind | status | n | mean effect / trade [95% CI] | p | BH | train / test "
      "| walk-forward blocks | unobservable | flags |")
    w("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in report["registry"]:
        e = row["evidence"]
        blocks = " ".join(_m(b, 1) for b in e["walk_forward_block_means"]) or "-"
        w(f"| {row['policy_id']} | {row['kind']} | **{row['status']}** | {e['n']} | "
          f"{_m(e['mean_effect'])} [{_m(e['ci_low'])}, {_m(e['ci_high'])}] | "
          f"{e['p_value']:.3f} | {row['fdr_significant']} | {_m(e['train_mean'])} / "
          f"{_m(e['test_mean'])} | {blocks} | {e['unobservable']} | "
          f"{', '.join(row['flags']) or '-'} |")
    w("")
    w("Gate matrix (PASS / FAIL / INSUFFICIENT_DATA):")
    w("")
    gate_names = list(report["registry"][0]["gates"]) if report["registry"] else []
    w("| policy | " + " | ".join(gate_names) + " |")
    w("|---|" + "---|" * len(gate_names))
    for row in report["registry"]:
        w(f"| {row['policy_id']} | " + " | ".join(row["gates"][g] for g in gate_names) + " |")
    w("")
    if report["transitions"]:
        w("Transitions this sweep: " + "; ".join(
            f"{t['policy_id']} {t['from_status']} -> {t['to_status']}"
            for t in report["transitions"]
        ))
        w("")

    w("## MFE model (what happened next, historically, from each favourable level)")
    w("")
    w("| level | status | n | P(further +1%) | median further MFE % | P(back to entry) "
      "| median final giveback | P(target) |")
    w("|---|---|---|---|---|---|---|---|")
    for row in report["mfe_model"]:
        w(f"| +{row['level_pct']}% | {row['status']} | {row['n']} | "
          f"{_p(row['p_further_1pct'])} | {_m(row['median_further_mfe_pct'])} | "
          f"{_p(row['p_revert_to_entry'])} | {_p(row['median_final_giveback_ratio'])} | "
          f"{_p(row['p_target'])} |")
    w("")

    es = report["entry_selection"]
    w("## Entry selection (cohort-relative TAKE / WAIT / REJECT)")
    w("")
    w(f"{es['confirmed_signals']} confirmed signals, {es['scored_signals']} with a real "
      f"outcome, {es['unavailable_outcomes']} UNAVAILABLE (never opened, zero size or "
      "unknown P/L).")
    w("")
    w("| verdict | n | total P/L | mean | win rate |")
    w("|---|---|---|---|---|")
    for verdict, g in es["by_verdict"].items():
        label = "TAKE" if verdict == "TRADE" else verdict
        w(f"| {label} | {g['n']} | {_m(g['total_pnl_usdt'])} | {_m(g['mean_pnl_usdt'])} | "
          f"{_p(g['win_rate'])} |")
    wc = es["within_cohort"]
    tv = es["take_vs_rest"]
    w("")
    w(f"Within the same cohort (same moment, same market): TAKE minus rest "
      f"{_m(wc['mean_effect_usdt'])} USDT [{_m(wc['ci_low_usdt'])}, "
      f"{_m(wc['ci_high_usdt'])}] over {wc['cohorts']} cohorts, p={wc['p_value']:.3f}; "
      f"train {_m(wc['train_mean_usdt'])} (n={wc['train_n']}) / test "
      f"{_m(wc['test_mean_usdt'])} (n={wc['test_n']}). Pooled TAKE vs rest: "
      f"{_m(tv['mean_diff_usdt'])} [{_m(tv['ci_low_usdt'])}, {_m(tv['ci_high_usdt'])}], "
      f"p={tv['p_value']:.3f}. Book if only TAKE had been traded: "
      f"{_m(es['book_if_only_take_usdt'])} vs actual {_m(es['book_actual_usdt'])} USDT - "
      "not evidence on its own: removing trades from a losing book always looks good.")
    w("")

    pf = report["portfolio"]
    dep = pf["cohort_outcome_dependence"]
    corr = pf["concurrent_return_correlation"]
    div = pf["diversification"]
    w("## Portfolio / correlation")
    w("")
    w(f"- Return correlation of concurrently held positions: same theme "
      f"{_m(corr['mean_corr_same_theme'], 3)} over {corr['pairs_same_theme']} pairs, "
      f"cross theme {_m(corr['mean_corr_cross_theme'], 3)} over "
      f"{corr['pairs_cross_theme']} pairs. BTC beta: {corr['btc_beta']} "
      "(no BTC series stored).")
    if dep["status"] == "ESTIMATE":
        w(f"- Outcome dependence inside a cohort: ICC {_m(dep['icc_pnl'], 3)} across "
          f"{dep['cohorts']} cohorts / {dep['trades']} trades; a cohort of "
          f"{dep['mean_cohort_size']:.1f} trades is worth about "
          f"{dep['effective_independent_bets_per_cohort']:.1f} independent bets.")
    else:
        w(f"- Outcome dependence inside a cohort: {dep['status']}.")
    w(f"- Exposure: {pf['exposure']['direction']}, peak concurrent notional "
      f"{_m(pf['exposure']['peak_concurrent_notional_usdt'], 0)} USDT, peak single-theme "
      f"share {_p(pf['exposure']['peak_single_theme_share'])}.")
    w(f"- Theme cap (advisory): kept {div['kept']}, skipped {div['skipped_concentration']} "
      f"(skipped trades made {_m(div['skipped_total_pnl_usdt'])} USDT).")
    w("")

    w("## Counterfactual coverage (observed / unobservable / unavailable)")
    w("")
    w("| policy | acted | observed | unobservable | unavailable |")
    w("|---|---|---|---|---|")
    for policy, c in sorted(report["counterfactual_coverage"].items()):
        w(f"| {policy} | {c['acted']} | {c['observed']} | {c['unobservable']} | "
          f"{c['unavailable']} |")
    w("")
    mix = report["thesis_policy_action_mix"]
    w("THESIS_POLICY decisions over all replayed ticks: "
      + ", ".join(f"{k} {v}" for k, v in sorted(mix.items())) + ".")
    w("")
    em = report["experience_memory"]
    w("## Experience Memory")
    w("")
    w(f"{em['samples']} samples; patterns by class: "
      + ", ".join(f"{k} {v}" for k, v in sorted(em["by_edge_class"].items()))
      + ". INSUFFICIENT_DATA and NOISE authorise no strategic change.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    import argparse
    from datetime import UTC
    from pathlib import Path

    from crypto_trading.config.loader import get_settings
    from crypto_trading.logging import new_run_id
    from crypto_trading.storage.repository import SQLiteRepository

    parser = argparse.ArgumentParser(description="GODFATHER supervisor sweep on real history")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    settings = get_settings()
    repo = SQLiteRepository(settings.db_path)
    report = run_supervisor_sweep(
        repo, settings, datetime.now(UTC), new_run_id(), persist=not args.no_persist
    )
    text = render_markdown(report)
    if args.markdown:
        args.markdown.write_text(text, encoding="utf-8")
    if args.json:
        args.json.write_text(dumps(report), encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
