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
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather.path import PathPoint
from crypto_trading.godfather.stop_simulation import MAX_UNOBSERVED_MINUTES
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")

# Catch-up runs BEFORE Fas 2A.1 replayed only the latest 1000 1m candles
# of a gap. From PAGED_CATCHUP_SINCE on, catch-up walks the whole gap
# (paper_trading/monitoring_catchup.py pages from the gap start) and
# reports any minute the exchange lacks as a `kline_history_gap` error.
CATCHUP_MAX_MINUTES = 1000
PAGED_CATCHUP_SINCE = datetime(2026, 9, 26, tzinfo=UTC)

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
    paged = {c for c in catchups if c >= PAGED_CATCHUP_SINCE}
    gaps: list[MonitoringGap] = []
    for (_s1, e1), (s2, _e2) in zip(completed, completed[1:], strict=False):
        if (s2 - e1).total_seconds() / 60 <= MONITORING_GAP_MINUTES:
            continue
        closing = [
            c for c in catchups if e1 <= c <= s2 + timedelta(minutes=MONITORING_GAP_MINUTES)
        ]
        if any(c in paged for c in closing):
            recovered_from = e1  # the whole gap was replayed
        elif closing:
            recovered_from = max(e1, s2 - timedelta(minutes=CATCHUP_MAX_MINUTES))
        else:
            recovered_from = s2
        gaps.append(MonitoringGap(start=e1, end=s2, recovered_from=recovered_from))
    return gaps


