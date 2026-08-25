from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.common import PositionStatus

Direction = Literal["LONG", "SHORT"]


class Position(BaseModel):
    position_id: str
    candidate_id: str
    instrument: str
    direction: Direction
    status: PositionStatus
    theoretical_entry: Decimal
    simulated_fill_entry: Decimal
    stop_loss: Decimal
    target: Decimal
    size: Decimal
    fill_model_version: str
    opened_at: datetime
    theoretical_exit: Decimal | None = None
    simulated_fill_exit: Decimal | None = None
    exit_reason: str | None = None
    fees: Decimal | None = None
    funding: Decimal | None = None
    closed_at: datetime | None = None
