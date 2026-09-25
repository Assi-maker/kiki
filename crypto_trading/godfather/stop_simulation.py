"""Shared, no-lookahead simulation of every "move the stop" policy.

Break-even, profit-lock, thesis-driven tighten, the full position
decision policy - they all differ only in WHERE they want the stop at a
given moment. This module owns everything else, once:

* **No lookahead.** A rule is handed `points[: i + 1]` - the path
  observed so far - and nothing else. What it proposes at tick `i` takes
  effect from tick `i + 1`, the same ordering the live mechanism and the
  PAPER shadow experiment have (`profit_protection_experiment.advance_shadow`).
* **Never widen, never remove.** A proposal at or below the original
  stop is ignored, and an armed stop only ever ratchets tighter.
* **Unobservable, never neutral.** Guardian ticks every ~97 s, but the
  path also has holes of up to 90 hours where nothing was running. An
  exchange stop keeps working through such a hole; our record of the
  price does not. A trade whose armed stop spans a hole longer than
  `MAX_UNOBSERVED_MINUTES` is UNOBSERVABLE for that policy. On the first
  real TIGHTEN_SL evaluation five break-even exits were credited inside
  15-74 h holes - one worth +287 USDT on its own - and together they
  flipped the sign of the whole result.
* **Two fill readings.** `pnl_usdt` is a stop-order fill at the stop
  level; `pnl_pessimistic_usdt` fills at the first observed price at or
  below it (the analogue of the paper engine booking stops at candle
  low). A conclusion that flips between them is not a conclusion.

LONG-only, like every execution path in this codebase.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.godfather.costs import simulate_exit_pnl
from crypto_trading.godfather.path import PathPoint
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")

MAX_UNOBSERVED_MINUTES = 10.0

# A rule sees the observed prefix (inclusive of the current tick) and
# proposes the stop it wants from the NEXT tick on, or None.
StopRule = Callable[[list[PathPoint]], Decimal | None]


@dataclass(frozen=True)
class StopPolicy:
    """A threshold stop policy: from `activation_pct` favourable
    excursion, stop = entry + `lock_fraction` x best excursion so far
    (0 = break-even)."""

    name: str
    activation_pct: Decimal
    lock_fraction: Decimal
    role: str
    provenance: str


@dataclass(frozen=True)
class StopSimulation:
    """One stop rule applied to one trade's real path."""

    activated: bool
    activation_index: int | None
    activation_minutes: float | None
    activation_at: datetime | None
    mfe_before_pct: Decimal | None
    mae_before_pct: Decimal | None
    stopped: bool
    stop_minutes: float | None
    stop_level: Decimal | None
    pnl_usdt: Decimal
    pnl_pessimistic_usdt: Decimal
    mae_until_exit_pct: Decimal
    # Longest stretch without an observation while the moved stop was in
    # force. None when the rule never armed a stop.
    max_unobserved_minutes_armed: float | None = None

    @property
    def observable(self) -> bool:
        return (
            self.max_unobserved_minutes_armed is None
            or self.max_unobserved_minutes_armed <= MAX_UNOBSERVED_MINUTES
        )


def excursion(price: Decimal, entry: Decimal) -> Decimal:
    return (price - entry) / entry


class ThresholdRule:
    """`StopPolicy` as a `StopRule`. Keeps its running best excursion
    incrementally, which is only valid if it is fed the prefixes in
    order - so it checks that it is, and refuses anything else."""

    def __init__(self, position: Position, policy: StopPolicy) -> None:
        self._entry = position.simulated_fill_entry
        self._policy = policy
        self._best = _ZERO
        self._seen = 0

    def __call__(self, prefix: list[PathPoint]) -> Decimal | None:
        if len(prefix) != self._seen + 1:
            raise ValueError("ThresholdRule must be fed consecutive prefixes")
        self._seen = len(prefix)
        self._best = max(self._best, excursion(prefix[-1].price, self._entry))
        if self._best < self._policy.activation_pct:
            return None
        return self._entry * (Decimal("1") + self._policy.lock_fraction * self._best)


