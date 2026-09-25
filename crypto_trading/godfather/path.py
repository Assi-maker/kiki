"""Real price-path reconstruction for an already-closed position.

The user's explicit requirement was "use real data - we already have
MFE/MAE, Guardian history, PP history, price paths; do not rebuild what
exists". This module is the adapter that makes that true: it turns the
21k+ append-only rows in `guardian_observations` (one per position per
~60s tick) back into the actual price path the position lived through,
without fetching anything, without inventing a single tick, and without
any new storage.

The reconstruction is exact, not approximate. `guardian/deterministic.py`
records `unrealized_pnl = size * (price - entry) / entry`, so

    price = entry * (1 + unrealized_pnl / size)

inverts it with no information loss. `progress_ratio` gives a completely
independent second expression of the same price
(`price = entry + progress_ratio * (target - entry)`), which
`reconstruct_price_path` uses as a cross-check and as the fallback for a
zero-size (exposure-blocked) position where the first formula divides by
zero.

Everything here is a pure function over already-persisted rows. Nothing
in this module writes anything, reads a connector, or looks at a price
the position had not yet reached - see `counterfactual.py` for the
no-lookahead discipline that depends on that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")

# Two independent reconstructions of the same price may differ by
# rounding only. A larger disagreement means one of the two source
# columns is not what this module thinks it is, which is a data-integrity
# signal worth surfacing rather than silently averaging away.
_CROSS_CHECK_TOLERANCE_PCT = Decimal("0.005")


@dataclass(frozen=True)
class PathPoint:
    """One observed moment in the life of a position. `price` is the real
    mark price at `observed_at`, re-derived from what Guardian already
    persisted."""

    observed_at: datetime
    minutes_in_trade: float
    price: Decimal
    unrealized_pnl: Decimal
    progress_ratio: Decimal
    decay_score: Decimal
    state: str
    factors: dict[str, float]
    price_cross_check_ok: bool


@dataclass(frozen=True)
class PathMetrics:
    """Everything the post-mortem needs about what the price actually did.

    All `*_pct` values are signed in the position's own favour direction
    (positive = good for this position), so a LONG and a SHORT read the
    same way. `None` means "not observable from the data we have" - never
    a substituted zero.
    """

    point_count: int
    first_observed_minutes: float | None
    last_observed_minutes: float | None
    mfe_price: Decimal | None
    mae_price: Decimal | None
    mfe_pct: Decimal | None
    mae_pct: Decimal | None
    mfe_pnl: Decimal | None
    mae_pnl: Decimal | None
    minutes_to_mfe: float | None
    minutes_to_mae: float | None
    minutes_to_target_touch: float | None
    minutes_to_sl_touch: float | None
    max_progress_ratio: Decimal | None
    final_progress_ratio: Decimal | None
    final_unrealized_pnl: Decimal | None
    giveback_pnl: Decimal | None
    giveback_ratio: Decimal | None
    minutes_since_mfe_at_close: float | None
    first_questionable_minutes: float | None
    first_invalid_minutes: float | None
    max_decay_score: Decimal | None
    cross_check_failures: int


_EMPTY_METRICS = PathMetrics(
    point_count=0,
    first_observed_minutes=None,
    last_observed_minutes=None,
    mfe_price=None,
    mae_price=None,
    mfe_pct=None,
    mae_pct=None,
    mfe_pnl=None,
    mae_pnl=None,
    minutes_to_mfe=None,
    minutes_to_mae=None,
    minutes_to_target_touch=None,
    minutes_to_sl_touch=None,
    max_progress_ratio=None,
    final_progress_ratio=None,
    final_unrealized_pnl=None,
    giveback_pnl=None,
    giveback_ratio=None,
    minutes_since_mfe_at_close=None,
    first_questionable_minutes=None,
    first_invalid_minutes=None,
    max_decay_score=None,
    cross_check_failures=0,
)


def _direction_sign(position: Position) -> Decimal:
    return Decimal("-1") if position.direction == "SHORT" else Decimal("1")


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _price_from_pnl(position: Position, pnl: Decimal) -> Decimal | None:
    """Inverse of guardian/deterministic.py::compute_unrealized_pnl."""
    if position.size == _ZERO:
        return None
    return position.simulated_fill_entry * (Decimal("1") + (pnl / position.size))


def _price_from_progress(position: Position, progress: Decimal) -> Decimal | None:
    """Inverse of guardian/deterministic.py::compute_progress_ratio."""
    span = position.target - position.simulated_fill_entry
    if span == _ZERO:
        return None
    return position.simulated_fill_entry + (progress * span)


def _cross_check(primary: Decimal, secondary: Decimal | None) -> bool:
    if secondary is None or primary == _ZERO:
        return True
    return abs(primary - secondary) / abs(primary) <= _CROSS_CHECK_TOLERANCE_PCT


def _safe_factors(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def reconstruct_price_path(position: Position, observations: list[dict]) -> list[PathPoint]:
    """Chronological real price path, one point per usable Guardian
    observation row.

    A row this function cannot turn into a price (both reconstructions
    unavailable, unparseable numerics, missing timestamp) is DROPPED, not
    guessed - the resulting `PathMetrics.point_count` then honestly
    reports thinner coverage instead of the analysis silently resting on
    invented ticks.
    """
    points: list[PathPoint] = []
    for row in observations:
        observed_at = row.get("observed_at")
        # A Guardian tick can land a fraction of a second - occasionally
        # up to tens of minutes, for a LIVE-mirrored close - AFTER the
        # position was already closed (34 of 122 real positions in
        # data/crypto_trading.db, audited 2026-09-25). Those rows are not
        # part of this position's life; keeping them would silently feed
        # post-close prices into MFE/MAE and into the counterfactual
        # engine's "information available at time T", which is exactly
        # the lookahead this whole subsystem promises never to do.
        if isinstance(observed_at, str):
            try:
                observed_at = datetime.fromisoformat(observed_at)
            except ValueError:
                continue
        if not isinstance(observed_at, datetime):
            continue
        if position.closed_at is not None and observed_at > position.closed_at:
            continue
        pnl = _to_decimal(row.get("unrealized_pnl"))
        progress = _to_decimal(row.get("progress_ratio"))
        decay = _to_decimal(row.get("decay_score"))
        if pnl is None or progress is None or decay is None:
            continue
        price = _price_from_pnl(position, pnl)
        alternative = _price_from_progress(position, progress)
        cross_ok = True
        if price is None:
            price = alternative
        else:
            cross_ok = _cross_check(price, alternative)
        if price is None:
            continue
        factors = row.get("factors")
        if isinstance(factors, str):
            factors = _safe_factors(factors)
        if not isinstance(factors, dict):
            factors = {}
        minutes = (observed_at - position.opened_at).total_seconds() / 60
        points.append(
            PathPoint(
                observed_at=observed_at,
                minutes_in_trade=minutes,
                price=price,
                unrealized_pnl=pnl,
                progress_ratio=progress,
                decay_score=decay,
                state=str(row.get("state") or "UNKNOWN"),
                factors={k: float(v) for k, v in factors.items() if _is_number(v)},
                price_cross_check_ok=cross_ok,
            )
        )
    points.sort(key=lambda p: p.observed_at)
    return points


def compute_path_metrics(
    position: Position,
    points: list[PathPoint],
    watch_threshold: Decimal,
    exit_threshold: Decimal,
) -> PathMetrics:
    """MFE/MAE/time-to-event/giveback over the real path.

    `watch_threshold`/`exit_threshold` are Guardian's own already-live
    decay thresholds (`config/guardian.yaml`), passed in rather than
    re-declared here so "when did this first become questionable" means
    exactly what the running system already means by WATCH/EXIT - this
    module never invents a second, competing definition of deterioration.
    """
    if not points:
        return _EMPTY_METRICS

    sign = _direction_sign(position)
    entry = position.simulated_fill_entry

    best = max(points, key=lambda p: sign * (p.price - entry))
    worst = min(points, key=lambda p: sign * (p.price - entry))
    last = points[-1]

    def _pct(price: Decimal) -> Decimal | None:
        if entry == _ZERO:
            return None
        return sign * (price - entry) / entry * Decimal("100")

    target_touch = next((p for p in points if sign * (p.price - position.target) >= _ZERO), None)
    sl_touch = next((p for p in points if sign * (p.price - position.stop_loss) <= _ZERO), None)
    questionable = next((p for p in points if p.decay_score >= watch_threshold), None)
    invalid = next((p for p in points if p.decay_score >= exit_threshold), None)

    mfe_pnl = best.unrealized_pnl
    giveback = None
    giveback_ratio = None
    if mfe_pnl > _ZERO:
        giveback = mfe_pnl - last.unrealized_pnl
        giveback_ratio = giveback / mfe_pnl

    return PathMetrics(
        point_count=len(points),
        first_observed_minutes=points[0].minutes_in_trade,
        last_observed_minutes=last.minutes_in_trade,
        mfe_price=best.price,
        mae_price=worst.price,
        mfe_pct=_pct(best.price),
        mae_pct=_pct(worst.price),
        mfe_pnl=mfe_pnl,
        mae_pnl=worst.unrealized_pnl,
        minutes_to_mfe=best.minutes_in_trade,
        minutes_to_mae=worst.minutes_in_trade,
        minutes_to_target_touch=(
            target_touch.minutes_in_trade if target_touch is not None else None
        ),
        minutes_to_sl_touch=(sl_touch.minutes_in_trade if sl_touch is not None else None),
        max_progress_ratio=max(p.progress_ratio for p in points),
        final_progress_ratio=last.progress_ratio,
        final_unrealized_pnl=last.unrealized_pnl,
        giveback_pnl=giveback,
        giveback_ratio=giveback_ratio,
        minutes_since_mfe_at_close=last.minutes_in_trade - best.minutes_in_trade,
        first_questionable_minutes=(
            questionable.minutes_in_trade if questionable is not None else None
        ),
        first_invalid_minutes=(invalid.minutes_in_trade if invalid is not None else None),
        max_decay_score=max(p.decay_score for p in points),
        cross_check_failures=sum(1 for p in points if not p.price_cross_check_ok),
    )
