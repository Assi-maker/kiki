"""The closed-trade book, loaded once, the same way for every consumer.

The supervisor sweep, the Experience Builder and the pipeline all need
the same view of a closed trade: its real P/L, its real price path, its
candidate and its pre-entry features. One loader means they can never
disagree about which trades exist or what a trade's features were.

Two conventions are enforced here rather than by each caller:

* a zero-size (exposure-blocked) position has P/L None - UNAVAILABLE,
  never a neutral 0 that would teach Experience Memory these setups are
  "flat";
* features come from the candidate's evidence record only when that
  evidence was evaluated at or before entry (`features_valid`). On the
  2026-09-25 audit that held for 148 of 148 positions; the check exists
  so that a future ordering bug cannot leak post-entry data silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from crypto_trading.godfather.features import btc_regime_bucket, build_candidate_features
from crypto_trading.godfather.observation import (
    MonitoringGap,
    TradeObservation,
    classify_observation,
    monitoring_gaps,
)
from crypto_trading.godfather.path import PathPoint, reconstruct_price_path
from crypto_trading.godfather.risk_units import RMultiple, compute_r, initial_stop_loss
from crypto_trading.paper_trading.execution import compute_pnl_or_none
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def safe_candidate(repo: Repository, candidate_id: str) -> Candidate | None:
    """A corrupt candidate row is context, not control flow - the same
    non-fatal treatment `guardian/self_improvement.py` already gives it."""
    try:
        return repo.get_candidate(candidate_id)
    except CorruptCandidateStateError:
        return None


def regime_for(observations: list[dict]) -> str:
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
    features_valid: bool = True
    live: bool = False
    live_execution: dict | None = None
    r: RMultiple | None = None
    observation: TradeObservation | None = None

    @property
    def scorable(self) -> bool:
        """Usable as the ACTUAL side of a path simulation: real paper P/L,
        exposure, a price path, and an exit that was actually seen."""
        return (
            self.pnl is not None
            and self.position.size != _ZERO
            and bool(self.points)
            and (self.observation is None or (
                self.observation.outcome_usable and self.observation.outcome_source == "PAPER"
            ))
        )

    @property
    def outcome_r(self) -> Decimal | None:
        """Net R when the outcome is usable evidence, else None."""
        if self.observation is not None and not self.observation.outcome_usable:
            return None
        return self.r.r_net if self.r is not None else None


def load_gaps(repo: Repository) -> list[MonitoringGap]:
    return monitoring_gaps(
        repo.find_runs_by_type("monitoring"), repo.find_runs_by_type("monitoring_catchup")
    )


def load_book(repo: Repository, fee_pct: Decimal | None = None) -> list[TradeContext]:
    """`fee_pct` (the configured paper fee) is used only to net an outcome
    that has to come from exchange fills; without it such an outcome has
    no net R."""
    gaps = load_gaps(repo)
    book: list[TradeContext] = []
    for position in repo.find_closed_positions():
        observations = repo.find_guardian_observations_for_position(position.position_id)
        candidate = safe_candidate(repo, position.candidate_id)
        screen = repo.get_assessment_payload(position.candidate_id, "opportunity_screen")
        regime = regime_for(observations)
        valid = (
            candidate is None
            or candidate.evidence_record.evaluated_at <= position.opened_at
        )
        live_execution = repo.get_live_execution(position.position_id)
        pnl = None if position.size == _ZERO else compute_pnl_or_none(position)
        points = reconstruct_price_path(position, observations)
        features = (
            build_candidate_features(candidate, screen, position.opened_at, regime)
            if valid else {}
        )
        sl, sl_problem = initial_stop_loss(
            position, repo.find_guardian_authority_decisions_for_position(position.position_id)
        )
        r = compute_r(position, sl, pnl) if sl_problem is None else RMultiple(
            "UNAVAILABLE", sl_problem
        )
        opened_run = repo.get_position_opened_run(position.position_id)
        run_end = (
            datetime.fromisoformat(opened_run["completed_at"])
            if opened_run and opened_run.get("completed_at") else None
        )
        observation = classify_observation(
            position, points, pnl, live_execution, gaps, run_end, bool(features),
            r.status == "AVAILABLE", r.r_net,
        )
        if observation.outcome_source == "EXCHANGE" and sl_problem is None:
            # The paper outcome is missing or unverified; the exchange fill
            # is the observed truth for this trade.
            r = compute_r(
                position, sl, None,
                exit_price=_decimal(live_execution.get("exchange_fill_exit")),
                entry_price=_decimal(live_execution.get("exchange_fill_entry")),
                fee_pct=fee_pct,
            )
            observation = classify_observation(
                position, points, pnl, live_execution, gaps, run_end, bool(features),
                r.status == "AVAILABLE", r.r_net,
            )
            fill = _decimal(live_execution.get("exchange_fill_entry"))
            if fill is not None and position.simulated_fill_entry > 0 and abs(
                fill - position.simulated_fill_entry
            ) / position.simulated_fill_entry > Decimal("0.01"):
                # The LIVE fill came >1% away from the planned entry (the
                # paper entry is priced at decision time, the fill happens
                # after the analysis). R still uses the planned risk.
                observation.reasons.append("LIVE_ENTRY_DRIFT_GT_1PCT")
        book.append(TradeContext(
            position=position,
            candidate=candidate,
            opportunity_screen=screen,
            gate_decision=repo.get_gate_decision(position.candidate_id),
            observations=observations,
            points=points,
            pnl=pnl if observation.outcome_usable and observation.outcome_source == "PAPER"
            else None,
            regime=regime,
            features=features,
            features_valid=valid,
            live=live_execution is not None,
            live_execution=live_execution,
            r=r,
            observation=observation,
        ))
    book.sort(key=lambda t: t.position.opened_at)
    return book
