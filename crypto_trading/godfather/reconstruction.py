"""Reconstruction from exchange history, and the trade timeline (Fas 2A.1).

Two jobs, both pure functions over data already in the database:

**1. What did the market actually do?** `exchange_klines_1m` holds the
real 1m candles of every traded window. From them:

* `replay_exit` re-runs the paper engine's own exit rule - stop first,
  then target, then the hard time limit, stops filled at
  min(low, stop) exactly like `check_exit_trigger` - from the moment the
  position existed. A missing minute BEFORE the exit makes the replay
  INCOMPLETE rather than letting it skip over a possible trigger.
* `verify_exit` compares that replay with the booked exit: MATCH,
  MISMATCH (the booked exit is wrong - typically decided after an
  outage), or UNVERIFIABLE.
* `candle_path` gives MFE / MAE / time-to-event from real highs and lows
  at 1m resolution, counting only candles that OPEN after the position
  existed - nothing from before creation leaks in.

**2. When did what happen?** `TradeTimeline` keeps every timestamp and
price level of a trade apart, because they were being conflated:
`positions.opened_at` and the paper entry are the START of the deciding
discovery run, while the position existed 10-27 minutes later and a LIVE
fill later still. Experience measures the ACTUAL position (actual entry,
from activation); the signal/decision timestamps and the planned entry
are kept separately so Entry Quality can later ask whether the signal was
good at discovery and whether it decayed during the analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_ONE_MINUTE = timedelta(minutes=1)
_MATCH_TOLERANCE = timedelta(minutes=3)


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def candles_from_rows(rows: list[dict]) -> list[Candle]:
    return [
        Candle(
            open_time=datetime.fromisoformat(r["open_time"]),
            open=Decimal(r["open"]), high=Decimal(r["high"]),
            low=Decimal(r["low"]), close=Decimal(r["close"]),
        )
        for r in rows
    ]


def after(candles: list[Candle], start: datetime) -> list[Candle]:
    """Candles that OPEN at or after `start` - the first one fully inside
    the position's life."""
    return [c for c in candles if c.open_time >= start]


