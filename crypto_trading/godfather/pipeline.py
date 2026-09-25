"""The GODFATHER Intelligence tick: one pass over everything that closed.

This is the integration point (requirement 14, step 10). It reads
already-persisted trading data, runs the six analysis subsystems in
dependency order, and writes ONLY to the seven `godfather_*` tables.

Order matters and is not arbitrary:

1. **Counterfactuals first.** The investigator's "what decision at time T
   would have helped" answer must be cross-checked against each policy's
   portfolio-wide effect on WINNING trades, so every simulation for the
   batch has to exist before the first verdict is written.
2. **Then investigation, then audit.** The audit reuses the
   investigator's entry/management verdicts rather than re-deriving them,
   so the two records can never contradict each other.
3. **Then prediction errors**, marked actionable only from the PREVIOUS
   sweep's Experience Memory. The one-sweep lag is deliberate: a lesson
   may not be declared actionable by the same pass that produced the
   trade it came from, or a single fresh loss could promote itself into
   a rule.
4. **Then the Experience Memory sweep**, over every investigation ever
   stored - because significance is a property of the whole history, not
   of this batch.
5. **Then thesis tracking** for currently open positions, and the
   advisory entry-quality backfill.

Nothing here can affect a live trade. `pipeline.py` imports no connector,
no order primitive and no sizing code, and the only repository methods it
writes through are the `save_godfather_*`/`upsert_godfather_*` family -
which `test_intelligence_isolation.py` pins by AST.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.godfather import experience as experience_module
from crypto_trading.godfather.auditor import audit_decision
from crypto_trading.godfather.counterfactual import (
    aggregate_policy_performance,
    common_scorable_positions,
    run_counterfactuals,
)
from crypto_trading.godfather.entry_quality import assess_entry_quality
from crypto_trading.godfather.experience import (
    ExperienceConfig,
    ExperienceSample,
    build_experience_memory,
)
from crypto_trading.godfather.features import btc_regime_bucket, build_candidate_features
from crypto_trading.godfather.investigator import (
    investigate_position,
    judge_entry,
    judge_management,
)
from crypto_trading.godfather.path import compute_path_metrics, reconstruct_price_path
from crypto_trading.godfather.prediction_error import build_prediction_errors
from crypto_trading.godfather.thesis import (
    ThesisThresholds,
    build_thesis_features,
    evaluate_thesis,
    validate_action_is_safe,
)
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.execution import compute_pnl_or_none
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.godfather import (
    CounterfactualResult,
    PredictionErrorSource,
    ThesisObservation,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")

DEFAULT_BATCH_LIMIT = 50


def _thresholds(settings: Settings) -> ThesisThresholds:
    return ThesisThresholds(
        watch=settings.guardian.watch_decay_threshold,
        protect=settings.guardian.protect_decay_threshold,
        exit=settings.guardian.exit_decay_threshold,
        max_hold_hours=settings.risk_limits.max_position_hold_hours,
    )


def _safe_candidate(repo: Repository, candidate_id: str) -> Candidate | None:
    """A corrupt candidate row is context, not control flow - the same
    non-fatal treatment `guardian/self_improvement.py` already gives it.
    An investigation with no candidate is still worth having; one that
    crashed the whole batch is not."""
    try:
        return repo.get_candidate(candidate_id)
    except CorruptCandidateStateError:
        return None


def _regime_for(observations: list[dict]) -> str:
    """BTC regime as Guardian measured it on this position's FIRST tick.

    The first observed tick rather than an average: regime compatibility
    is an entry-time question, and averaging over the trade's life would
    mix in conditions that only existed after the decision was made.
    """
    if not observations:
        return "unknown"
    first = min(observations, key=lambda row: str(row.get("observed_at") or ""))
    factors = first.get("factors")
    if isinstance(factors, str):
        try:
            factors = json.loads(factors)
        except (ValueError, TypeError):
            factors = {}
    if not isinstance(factors, dict):
        return "unknown"
    value = factors.get("market_regime")
    if not isinstance(value, (int, float)):
        return "unknown"
    return btc_regime_bucket(float(value))


def _thesis_id(position_id: str, observed_at: datetime) -> str:
    return hashlib.sha256(f"{position_id}:{observed_at.isoformat()}".encode()).hexdigest()


def run_counterfactual_batch(
    repo: Repository,
    settings: Settings,
    positions: list[Position],
    now: datetime,
    run_id: str,
) -> tuple[int, dict[str, list[CounterfactualResult]]]:
    """Simulate every policy for every position in the batch and persist.

    Saved BEFORE any investigation is written, because the investigator's
    avoidable-loss finding depends on the portfolio-wide aggregate that
    only exists once these rows are in the table.

    Also RETURNS the results keyed by position id, so the investigation
    pass can reuse them instead of re-running the whole simulation. The
    engine is deterministic, so recomputing would produce identical rows
    - it would simply cost a second full pass over every price path for
    nothing.
    """
    saved = 0
    thresholds = _thresholds(settings)
    by_position: dict[str, list[CounterfactualResult]] = {}
    for position in positions:
        observations = repo.find_guardian_observations_for_position(position.position_id)
        points = reconstruct_price_path(position, observations)
        results = run_counterfactuals(
            position, points, settings.risk_limits, thresholds, now, run_id
        )
        by_position[position.position_id] = results
        for result in results:
            if repo.save_godfather_counterfactual(result):
                saved += 1
    return saved, by_position


def _actionable_sources(
    patterns: list[dict], features: dict[str, object]
) -> set[PredictionErrorSource]:
    """Which lessons this trade is allowed to state as actionable.

    Answered exclusively from the PREVIOUS Experience Memory sweep: a
    source is actionable only when a pattern covering this trade already
    reached EDGE or FAILURE_PATTERN. When Experience Memory is empty -
    which it is for a young system, and honestly so - this returns the
    empty set and every lesson is recorded as OBSERVATION ONLY.

    `patterns` is passed in already loaded: it is the same list for every
    position in a batch, and re-reading the whole table per trade was an
    N+1 over a table each sweep rewrites wholesale.
    """
    edge_class, _expectancy, matched = experience_module.lookup_edge_class(patterns, features)
    if edge_class in ("EDGE", "FAILURE_PATTERN") and matched:
        return {"trade_thesis"}
    return set()


def _load_patterns(repo: Repository) -> list[dict]:
    return [
        {**row, "condition": _load_condition(row)}
        for row in repo.find_godfather_experience_patterns()
    ]


def _load_condition(row: dict) -> dict:
    try:
        parsed = json.loads(row.get("condition_json") or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def run_investigation_batch(
    repo: Repository,
    settings: Settings,
    positions: list[Position],
    counterfactuals_by_position: dict[str, list[CounterfactualResult]],
    now: datetime,
    run_id: str,
) -> dict:
    """Investigate, audit and score prediction errors for a batch."""
    counterfactual_rows = repo.find_godfather_counterfactuals()
    portfolio_effect = aggregate_policy_performance(
        counterfactual_rows, restrict_to=common_scorable_positions(counterfactual_rows)
    )
    patterns = _load_patterns(repo)

    investigated = 0
    audited = 0
    prediction_errors = 0
    for position in positions:
        candidate = _safe_candidate(repo, position.candidate_id)
        gate_decision = repo.get_gate_decision(position.candidate_id)
        opportunity_screen = repo.get_assessment_payload(
            position.candidate_id, "opportunity_screen"
        )
        live_execution = repo.get_live_execution(position.position_id)
        observations = repo.find_guardian_observations_for_position(position.position_id)
        points = reconstruct_price_path(position, observations)
        metrics = compute_path_metrics(
            position,
            points,
            settings.guardian.watch_decay_threshold,
            settings.guardian.exit_decay_threshold,
        )
        realized_pnl = compute_pnl_or_none(position)
        counterfactuals = counterfactuals_by_position.get(position.position_id, [])

        investigation = investigate_position(
            position=position,
            candidate=candidate,
            gate_decision=gate_decision,
            live_execution=live_execution,
            points=points,
            metrics=metrics,
            realized_pnl=realized_pnl,
            counterfactuals=counterfactuals,
            policy_portfolio_effect=portfolio_effect or None,
            now=now,
            run_id=run_id,
        )
        if repo.save_godfather_trade_investigation(investigation):
            investigated += 1

        audit = audit_decision(
            position=position,
            candidate=candidate,
            opportunity_screen=opportunity_screen,
            gate_decision=gate_decision,
            metrics=metrics,
            realized_pnl=realized_pnl,
            classification=investigation.classification,
            entry_verdict=judge_entry(metrics, realized_pnl),
            management_verdict=judge_management(metrics, realized_pnl),
            now=now,
            run_id=run_id,
        )
        if repo.save_godfather_decision_audit(audit):
            audited += 1

        features = build_candidate_features(
            candidate,
            opportunity_screen,
            position.opened_at,
            _regime_for(observations),
        )
        for record in build_prediction_errors(
            investigation, audit, _actionable_sources(patterns, features)
        ):
            if repo.save_godfather_prediction_error(record):
                prediction_errors += 1

    return {
        "investigated": investigated,
        "audited": audited,
        "prediction_errors": prediction_errors,
    }


def build_experience_samples(repo: Repository, settings: Settings) -> list[ExperienceSample]:
    """One sample per stored investigation that has a scorable outcome.

    Built from the investigations table rather than recomputed from
    positions, so Experience Memory and the post-mortems can never be
    looking at two different versions of the same trade.
    """
    samples: list[ExperienceSample] = []
    for row in repo.find_godfather_trade_investigations():
        pnl_raw = row.get("realized_pnl_usdt")
        if pnl_raw is None:
            continue
        position = repo.get_position(str(row["position_id"]))
        if position is None or position.closed_at is None:
            continue
        candidate = _safe_candidate(repo, str(row["candidate_id"]))
        opportunity_screen = repo.get_assessment_payload(
            str(row["candidate_id"]), "opportunity_screen"
        )
        observations = repo.find_guardian_observations_for_position(position.position_id)
        regime = _regime_for(observations)
        features = build_candidate_features(
            candidate, opportunity_screen, position.opened_at, regime
        )
        # The outcome classification is a legitimate GROUPING key for
        # reporting but must never be a pattern FEATURE: it is derived
        # from the outcome, so a pattern conditioned on it would predict
        # the answer with the answer.
        samples.append(
            ExperienceSample(
                position_id=position.position_id,
                closed_at=position.closed_at,
                pnl=Decimal(str(pnl_raw)),
                mfe_pct=_optional_decimal(row.get("mfe_pct")),
                mae_pct=_optional_decimal(row.get("mae_pct")),
                minutes_to_mfe=row.get("minutes_to_mfe"),
                regime=regime,
                features=features,
            )
        )
    return samples


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def run_experience_sweep(
    repo: Repository, settings: Settings, now: datetime, run_id: str
) -> dict:
    """Recompute every pattern verdict from the full investigation history."""
    samples = build_experience_samples(repo, settings)
    config = ExperienceConfig(
        min_sample_size=settings.godfather.experience_min_sample_size,
        min_support=settings.godfather.experience_min_support,
        fdr_q=settings.godfather.experience_fdr_q,
    )
    patterns = build_experience_memory(samples, now, run_id, config)
    for pattern in patterns:
        repo.upsert_godfather_experience_pattern(pattern)
    by_class: dict[str, int] = {}
    for pattern in patterns:
        by_class[pattern.edge_class] = by_class.get(pattern.edge_class, 0) + 1
    return {"samples": len(samples), "patterns": len(patterns), "by_edge_class": by_class}


def run_thesis_tracking(
    repo: Repository, settings: Settings, now: datetime, run_id: str
) -> int:
    """Record the current thesis state of every OPEN position.

    Advisory: `enforced=False` on every row, and
    `validate_action_is_safe` is asserted before the write - so a
    recommendation that would widen a stop or carry an impossible action
    is never persisted at all, let alone acted on.
    """
    thresholds = _thresholds(settings)
    written = 0
    for position in repo.find_open_positions():
        observations = repo.find_guardian_observations_for_position(position.position_id)
        points = reconstruct_price_path(position, observations)
        features = build_thesis_features(position, points, thresholds.max_hold_hours)
        if features is None:
            continue
        decision = evaluate_thesis(position, features, thresholds)
        violations = validate_action_is_safe(position, decision)
        if violations:
            log_event(
                run_id,
                event="godfather_thesis_recommendation_rejected",
                position_id=position.position_id,
                violations=violations,
            )
            continue
        observed_at = points[-1].observed_at
        record = ThesisObservation(
            thesis_id=_thesis_id(position.position_id, observed_at),
            position_id=position.position_id,
            observed_at=observed_at,
            thesis_state=decision.state,
            recommended_action=decision.action,
            enforced=False,
            reason_codes=decision.reason_codes,
            features={
                "minutes_in_trade": features.minutes_in_trade,
                "time_fraction": features.time_fraction,
                "decay_score": str(features.decay_score),
                "progress_ratio": str(features.progress_ratio),
                "unrealized_pnl": str(features.unrealized_pnl),
                "mfe_pct_so_far": (
                    str(features.mfe_pct_so_far) if features.mfe_pct_so_far is not None else None
                ),
                "giveback_ratio_so_far": (
                    str(features.giveback_ratio_so_far)
                    if features.giveback_ratio_so_far is not None
                    else None
                ),
                "minutes_since_mfe": features.minutes_since_mfe,
                "proposed_stop_loss": (
                    str(decision.proposed_stop_loss)
                    if decision.proposed_stop_loss is not None
                    else None
                ),
                "guardian_factors": features.factors,
            },
            run_id=run_id,
        )
        if repo.save_godfather_position_thesis(record):
            written += 1
    return written


def run_entry_quality_backfill(
    repo: Repository, settings: Settings, now: datetime, run_id: str, limit: int
) -> int:
    """Score the entry quality of positions the system ALREADY took.

    This is what makes the layer testable before it is ever enforced: run
    it over the real book, then compare the P/L of the trades it would
    have kept against the P/L of the ones it would have blocked
    (`entry_quality.backtest_entry_quality`). A filter is only worth
    switching on if that comparison is favourable, and this is how that
    question gets a number instead of an opinion.
    """
    patterns = _load_patterns(repo)
    written = 0
    for row in repo.find_godfather_trade_investigations():
        if written >= limit:
            break
        candidate_id = str(row["candidate_id"])
        if repo.get_godfather_entry_quality(candidate_id) is not None:
            continue
        position = repo.get_position(str(row["position_id"]))
        candidate = _safe_candidate(repo, candidate_id)
        if position is None or candidate is None:
            continue
        opportunity_screen = repo.get_assessment_payload(candidate_id, "opportunity_screen")
        observations = repo.find_guardian_observations_for_position(position.position_id)
        regime = _regime_for(observations)
        features = build_candidate_features(
            candidate, opportunity_screen, position.opened_at, regime
        )
        assessment = assess_entry_quality(
            candidate=candidate,
            features=features,
            conflicts=_audit_conflicts(repo, position.position_id),
            experience_patterns=patterns,
            planned_entry=position.simulated_fill_entry,
            stop_loss=position.stop_loss,
            target=position.target,
            size=position.size,
            risk_limits=settings.risk_limits,
            now=now,
            run_id=run_id,
            regime_compatible=(None if regime == "unknown" else regime in ("btc_strong", "btc_ok")),
        )
        if repo.save_godfather_entry_quality(assessment):
            written += 1
    return written


def _audit_conflicts(repo: Repository, position_id: str) -> list[str]:
    """The conflict codes the Decision Auditor already counted for this
    trade. Read back rather than recomputed, so the entry-quality
    penalties and the audit record can never disagree about what was
    contradictory before entry."""
    audit = repo.get_godfather_decision_audit(position_id)
    if audit is None:
        return []
    try:
        payload = json.loads(audit.get("conflicts_json") or "[]")
    except (ValueError, TypeError):
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []


def run_godfather_intelligence_tick(
    repo: Repository,
    settings: Settings,
    now: datetime,
    run_id: str,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
) -> dict:
    """One full pass. Returns a summary dict for logging and reporting."""
    if not settings.godfather.intelligence_enabled:
        return {"skipped": "godfather.intelligence_enabled is false"}

    pending = repo.find_closed_positions_pending_godfather_investigation(batch_limit)
    counterfactuals, by_position = run_counterfactual_batch(
        repo, settings, pending, now, run_id
    )
    investigation = run_investigation_batch(
        repo, settings, pending, by_position, now, run_id
    )
    sweep = run_experience_sweep(repo, settings, now, run_id)
    thesis_rows = run_thesis_tracking(repo, settings, now, run_id)
    entry_quality_rows = run_entry_quality_backfill(
        repo, settings, now, run_id, settings.godfather.entry_quality_backfill_limit
    )
    summary = {
        "pending_before": len(pending),
        "counterfactuals_saved": counterfactuals,
        **investigation,
        "experience": sweep,
        "thesis_rows": thesis_rows,
        "entry_quality_rows": entry_quality_rows,
        "still_pending": repo.count_closed_positions_pending_godfather_investigation(),
    }
    log_event(run_id, event="godfather_intelligence_tick", **summary)
    return summary
