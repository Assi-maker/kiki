"""Observation integrity: how much of a trade did the system actually see?

A closed trade carries two kinds of evidence, and a monitoring outage
damages them differently:

* **The outcome** (exit price, exit reason). The paper engine decides
  exits on exchange 1m candles, not on Guardian ticks. After a restart,
  `paper_trading/monitoring_catchup.py` replays the missed candles - but
  only the most recent `CATCHUP_MAX_MINUTES` of them. An outage longer
  than that leaves an UNRECOVERED window in which a stop, target or time
  limit may have triggered unseen, and the booked exit may be at the
  wrong price or even the wrong reason. Such an outcome is verified from
  exchange history (the LIVE fill) when that exists, and is UNVERIFIED
  otherwise.
* **The price path** (MFE, MAE, time-to-event, entry success). This comes
  from Guardian's ticks. A hole in them does not change the outcome, but
  every path statistic computed across it is a guess - MFE is only a
  lower bound, MAE only an upper bound.

**Activation vs decision time.** `positions.opened_at` and the paper entry
price are stamped at the START of the discovery run that decided the
trade; the position only exists once that run's AI analysis finishes,
typically 10-27 minutes later (measured on 2026-09-25: from the LIVE claim,
Guardian's first tick follows after a median 0.3 minutes). The path is
therefore measured from ACTIVATION - the LIVE claim, else the end of the
deciding discovery run - never from `opened_at`, so the analysis window is
not mistaken for a Guardian gap.

Every trade gets COMPLETE / PARTIAL / UNOBSERVABLE plus the reasons, and
downstream code uses a trade only for the evidence it can support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather.path import PathPoint
from crypto_trading.godfather.stop_simulation import MAX_UNOBSERVED_MINUTES
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")

# Mirrors paper_trading/monitoring_catchup.py::_MAX_CATCHUP_KLINES (1m
# candles). Duplicated rather than imported so the analysis layer never
# imports a module that imports a connector; a test pins the equality.
CATCHUP_MAX_MINUTES = 1000

# Two completed monitoring runs further apart than this mean monitoring
# was not running. Normal cadence is ~45 s.
MONITORING_GAP_MINUTES = 5.0

# A stop exit that lost more than this many R gapped through its stop.
STOP_OVERSHOOT_R = Decimal("1.5")


@dataclass(frozen=True)
class MonitoringGap:
    start: datetime
    end: datetime
    # Catch-up replays the most recent candles before the restart, so the
    # UNRECOVERED part of a gap is its beginning: [start, recovered_from).
    recovered_from: datetime

    @property
    def unrecovered(self) -> tuple[datetime, datetime] | None:
        return (self.start, self.recovered_from) if self.recovered_from > self.start else None


def monitoring_gaps(monitoring_runs: list[dict], catchup_runs: list[dict]) -> list[MonitoringGap]:
    """Gaps between consecutive completed monitoring runs, and how much of
    each gap a catch-up run at its end replayed from exchange candles."""
    completed = sorted(
        (datetime.fromisoformat(r["started_at"]), datetime.fromisoformat(r["completed_at"]))
        for r in monitoring_runs
        if r.get("completed_at")
    )
    catchups = sorted(datetime.fromisoformat(r["started_at"]) for r in catchup_runs)
    gaps: list[MonitoringGap] = []
    for (_s1, e1), (s2, _e2) in zip(completed, completed[1:], strict=False):
        if (s2 - e1).total_seconds() / 60 <= MONITORING_GAP_MINUTES:
            continue
        caught_up = any(e1 <= c <= s2 + timedelta(minutes=MONITORING_GAP_MINUTES) for c in catchups)
        recovered_from = (
            max(e1, s2 - timedelta(minutes=CATCHUP_MAX_MINUTES)) if caught_up else s2
        )
        gaps.append(MonitoringGap(start=e1, end=s2, recovered_from=recovered_from))
    return gaps


@dataclass
class TradeObservation:
    status: str  # COMPLETE | PARTIAL | UNOBSERVABLE
    reasons: list[str] = field(default_factory=list)
    outcome_source: str = "NONE"  # PAPER | EXCHANGE | NONE
    exit_verification: str = "UNVERIFIED"  # CANDLES | CANDLES_REPLAYED | EXCHANGE | UNVERIFIED
    path_status: str = "MISSING"  # OBSERVED | PARTIAL | MISSING
    activation_minutes: float | None = None
    activation_source: str | None = None  # LIVE_CLAIM | DISCOVERY_RUN_END | FIRST_OBSERVATION
    max_guardian_gap_minutes: float | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "outcome_source": self.outcome_source,
            "exit_verification": self.exit_verification,
            "path_status": self.path_status,
            "activation_minutes": self.activation_minutes,
            "activation_source": self.activation_source,
            "max_guardian_gap_minutes": self.max_guardian_gap_minutes,
        }

    @property
    def outcome_usable(self) -> bool:
        return self.status != "UNOBSERVABLE"

    @property
    def path_usable(self) -> bool:
        return self.status != "UNOBSERVABLE" and self.path_status == "OBSERVED"


def _minutes(position: Position, moment: datetime) -> float:
    return (moment - position.opened_at).total_seconds() / 60


def classify_observation(
    position: Position,
    points: list[PathPoint],
    paper_pnl: Decimal | None,
    live_execution: dict | None,
    gaps: list[MonitoringGap],
    discovery_run_end: datetime | None,
    has_entry_features: bool,
    r_available: bool,
    r_net: Decimal | None = None,
) -> TradeObservation:
    obs = TradeObservation(status="COMPLETE")
    reasons = obs.reasons

    if position.size == _ZERO:
        return TradeObservation(status="UNOBSERVABLE", reasons=["ZERO_SIZE"])
    if position.closed_at is None:
        return TradeObservation(status="UNOBSERVABLE", reasons=["NOT_CLOSED"])

    exchange_exit = bool(
        live_execution
        and live_execution.get("exchange_fill_entry")
        and live_execution.get("exchange_fill_exit")
    )

    # --- outcome: where does it come from, and was the exit seen? -------
    unrecovered_overlap = [
        g for g in gaps
        if g.unrecovered is not None
        and g.unrecovered[0] < position.closed_at
        and g.unrecovered[1] > position.opened_at
    ]
    recovered_overlap = [
        g for g in gaps
        if g.start < position.closed_at and g.end > position.opened_at
    ]
    if paper_pnl is None:
        if exchange_exit:
            obs.outcome_source, obs.exit_verification = "EXCHANGE", "EXCHANGE"
            reasons.append("PAPER_OUTCOME_MISSING_EXCHANGE_USED")
        else:
            obs.outcome_source = "NONE"
            reasons.append("UNKNOWN_PNL")
    elif unrecovered_overlap:
        reasons.append("MONITORING_GAP_UNRECOVERED")
        if exchange_exit:
            obs.outcome_source, obs.exit_verification = "EXCHANGE", "EXCHANGE"
            reasons.append("EXIT_VERIFIED_BY_EXCHANGE")
        else:
            obs.outcome_source = "PAPER"
            obs.exit_verification = "UNVERIFIED"
            reasons.append("EXCHANGE_HISTORY_GAP")
    else:
        obs.outcome_source = "PAPER"
        obs.exit_verification = "CANDLES_REPLAYED" if recovered_overlap else "CANDLES"
        if recovered_overlap:
            reasons.append("MONITORING_GAP_RECOVERED_FROM_CANDLES")

    # --- path: what did Guardian see, from activation to close? ----------
    close_minutes = _minutes(position, position.closed_at)
    if live_execution and live_execution.get("claimed_at"):
        obs.activation_minutes = _minutes(
            position, datetime.fromisoformat(live_execution["claimed_at"])
        )
        obs.activation_source = "LIVE_CLAIM"
    elif discovery_run_end is not None:
        obs.activation_minutes = _minutes(position, discovery_run_end)
        obs.activation_source = "DISCOVERY_RUN_END"
    elif points:
        obs.activation_minutes = points[0].minutes_in_trade
        obs.activation_source = "FIRST_OBSERVATION"
        reasons.append("ACTIVATION_ESTIMATED")

    if not points:
        obs.path_status = "MISSING"
        reasons.append("MISSING_PRICE_PATH")
    else:
        start = min(obs.activation_minutes or 0.0, points[0].minutes_in_trade)
        watched = [start] + [p.minutes_in_trade for p in points] + [close_minutes]
        gaps_seen = [b - a for a, b in zip(watched, watched[1:], strict=False)]
        obs.max_guardian_gap_minutes = max(gaps_seen) if gaps_seen else 0.0
        if obs.max_guardian_gap_minutes > MAX_UNOBSERVED_MINUTES:
            obs.path_status = "PARTIAL"
            first_gap = gaps_seen[0] if gaps_seen else 0.0
            reasons.append(
                "LATE_FIRST_OBSERVATION" if first_gap > MAX_UNOBSERVED_MINUTES else "MONITORING_GAP"
            )
        else:
            obs.path_status = "OBSERVED"

    if obs.activation_minutes is not None and obs.activation_minutes > MAX_UNOBSERVED_MINUTES:
        # Not a data hole: the paper entry is priced at decision time and
        # the position exists only after the analysis. Recorded so the
        # stale-entry effect can be measured.
        reasons.append("DECISION_TO_ACTIVATION_DELAY")

    if not has_entry_features:
        reasons.append("MISSING_ENTRY_FEATURES")
    if not r_available:
        reasons.append("MISSING_INITIAL_SL")
    if (
        r_net is not None
        and (position.exit_reason or "").lower() == "stop_loss"
        and r_net < -STOP_OVERSHOOT_R
    ):
        # Real, not a data error - the stop was gapped through. Flagged
        # because one such trade can dominate an average.
        reasons.append("STOP_OVERSHOOT")

    if obs.outcome_source == "NONE" or obs.exit_verification == "UNVERIFIED":
        obs.status = "UNOBSERVABLE"
    elif (
        obs.path_status != "OBSERVED"
        or obs.exit_verification != "CANDLES"
        or not has_entry_features
        or not r_available
    ):
        obs.status = "PARTIAL"
    return obs
