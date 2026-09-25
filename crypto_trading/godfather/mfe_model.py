"""MFE/MAE-centric context: "what usually happens next, from here?"

For an open position that has already moved X% in its favour, position
management needs three numbers, and all three must come from real history
rather than from a rule of thumb:

* how much FURTHER favourable movement similar trades went on to make,
* how often similar trades came all the way back to the entry price,
* how much of their best move they gave back by the close.

This module answers them empirically. For every closed trade it records
the first moment the trade crossed each of a few pre-declared favourable
levels (0.5 / 1 / 2 / 3 %, structural round numbers, not fitted) and
what happened AFTER that moment. An estimate for a live position at
level L is then the distribution of those after-the-fact outcomes over
the historical trades that also reached L.

**No lookahead, twice over.** Historical trades contribute only when
they CLOSED before the moment being estimated (`as_of`), so replaying a
decision from last week never uses a trade that closed yesterday. And an
observation is dropped - not guessed - when Guardian was not watching
after the crossing (a hole longer than `MAX_UNOBSERVED_MINUTES`): a
reversal inside a hole is exactly the event this module exists to count.

**No rule.** Nothing here maps "MFE = X" to an action. `position_decision`
combines these estimates with the thesis state, and whether that
combination pays is decided by the counterfactual engine and the policy
registry, not asserted here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from crypto_trading.godfather.path import PathPoint
from crypto_trading.godfather.stop_simulation import MAX_UNOBSERVED_MINUTES, excursion
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")

MFE_LEVELS_PCT: tuple[Decimal, ...] = (
    Decimal("0.5"), Decimal("1.0"), Decimal("2.0"), Decimal("3.0"),
)

# Below this many historical trades at a level, the answer is
# INSUFFICIENT_DATA and the decision layer must not use it.
MIN_SAMPLES = 20


@dataclass(frozen=True)
class MfeObservation:
    position_id: str
    closed_at: datetime
    level_pct: Decimal
    minutes_to_level: float
    further_mfe_pct: Decimal
    reverted_to_entry: bool
    final_return_pct: Decimal
    final_giveback_ratio: Decimal | None
    hit_target: bool


@dataclass(frozen=True)
class MfeEstimate:
    status: str  # ESTIMATE | INSUFFICIENT_DATA | NO_FAVOURABLE_MOVE_YET
    level_pct: Decimal | None
    n: int
    p_further_1pct: float | None = None
    median_further_mfe_pct: float | None = None
    p_revert_to_entry: float | None = None
    median_final_giveback_ratio: float | None = None
    p_target: float | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "level_pct": None if self.level_pct is None else str(self.level_pct),
            "n": self.n,
            "p_further_1pct": self.p_further_1pct,
            "median_further_mfe_pct": self.median_further_mfe_pct,
            "p_revert_to_entry": self.p_revert_to_entry,
            "median_final_giveback_ratio": self.median_final_giveback_ratio,
            "p_target": self.p_target,
        }


def observations_for_trade(position: Position, points: list[PathPoint]) -> list[MfeObservation]:
    """One observation per level the trade reached, measured from the
    first tick at or above that level to the real close."""
    if (
        not points
        or position.size == _ZERO
        or position.closed_at is None
        or position.theoretical_exit is None
        or position.simulated_fill_entry == _ZERO
    ):
        return []
    entry = position.simulated_fill_entry
    close_minutes = (position.closed_at - position.opened_at).total_seconds() / 60
    final = excursion(position.theoretical_exit, entry) * _HUNDRED
    moves = [excursion(p.price, entry) * _HUNDRED for p in points]
    trade_mfe = max([*moves, final])
    hit_target = (position.exit_reason or "").lower() == "target"

    observations: list[MfeObservation] = []
    for level in MFE_LEVELS_PCT:
        index = next((i for i, m in enumerate(moves) if m >= level), None)
        if index is None:
            continue
        later_minutes = [p.minutes_in_trade for p in points[index:]] + [close_minutes]
        worst_gap = max(
            (b - a for a, b in zip(later_minutes, later_minutes[1:], strict=False)),
            default=0.0,
        )
        if worst_gap > MAX_UNOBSERVED_MINUTES:
            continue
        after = [*moves[index + 1 :], final]
        observations.append(
            MfeObservation(
                position_id=position.position_id,
                closed_at=position.closed_at,
                level_pct=level,
                minutes_to_level=points[index].minutes_in_trade,
                further_mfe_pct=max(_ZERO, max(after) - moves[index]),
                reverted_to_entry=min(after) <= _ZERO,
                final_return_pct=final,
                final_giveback_ratio=(
                    (trade_mfe - final) / trade_mfe if trade_mfe > _ZERO else None
                ),
                hit_target=hit_target,
            )
        )
    return observations


class MfeModel:
    def __init__(self, observations: list[MfeObservation]) -> None:
        self._observations = list(observations)

    @property
    def observations(self) -> list[MfeObservation]:
        return list(self._observations)

    def as_of(self, moment: datetime) -> MfeModel:
        """Only trades that had CLOSED strictly before `moment`."""
        return MfeModel([o for o in self._observations if o.closed_at < moment])

    def estimate(self, current_mfe_pct: Decimal | None) -> MfeEstimate:
        if current_mfe_pct is None:
            return MfeEstimate("NO_FAVOURABLE_MOVE_YET", None, 0)
        reached = [level for level in MFE_LEVELS_PCT if current_mfe_pct >= level]
        if not reached:
            return MfeEstimate("NO_FAVOURABLE_MOVE_YET", None, 0)
        level = reached[-1]
        sample = [o for o in self._observations if o.level_pct == level]
        if len(sample) < MIN_SAMPLES:
            return MfeEstimate("INSUFFICIENT_DATA", level, len(sample))
        givebacks = [float(o.final_giveback_ratio) for o in sample
                     if o.final_giveback_ratio is not None]
        n = len(sample)
        return MfeEstimate(
            status="ESTIMATE",
            level_pct=level,
            n=n,
            p_further_1pct=sum(1 for o in sample if o.further_mfe_pct >= 1) / n,
            median_further_mfe_pct=float(statistics.median(o.further_mfe_pct for o in sample)),
            p_revert_to_entry=sum(1 for o in sample if o.reverted_to_entry) / n,
            median_final_giveback_ratio=(
                float(statistics.median(givebacks)) if givebacks else None
            ),
            p_target=sum(1 for o in sample if o.hit_target) / n,
        )

    def summary(self) -> list[dict]:
        return [
            {**self.estimate(level).as_dict(), "level_pct": str(level)}
            for level in MFE_LEVELS_PCT
        ]