def missing_windows(
    candles: list[Candle], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Stretches of more than one minute with no candle in [start, end]."""
    edges = [start] + [c.open_time for c in candles if start <= c.open_time <= end] + [end]
    return [
        (a, b) for a, b in zip(edges, edges[1:], strict=False)
        if b - a > _ONE_MINUTE + _ONE_MINUTE
    ]


@dataclass(frozen=True)
class KlineReplay:
    status: str  # EXIT_FOUND | NO_PRICE_EXIT | INCOMPLETE_HISTORY | NO_HISTORY
    reason: str | None = None
    exit_time: datetime | None = None
    exit_price: Decimal | None = None


def replay_exit(
    candles: list[Candle],
    start: datetime,
    end: datetime,
    stop_loss: Decimal,
    target: Decimal,
    deadline: datetime,
) -> KlineReplay:
    """The paper engine's exit rule over real candles, LONG-only like the
    engine itself. `end` is how far to look (the booked close, padded)."""
    window = [c for c in after(candles, start) if c.open_time <= end]
    if not window:
        return KlineReplay("NO_HISTORY")
    previous = start
    for candle in window:
        if candle.open_time - previous > _ONE_MINUTE + _ONE_MINUTE:
            # A hole before any trigger: the trigger may be inside it.
            return KlineReplay("INCOMPLETE_HISTORY")
        previous = candle.open_time
        if candle.low <= stop_loss:
            return KlineReplay("EXIT_FOUND", "stop_loss", candle.open_time,
                               min(candle.low, stop_loss))
        if candle.high >= target:
            return KlineReplay("EXIT_FOUND", "target", candle.open_time, target)
        if candle.open_time >= deadline:
            return KlineReplay("EXIT_FOUND", "time_limit", candle.open_time, candle.close)
    return KlineReplay("NO_PRICE_EXIT")


def verify_exit(
    replay: KlineReplay, booked_reason: str | None, booked_time: datetime | None
) -> str:
    """MATCH / MISMATCH / UNVERIFIABLE for the booked exit."""
    booked = (booked_reason or "").lower()
    if replay.status in ("NO_HISTORY", "INCOMPLETE_HISTORY") or booked_time is None:
        return "UNVERIFIABLE"
    if replay.status == "NO_PRICE_EXIT":
        # No stop/target/deadline touch up to the booked close: consistent
        # with an exit that was not price-triggered (Guardian exit, manual).
        return "MATCH" if booked in ("guardian_exit", "manual_close_stale_signal") else "MISMATCH"
    same_reason = replay.reason == booked
    close_in_time = abs(replay.exit_time - booked_time) <= _MATCH_TOLERANCE
    if same_reason and close_in_time:
        return "MATCH"
    if booked == "guardian_exit" and replay.exit_time > booked_time:
        return "MATCH"  # Guardian closed it before any price trigger
    return "MISMATCH"


def candle_path(
    candles: list[Candle], start: datetime, end: datetime, entry: Decimal,
    stop_loss: Decimal, target: Decimal,
) -> dict | None:
    """Path statistics from real candles between activation and close.
    None when history is missing inside the window - never a partial
    MFE presented as the whole."""
    window = [c for c in after(candles, start) if c.open_time < end]
    if not window or entry <= _ZERO or missing_windows(window, start, end):
        return None

    def pct(price: Decimal) -> Decimal:
        return (price - entry) / entry * _HUNDRED

    def minutes(moment: datetime) -> float:
        return (moment - start).total_seconds() / 60

    best = max(window, key=lambda c: c.high)
    worst = min(window, key=lambda c: c.low)
    first_favourable = next((c for c in window if pct(c.high) >= Decimal("0.5")), None)
    to_target = next((c for c in window if c.high >= target), None)
    to_stop = next((c for c in window if c.low <= stop_loss), None)
    entry_success = None
    for candle in window:
        up = pct(candle.high) >= 1
        down = pct(candle.low) <= -1
        if up and down:
            break  # both inside one minute: order unknowable, stays None
        if up or down:
            entry_success = up
            break
    return {
        "source": "EXCHANGE_KLINES",
        "candles": len(window),
        "mfe_pct": pct(best.high),
        "mae_pct": pct(worst.low),
        "minutes_to_mfe": minutes(best.open_time),
        "minutes_to_mae": minutes(worst.open_time),
        "minutes_to_first_favorable": minutes(first_favourable.open_time)
        if first_favourable else None,
        "minutes_to_target": minutes(to_target.open_time) if to_target else None,
        "minutes_to_sl": minutes(to_stop.open_time) if to_stop else None,
        "entry_success": entry_success,
    }


@dataclass
class TradeTimeline:
    """Every timestamp and price level of one trade, kept apart."""

    signal_at: datetime | None  # candidate evidence evaluated
    discovery_started_at: datetime | None  # deciding discovery run start
    ai_decision_at: datetime | None  # Gate CONFIRMED
    godfather_decision_at: datetime | None  # advisory entry verdict (as-of)
    paper_opened_at: datetime  # positions.opened_at (= decision time)
    created_at: datetime | None  # the position row came into existence
    created_at_source: str  # RECORDED | UPPER_BOUND_<source> | UNAVAILABLE
    claim_at: datetime | None  # LIVE execution claimed
    fill_at: datetime | None  # exchange fill time - not recorded by the system
    planned_entry: Decimal  # paper entry, priced at decision time
    actual_entry: Decimal | None
    actual_entry_source: str  # EXCHANGE_FILL | KLINE_AT_CREATION | FIRST_OBSERVATION | UNAVAILABLE

    @property
    def activation_at(self) -> datetime | None:
        """When the ACTUAL position began: the LIVE claim when there is one
        (the exchange position), else the creation of the paper row."""
        return self.claim_at or self.created_at

    @property
    def decision_to_activation_minutes(self) -> float | None:
        if self.activation_at is None:
            return None
        return (self.activation_at - self.paper_opened_at).total_seconds() / 60

    @property
    def drift_during_analysis_pct(self) -> Decimal | None:
        """Price move between the planned entry and the actual entry - did
        the situation change while the AI and the claim were running?"""
        if self.actual_entry is None or self.planned_entry <= _ZERO:
            return None
        return (self.actual_entry - self.planned_entry) / self.planned_entry * _HUNDRED

    def as_dict(self) -> dict:
        def iso(moment: datetime | None) -> str | None:
            return None if moment is None else moment.isoformat()

        return {
            "signal_at": iso(self.signal_at),
            "discovery_started_at": iso(self.discovery_started_at),
            "ai_decision_at": iso(self.ai_decision_at),
            "godfather_decision_at": iso(self.godfather_decision_at),
            "paper_opened_at": iso(self.paper_opened_at),
            "created_at": iso(self.created_at),
            "created_at_source": self.created_at_source,
            "claim_at": iso(self.claim_at),
            "fill_at": iso(self.fill_at),
            "activation_at": iso(self.activation_at),
            "planned_entry": str(self.planned_entry),
            "actual_entry": None if self.actual_entry is None else str(self.actual_entry),
            "actual_entry_source": self.actual_entry_source,
            "decision_to_activation_minutes": self.decision_to_activation_minutes,
            "drift_during_analysis_pct": (
                None if self.drift_during_analysis_pct is None
                else str(self.drift_during_analysis_pct)
            ),
        }