@dataclass
class TradeObservation:
    status: str  # COMPLETE | PARTIAL | UNOBSERVABLE
    reasons: list[str] = field(default_factory=list)
    # Where the ACTUAL outcome comes from: the exchange position (LIVE),
    # the verified paper exit, the paper exit corrected from exchange
    # candles, or nowhere.
    outcome_source: str = "NONE"  # EXCHANGE | PAPER | KLINES | NONE
    exit_verification: str = "UNVERIFIED"
    # CANDLES | CANDLES_REPLAYED | KLINES | EXCHANGE | UNVERIFIED
    # Whether the PAPER book's own booked exit can be trusted - what the
    # counterfactual engine compares policies against.
    paper_exit_status: str = "MISSING"  # VERIFIED | MISMATCH | UNVERIFIED | MISSING
    path_status: str = "MISSING"  # OBSERVED | RECONSTRUCTED | PARTIAL | MISSING
    activation_minutes: float | None = None
    activation_source: str | None = None
    max_guardian_gap_minutes: float | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "outcome_source": self.outcome_source,
            "exit_verification": self.exit_verification,
            "paper_exit_status": self.paper_exit_status,
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
        return self.status != "UNOBSERVABLE" and self.path_status in ("OBSERVED", "RECONSTRUCTED")


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
    *,
    kline_verdict: str | None = None,
    candle_path_available: bool = False,
    partial_error_overlap: bool = False,
    activation_at: datetime | None = None,
) -> TradeObservation:
    """`kline_verdict` is `reconstruction.verify_exit` of the paper exit
    against exchange candles (None when no candles are archived);
    `candle_path_available` means real candles cover activation->close
    without a hole; `partial_error_overlap` means a monitoring run failed
    for this instrument while the trade was open (the minute's stop/target
    check was skipped)."""
    obs = TradeObservation(status="COMPLETE")
    reasons = obs.reasons

    if position.size == _ZERO:
        return TradeObservation(status="UNOBSERVABLE", reasons=["ZERO_SIZE"])
    if position.closed_at is None:
        return TradeObservation(status="UNOBSERVABLE", reasons=["NOT_CLOSED"])
    if position.closed_at <= position.opened_at and not (
        live_execution and live_execution.get("exchange_fill_exit")
    ):
        # Opened and closed at the same instant: priced at decision time and
        # already past a level when the row appeared. No trade to measure.
        return TradeObservation(status="UNOBSERVABLE", reasons=["ZERO_DURATION_TRADE"])

    exchange_exit = bool(
        live_execution
        and live_execution.get("exchange_fill_entry")
        and live_execution.get("exchange_fill_exit")
    )

    # --- is the PAPER book's own exit trustworthy? ------------------------
    unrecovered_overlap = any(
        g.unrecovered is not None
        and g.unrecovered[0] < position.closed_at
        and g.unrecovered[1] > position.opened_at
        for g in gaps
    )
    recovered_overlap = any(
        g.start < position.closed_at and g.end > position.opened_at for g in gaps
    )
    if paper_pnl is None:
        obs.paper_exit_status = "MISSING"
    elif kline_verdict == "MATCH":
        obs.paper_exit_status = "VERIFIED"
        if unrecovered_overlap or partial_error_overlap:
            reasons.append("EXIT_VERIFIED_BY_KLINES")
    elif kline_verdict == "MISMATCH":
        obs.paper_exit_status = "MISMATCH"
        reasons.append("PAPER_EXIT_CONTRADICTED_BY_KLINES")
    elif unrecovered_overlap:
        obs.paper_exit_status = "UNVERIFIED"
        reasons.append("MONITORING_GAP_UNRECOVERED")
    elif partial_error_overlap:
        obs.paper_exit_status = "UNVERIFIED"
        reasons.append("MONITORING_PARTIAL_ERROR")
    else:
        obs.paper_exit_status = "VERIFIED"
        if recovered_overlap:
            reasons.append("MONITORING_GAP_RECOVERED_FROM_CANDLES")

    # --- where does the ACTUAL outcome come from? -------------------------
    if exchange_exit:
        # The LIVE position is the actual trade.
        obs.outcome_source, obs.exit_verification = "EXCHANGE", "EXCHANGE"
        if paper_pnl is None:
            reasons.append("PAPER_OUTCOME_MISSING_EXCHANGE_USED")
    elif obs.paper_exit_status == "VERIFIED":
        obs.outcome_source = "PAPER"
        obs.exit_verification = (
            "KLINES" if kline_verdict == "MATCH"
            else "CANDLES_REPLAYED" if recovered_overlap else "CANDLES"
        )
    elif obs.paper_exit_status == "MISMATCH":
        obs.outcome_source, obs.exit_verification = "KLINES", "KLINES"
        reasons.append("EXIT_CORRECTED_FROM_KLINES")
    else:
        obs.outcome_source, obs.exit_verification = "NONE", "UNVERIFIED"
        reasons.append("UNKNOWN_PNL" if paper_pnl is None else "EXCHANGE_HISTORY_GAP")

    # --- path: what was seen, from activation to close? --------------------
    close_minutes = _minutes(position, position.closed_at)
    if activation_at is not None:
        obs.activation_minutes = _minutes(position, activation_at)
        obs.activation_source = "TIMELINE"
    elif live_execution and live_execution.get("claimed_at"):
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

    guardian_complete = False
    if not points:
        reasons.append("MISSING_PRICE_PATH")
    else:
        start = min(obs.activation_minutes or 0.0, points[0].minutes_in_trade)
        watched = [start] + [p.minutes_in_trade for p in points] + [close_minutes]
        gaps_seen = [b - a for a, b in zip(watched, watched[1:], strict=False)]
        obs.max_guardian_gap_minutes = max(gaps_seen) if gaps_seen else 0.0
        guardian_complete = obs.max_guardian_gap_minutes <= MAX_UNOBSERVED_MINUTES
        if not guardian_complete:
            first_gap = gaps_seen[0] if gaps_seen else 0.0
            reasons.append(
                "LATE_FIRST_OBSERVATION" if first_gap > MAX_UNOBSERVED_MINUTES else "MONITORING_GAP"
            )
    if candle_path_available:
        # Exchange candles cover the whole life at 1m resolution - the
        # strongest path source; Guardian holes no longer matter for it.
        obs.path_status = "RECONSTRUCTED"
        if not guardian_complete:
            reasons.append("GUARDIAN_GAP_RECONSTRUCTED_FROM_KLINES")
    elif guardian_complete:
        obs.path_status = "OBSERVED"
    else:
        obs.path_status = "PARTIAL" if points else "MISSING"

    if obs.activation_minutes is not None and obs.activation_minutes > MAX_UNOBSERVED_MINUTES:
        # Not a data hole: the paper entry is priced at decision time and
        # the position exists only after the analysis.
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
        reasons.append("STOP_OVERSHOOT")

    if obs.outcome_source == "NONE":
        obs.status = "UNOBSERVABLE"
    elif (
        obs.path_status not in ("OBSERVED", "RECONSTRUCTED")
        or not has_entry_features
        or not r_available
    ):
        obs.status = "PARTIAL"
    return obs
