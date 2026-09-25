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
from decimal import Decimal

from crypto_trading.godfather.features import btc_regime_bucket, build_candidate_features
from crypto_trading.godfather.path import PathPoint, reconstruct_price_path
from crypto_trading.paper_trading.execution import compute_pnl_or_none
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")


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

    @property
    def scorable(self) -> bool:
        return self.pnl is not None and self.position.size != _ZERO and bool(self.points)


def load_book(repo: Repository) -> list[TradeContext]:
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
        book.append(TradeContext(
            position=position,
            candidate=candidate,
            opportunity_screen=screen,
            gate_decision=repo.get_gate_decision(position.candidate_id),
            observations=observations,
            points=reconstruct_price_path(position, observations),
            pnl=None if position.size == _ZERO else compute_pnl_or_none(position),
            regime=regime,
            features=(
                build_candidate_features(candidate, screen, position.opened_at, regime)
                if valid else {}
            ),
            features_valid=valid,
            live=repo.get_live_execution(position.position_id) is not None,
        ))
    book.sort(key=lambda t: t.position.opened_at)
    return book
