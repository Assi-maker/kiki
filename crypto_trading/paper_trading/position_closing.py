from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.paper_trading.execution import compute_fees, compute_fill_price, compute_funding
from crypto_trading.paper_trading.monitoring import check_exit_trigger
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Direction, Position
from crypto_trading.storage.repository import Repository

_DIRECTION: Direction = "LONG"  # se position_opening.py - LONG-only i denna fas


def close_triggered_positions(
    repo: Repository,
    price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]],
    now: datetime,
    risk_limits: RiskLimitsConfig,
    run_id: str,
) -> list[Position]:
    """Itererar repo.find_open_positions() (redan idempotent - en stängd
    position dyker aldrig upp igen där, SPEC §8.6). price_lookup:
    instrument -> (candle_low, candle_high, current_price, funding_rate)."""
    closed: list[Position] = []
    for position in repo.find_open_positions():
        if position.instrument not in price_lookup:
            continue
        candle_low, candle_high, current_price, funding_rate = price_lookup[position.instrument]

        trigger = check_exit_trigger(
            position,
            candle_low,
            candle_high,
            current_price,
            now,
            risk_limits.max_position_hold_hours,
        )
        if trigger is None:
            continue
        exit_reason, theoretical_exit = trigger

        simulated_fill_exit = compute_fill_price(
            theoretical_exit, _DIRECTION, risk_limits.spread_pct, risk_limits.slippage_pct, "exit"
        )
        fees = compute_fees(position.size, risk_limits.fee_pct)
        hold_hours = Decimal(str((now - position.opened_at).total_seconds())) / Decimal("3600")
        funding = compute_funding(position.size, funding_rate, hold_hours)

        event = Event(
            event_id=f"POSITION_CLOSED:{position.position_id}",
            event_type="POSITION_CLOSED",
            aggregate_type="position",
            aggregate_id=position.position_id,
            occurred_at=now,
            run_id=run_id,
            schema_version=1,
            payload={"exit_reason": exit_reason},
        )
        repo.close_position_with_event(
            position_id=position.position_id,
            theoretical_exit=theoretical_exit,
            simulated_fill_exit=simulated_fill_exit,
            exit_reason=exit_reason,
            fees=fees,
            funding=funding,
            closed_at=now,
            event=event,
        )
        closed.append(repo.get_position(position.position_id))
    return closed