def simulate_stop_rule(
    position: Position,
    points: list[PathPoint],
    rule: StopRule,
    actual_pnl: Decimal,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
) -> StopSimulation:
    """Walk the real path. Before the rule arms a stop the ORIGINAL stop
    governs, which is exactly what the real trade had - so the no-policy
    outcome is the real outcome and is never simulated; only the rule's
    own exit is priced, through `costs.simulate_exit_pnl`."""
    entry = position.simulated_fill_entry
    best = _ZERO
    worst = _ZERO
    stop: Decimal | None = None
    activation: tuple[int, PathPoint, Decimal, Decimal] | None = None
    armed_gap: float | None = None

    for i, point in enumerate(points):
        if stop is not None:
            gap = point.minutes_in_trade - points[i - 1].minutes_in_trade
            armed_gap = gap if armed_gap is None else max(armed_gap, gap)
            if point.price <= stop:
                return _stopped(
                    position, activation, point, stop, point.price, worst,
                    risk_limits, funding_rate, armed_gap,
                )
        move = excursion(point.price, entry)
        best = max(best, move)
        worst = min(worst, move)
        proposal = rule(points[: i + 1])
        if proposal is None or proposal <= position.stop_loss:
            continue
        if activation is None:
            activation = (i, point, best, worst)
        stop = proposal if stop is None else max(stop, proposal)

    if stop is not None and position.closed_at is not None:
        tail = (position.closed_at - position.opened_at).total_seconds() / 60 - (
            points[-1].minutes_in_trade
        )
        armed_gap = tail if armed_gap is None else max(armed_gap, tail)

    # The real exit is below the stop but no tick caught the crossing: the
    # price passed through the stop in the last interval before the close.
    # A stop order fills there; the pessimistic reading keeps the real
    # exit (grants the policy nothing).
    exit_price = position.theoretical_exit
    if (
        stop is not None
        and exit_price is not None
        and exit_price <= stop
        and position.closed_at is not None
    ):
        close_point = PathPoint(
            observed_at=position.closed_at,
            minutes_in_trade=(position.closed_at - position.opened_at).total_seconds() / 60,
            price=exit_price,
            unrealized_pnl=_ZERO,
            progress_ratio=_ZERO,
            decay_score=_ZERO,
            state="CLOSE",
            factors={},
            price_cross_check_ok=True,
        )
        worst = min(worst, excursion(exit_price, entry))
        return _stopped(
            position, activation, close_point, stop, exit_price, worst,
            risk_limits, funding_rate, armed_gap, pessimistic_is_actual=actual_pnl,
        )

    return StopSimulation(
        activated=activation is not None,
        activation_index=activation[0] if activation else None,
        activation_minutes=activation[1].minutes_in_trade if activation else None,
        activation_at=activation[1].observed_at if activation else None,
        mfe_before_pct=activation[2] * _HUNDRED if activation else None,
        mae_before_pct=activation[3] * _HUNDRED if activation else None,
        stopped=False,
        stop_minutes=None,
        stop_level=stop,
        pnl_usdt=actual_pnl,
        pnl_pessimistic_usdt=actual_pnl,
        mae_until_exit_pct=worst * _HUNDRED,
        max_unobserved_minutes_armed=armed_gap,
    )


def simulate_stop_policy(
    position: Position,
    points: list[PathPoint],
    policy: StopPolicy,
    actual_pnl: Decimal,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
) -> StopSimulation:
    return simulate_stop_rule(
        position, points, ThresholdRule(position, policy), actual_pnl, risk_limits,
        funding_rate,
    )


def _stopped(
    position: Position,
    activation: tuple[int, PathPoint, Decimal, Decimal] | None,
    point: PathPoint,
    stop: Decimal,
    observed_price: Decimal,
    worst: Decimal,
    risk_limits: RiskLimitsConfig,
    funding_rate: Decimal,
    armed_gap: float | None,
    pessimistic_is_actual: Decimal | None = None,
) -> StopSimulation:
    assert activation is not None  # a stop only exists after activation
    pnl = simulate_exit_pnl(position, stop, point.minutes_in_trade, risk_limits, funding_rate)
    if pessimistic_is_actual is not None:
        pessimistic = pessimistic_is_actual
    else:
        pessimistic = simulate_exit_pnl(
            position, min(observed_price, stop), point.minutes_in_trade, risk_limits,
            funding_rate,
        )
    return StopSimulation(
        activated=True,
        activation_index=activation[0],
        activation_minutes=activation[1].minutes_in_trade,
        activation_at=activation[1].observed_at,
        mfe_before_pct=activation[2] * _HUNDRED,
        mae_before_pct=activation[3] * _HUNDRED,
        stopped=True,
        stop_minutes=point.minutes_in_trade,
        stop_level=stop,
        pnl_usdt=pnl,
        pnl_pessimistic_usdt=pessimistic,
        mae_until_exit_pct=min(worst, excursion(stop, position.simulated_fill_entry)) * _HUNDRED,
        max_unobserved_minutes_armed=armed_gap,
    )
