from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.storage.repository import Repository

_PAGE_SIZE = 10_000


class BacktestTarget(BaseModel):
    """One real, already AI/Gate-confirmed historical trade setup, read
    read-only from production. `original_size`/`original_status`/
    `original_exit_reason`/`original_closed_at`/`original_theoretical_exit`/
    `original_simulated_fill_exit` are never used to compute anything in
    the replay itself (Tier 1 always uses a fixed backtest notional,
    decoupled from whatever size the live exposure pool actually gave this
    position) - they exist ONLY for the baseline-parity cross-check in
    report.py (Task 6)."""

    position_id: str
    instrument: str
    entry_price: Decimal
    simulated_fill_entry: Decimal
    stop_loss: Decimal
    target: Decimal
    opened_at: datetime
    original_size: Decimal
    original_status: Literal["OPEN_POSITION", "CLOSED"]
    original_exit_reason: str | None
    original_closed_at: datetime | None
    original_theoretical_exit: Decimal | None
    original_simulated_fill_exit: Decimal | None


def select_backtest_targets(repo: Repository) -> list[BacktestTarget]:
    """Read-only selection of EVERY real historical PAPER position
    (CLOSED and OPEN, size=0 and size>0 alike - see BacktestTarget
    docstring for why size is never a filter here). No exclusions: every
    position in `positions` reached CONFIRMED through the real, unmodified
    Gate/AI pipeline, so every one is a valid entry/stop/target setup."""
    targets: list[BacktestTarget] = []
    offset = 0
    while True:
        page = repo.find_all_positions(limit=_PAGE_SIZE, offset=offset)
        if not page:
            break
        for position in page:
            targets.append(
                BacktestTarget(
                    position_id=position.position_id,
                    instrument=position.instrument,
                    entry_price=position.theoretical_entry,
                    simulated_fill_entry=position.simulated_fill_entry,
                    stop_loss=position.stop_loss,
                    target=position.target,
                    opened_at=position.opened_at,
                    original_size=position.size,
                    original_status=position.status,
                    original_exit_reason=position.exit_reason,
                    original_closed_at=position.closed_at,
                    original_theoretical_exit=position.theoretical_exit,
                    original_simulated_fill_exit=position.simulated_fill_exit,
                )
            )
        offset += _PAGE_SIZE
    return targets
