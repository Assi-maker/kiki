"""Shared builders for the GODFATHER Intelligence Layer tests.

Everything here constructs data in the SAME shape the production tables
hold it - `guardian_observations` rows as dicts of strings, positions as
real `Position` models - so the tests exercise the real parsing paths
rather than a convenient in-memory approximation of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather.path import PathPoint
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    SecondaryTimeframeEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.trade import Position

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
OPENED = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)

ENTRY = Decimal("100")
SIZE = Decimal("1000")


def make_position(
    position_id: str = "pos-1",
    *,
    entry: Decimal = ENTRY,
    stop_loss: Decimal = Decimal("95"),
    target: Decimal = Decimal("110"),
    size: Decimal = SIZE,
    exit_price: Decimal | None = Decimal("97"),
    exit_reason: str | None = "stop_loss",
    fees: Decimal | None = Decimal("0"),
    funding: Decimal | None = Decimal("0"),
    opened_at: datetime = OPENED,
    closed_at: datetime | None = None,
    status: str = "CLOSED",
    direction: str = "LONG",
) -> Position:
    return Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTC-USDT",
        direction=direction,
        status=status,
        theoretical_entry=entry,
        simulated_fill_entry=entry,
        stop_loss=stop_loss,
        target=target,
        size=size,
        fill_model_version="v1",
        opened_at=opened_at,
        theoretical_exit=exit_price,
        simulated_fill_exit=exit_price,
        exit_reason=exit_reason,
        fees=fees,
        funding=funding,
        closed_at=closed_at if closed_at is not None else (opened_at + timedelta(hours=4)),
    )


def observation_row(
    position: Position,
    minutes: float,
    price: Decimal,
    *,
    decay: str = "0.0",
    state: str = "HOLD",
    factors: dict | None = None,
) -> dict:
    """A `guardian_observations` row, with the numbers derived from
    `price` exactly the way the live Guardian tick derives them - so a
    test never asserts against a hand-typed unrealized_pnl that the real
    formula would not have produced."""
    unrealized = position.size * (price - position.simulated_fill_entry) / (
        position.simulated_fill_entry
    )
    span = position.target - position.simulated_fill_entry
    progress = (
        (price - position.simulated_fill_entry) / span if span != 0 else Decimal("0")
    )
    return {
        "observation_id": f"{position.position_id}-{minutes}",
        "position_id": position.position_id,
        "observed_at": (position.opened_at + timedelta(minutes=minutes)).isoformat(),
        "state": state,
        "decay_score": decay,
        "progress_ratio": str(progress),
        "unrealized_pnl": str(unrealized),
        "factors": factors if factors is not None else {},
        "run_id": "test-run",
    }


def path_point(
    minutes: float,
    price: Decimal,
    *,
    position: Position | None = None,
    decay: Decimal = Decimal("0"),
    state: str = "HOLD",
    factors: dict | None = None,
) -> PathPoint:
    position = position or make_position()
    unrealized = position.size * (price - position.simulated_fill_entry) / (
        position.simulated_fill_entry
    )
    span = position.target - position.simulated_fill_entry
    progress = (price - position.simulated_fill_entry) / span if span != 0 else Decimal("0")
    return PathPoint(
        observed_at=position.opened_at + timedelta(minutes=minutes),
        minutes_in_trade=minutes,
        price=price,
        unrealized_pnl=unrealized,
        progress_ratio=progress,
        decay_score=decay,
        state=state,
        factors=factors or {},
        price_cross_check_ok=True,
    )


def evidence_record(
    *,
    instrument: str = "BTC-USDT",
    candidate_score: float = 0.5,
    trigger_reasons: tuple[str, ...] = ("momentum_breakout",),
    rsi: float = 75.0,
    volume_zscore: float = 1.0,
    volume_triggered: bool = False,
    momentum_triggered: bool = True,
    price_volatility_triggered: bool = True,
    secondary_triggered: bool | None = True,
    evaluated_at: datetime = OPENED,
) -> CandidateEvidenceRecord:
    def _volume(triggered: bool) -> VolumeEvidence:
        return VolumeEvidence(
            triggered=triggered,
            metric="volume_zscore",
            value=volume_zscore,
            baseline=0.0,
            threshold=2.5,
        )

    def _momentum(triggered: bool) -> MomentumBreakoutEvidence:
        return MomentumBreakoutEvidence(
            triggered=triggered, metric="rsi", value=rsi, baseline=50.0, threshold=70.0
        )

    def _price(triggered: bool) -> PriceVolatilityEvidence:
        return PriceVolatilityEvidence(
            triggered=triggered,
            metric="pct_change",
            value=3.0,
            baseline=1.0,
            threshold=2.0,
        )

    def _funding(triggered: bool) -> FundingOpenInterestEvidence:
        return FundingOpenInterestEvidence(
            triggered=triggered,
            metric="funding_rate_pct",
            value=0.005,
            baseline=0.004,
            threshold=0.05,
        )

    secondary = None
    if secondary_triggered is not None:
        secondary = SecondaryTimeframeEvidence(
            timeframe="1h",
            price_volatility_evidence=_price(secondary_triggered),
            momentum_breakout_evidence=_momentum(secondary_triggered),
            volume_evidence=_volume(False),
            funding_oi_evidence=_funding(False),
        )

    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=["30m", "1h"],
        evaluated_at=evaluated_at,
        price_volatility_evidence=_price(price_volatility_triggered),
        momentum_breakout_evidence=_momentum(momentum_triggered),
        volume_evidence=_volume(volume_triggered),
        funding_oi_evidence=_funding(False),
        secondary_timeframe_evidence=secondary,
        candidate_score=candidate_score,
        trigger_reasons=list(trigger_reasons),
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )
