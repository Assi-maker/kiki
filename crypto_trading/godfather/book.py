"""The closed-trade book, loaded once, the same way for every consumer.

The supervisor sweep, the Experience Builder and the pipeline all need
the same view of a closed trade: its real P/L, its real price path, its
candidate and its pre-entry features. One loader means they can never
disagree about which trades exist or what a trade's features were.

Two conventions are enforced here rather than by each caller:

* a zero-size (exposure-blocked) position has P/L None - UNAVAILABLE,
  never a neutral 0 that would teach Experience Memory these setups are
  "flat";
* features come from the candidate's evidence record only when that
  evidence was evaluated at or before entry (`features_valid`). On the
  2026-09-25 audit that held for 148 of 148 positions; the check exists
  so that a future ordering bug cannot leak post-entry data silently.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.godfather.features import btc_regime_bucket, build_candidate_features
from crypto_trading.godfather.observation import (
    STOP_OVERSHOOT_R,
    MonitoringGap,
    TradeObservation,
    classify_observation,
    monitoring_gaps,
)
from crypto_trading.godfather.path import PathPoint, reconstruct_price_path
from crypto_trading.godfather.reconstruction import (
    Candle,
    KlineReplay,
    TradeTimeline,
    candle_path,
    candles_from_rows,
    replay_exit,
    verify_exit,
)
from crypto_trading.godfather.risk_units import RMultiple, compute_r, initial_stop_loss
from crypto_trading.paper_trading.execution import compute_fill_price, compute_pnl_or_none
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def safe_candidate(repo: Repository, candidate_id: str) -> Candidate | None:
    """A corrupt candidate row is context, not control flow - the same
    non-fatal treatment `guardian/self_improvement.py` already gives it."""
    try:
        return repo.get_candidate(candidate_id)
    except CorruptCandidateStateError:
        return None


def regime_for(observations: list[dict]) -> str:
    """BTC regime as Guardian measured it on this position's FIRST tick.

    The first observed tick rather than an average: regime compatibility
    is an entry-time question, and averaging over the trade's life would
    mix in conditions that only existed after the decision was made.
    """
    if not observations:
        return "unknown"
    first = min(observations, key=lambda row: str(row.get("observed_at") or ""))
    factors = first.get("factors")
    if isinstance(factors, str):
        try:
            factors = json.loads(factors)
        except (ValueError, TypeError):
            factors = {}
    if not isinstance(factors, dict):
        return "unknown"
    value = factors.get("market_regime")
    if not isinstance(value, (int, float)):
        return "unknown"
    return btc_regime_bucket(float(value))


@dataclass
class TradeContext:
    position: Position
    candidate: Candidate | None
    opportunity_screen: dict | None
    gate_decision: dict | None
    observations: list[dict]
    points: list[PathPoint]
    pnl: Decimal | None
    regime: str
    features: dict = field(default_factory=dict)
    features_valid: bool = True
    live: bool = False
    live_execution: dict | None = None
    r: RMultiple | None = None
    observation: TradeObservation | None = None
    timeline: TradeTimeline | None = None
    candle_path: dict | None = None
    kline_replay: KlineReplay | None = None
    kline_verdict: str | None = None

    @property
    def scorable(self) -> bool:
        """Usable as the ACTUAL side of a path simulation: real paper P/L,
        exposure, a price path, and an exit that was actually seen."""
        return (
            self.pnl is not None
            and self.position.size != _ZERO
            and bool(self.points)
            and (self.observation is None or self.observation.paper_exit_status == "VERIFIED")
        )

    @property
    def outcome_r(self) -> Decimal | None:
        """Net R when the outcome is usable evidence, else None."""
        if self.observation is not None and not self.observation.outcome_usable:
            return None
        return self.r.r_net if self.r is not None else None


def load_gaps(repo: Repository) -> list[MonitoringGap]:
    return monitoring_gaps(
        repo.find_runs_by_type("monitoring"), repo.find_runs_by_type("monitoring_catchup")
    )


_SYMBOL = re.compile(r"\(([A-Z0-9]+-[A-Z]+)\)")


def load_partial_errors(repo: Repository) -> tuple[dict[str, list], list]:
    """Monitoring runs that failed for some instrument: per-symbol windows,
    plus windows whose error names no symbol (e.g. the ticker endpoint was
    unreachable) - those count against every position open at the time."""
    by_symbol: dict[str, list[tuple[datetime, datetime]]] = {}
    global_windows: list[tuple[datetime, datetime]] = []
    for run in repo.find_runs_with_errors("monitoring"):
        start = datetime.fromisoformat(run["started_at"])
        end = datetime.fromisoformat(run["completed_at"]) if run.get("completed_at") else start
        try:
            messages = json.loads(run.get("errors") or "[]")
        except (ValueError, TypeError):
            messages = []
        for message in messages:
            symbols = _SYMBOL.findall(str(message))
            if symbols:
                for symbol in symbols:
                    by_symbol.setdefault(symbol, []).append((start, end))
            else:
                global_windows.append((start, end))
    return by_symbol, global_windows


def _overlaps(windows: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(a <= end and b >= start for a, b in windows)


def build_timeline(
    repo: Repository,
    position: Position,
    candidate: Candidate | None,
    gate_decision: dict | None,
    live_execution: dict | None,
    points: list[PathPoint],
    candles: list[Candle],
    risk_limits: RiskLimitsConfig | None,
) -> TradeTimeline:
    opened_run = repo.get_position_opened_run(position.position_id)
    run_end = (
        datetime.fromisoformat(opened_run["completed_at"])
        if opened_run and opened_run.get("completed_at") else None
    )
    claim_at = (
        datetime.fromisoformat(live_execution["claimed_at"])
        if live_execution and live_execution.get("claimed_at") else None
    )
    recorded = repo.get_position_created_at(position.position_id)
    if recorded is not None:
        created_at, created_source = recorded, "RECORDED"
    else:
        # Not recorded before Fas 2A.1. Every later event is an UPPER bound
        # on when the row existed - the deciding run's end (the run goes on
        # analysing other candidates after opening this one), Guardian's
        # first observation, the LIVE claim - so the earliest is the
        # tightest. Never a guess earlier than the evidence.
        bounds = [
            (moment, source) for moment, source in (
                (run_end, "DISCOVERY_RUN_END"),
                (points[0].observed_at if points else None, "FIRST_OBSERVATION"),
                (claim_at, "LIVE_CLAIM"),
            ) if moment is not None
        ]
        if bounds:
            created_at, source = min(bounds, key=lambda b: b[0])
            created_source = f"UPPER_BOUND_{source}"
        else:
            created_at, created_source = None, "UNAVAILABLE"
    quality = repo.get_godfather_entry_quality(position.candidate_id)

    actual_entry, entry_source = None, "UNAVAILABLE"
    fill = _decimal(live_execution.get("exchange_fill_entry")) if live_execution else None
    if fill is not None and fill > _ZERO:
        actual_entry, entry_source = fill, "EXCHANGE_FILL"
    elif created_at is not None:
        first = next((c for c in candles if c.open_time >= created_at), None)
        if first is not None and first.open_time - created_at <= timedelta(minutes=2):
            actual_entry = (
                compute_fill_price(first.open, position.direction, risk_limits.spread_pct,
                                   risk_limits.slippage_pct, "entry")
                if risk_limits is not None else first.open
            )
            entry_source = "KLINE_AT_CREATION"
        elif points:
            actual_entry, entry_source = points[0].price, "FIRST_OBSERVATION"
    return TradeTimeline(
        signal_at=candidate.evidence_record.evaluated_at if candidate else None,
        discovery_started_at=(
            datetime.fromisoformat(opened_run["started_at"]) if opened_run else None
        ),
        ai_decision_at=(
            datetime.fromisoformat(gate_decision["evaluated_at"])
            if gate_decision and gate_decision.get("evaluated_at") else None
        ),
        godfather_decision_at=(
            datetime.fromisoformat(quality["assessed_at"]) if quality else None
        ),
        paper_opened_at=position.opened_at,
        created_at=created_at,
        created_at_source=created_source,
        claim_at=claim_at,
        fill_at=None,  # the exchange fill time is not recorded by the system
        planned_entry=position.simulated_fill_entry,
        actual_entry=actual_entry,
        actual_entry_source=entry_source,
    )


def load_book(
    repo: Repository,
    fee_pct: Decimal | None = None,
    risk_limits: RiskLimitsConfig | None = None,
) -> list[TradeContext]:
    """The closed-trade book with, per trade: the timeline, the exit
    checked against exchange candles, the candle path, the observation
    classification and R on the ACTUAL position.

    `fee_pct` nets results computed from prices; `risk_limits` (spread,
    slippage, time limit) is needed to price a kline-reconstructed entry or
    exit like the paper engine would and to replay the hard time limit.
    """
    if fee_pct is None and risk_limits is not None:
        fee_pct = risk_limits.fee_pct
    gaps = load_gaps(repo)
    error_windows, global_error_windows = load_partial_errors(repo)
    book: list[TradeContext] = []
    for position in repo.find_closed_positions():
        observations = repo.find_guardian_observations_for_position(position.position_id)
        candidate = safe_candidate(repo, position.candidate_id)
        screen = repo.get_assessment_payload(position.candidate_id, "opportunity_screen")
        gate_decision = repo.get_gate_decision(position.candidate_id)
        regime = regime_for(observations)
        valid = (
            candidate is None
            or candidate.evidence_record.evaluated_at <= position.opened_at
        )
        live_execution = repo.get_live_execution(position.position_id)
        paper_pnl = None if position.size == _ZERO else compute_pnl_or_none(position)
        points = reconstruct_price_path(position, observations)
        features = (
            build_candidate_features(candidate, screen, position.opened_at, regime)
            if valid else {}
        )
        sl, sl_problem = initial_stop_loss(
            position, repo.find_guardian_authority_decisions_for_position(position.position_id)
        )
        live_closed = (
            datetime.fromisoformat(live_execution["closed_at"])
            if live_execution and live_execution.get("closed_at") else None
        )
        last_close = max(filter(None, [position.closed_at, live_closed]), default=None)
        candles = (
            candles_from_rows(repo.find_exchange_klines(
                position.instrument, position.opened_at - timedelta(minutes=1),
                last_close + timedelta(minutes=3),
            ))
            if last_close is not None else []
        )
        timeline = build_timeline(
            repo, position, candidate, gate_decision, live_execution, points, candles,
            risk_limits,
        )

        # --- the paper exit, replayed on exchange candles -----------------
        replay = None
        verdict = None
        if candles and position.closed_at is not None and timeline.created_at and sl:
            max_hold = risk_limits.max_position_hold_hours if risk_limits else 24
            replay = replay_exit(
                candles, timeline.created_at, position.closed_at + timedelta(minutes=3),
                sl, position.target, position.opened_at + timedelta(hours=max_hold),
            )
            verdict = verify_exit(replay, position.exit_reason, position.closed_at)

        partial_error = position.closed_at is not None and (
            _overlaps(error_windows.get(position.instrument, []),
                      position.opened_at, position.closed_at)
            or _overlaps(global_error_windows, position.opened_at, position.closed_at)
        )

        # --- the path of the ACTUAL position, from real candles -------------
        activation = timeline.activation_at
        path_end = live_closed if (live_execution and live_closed) else position.closed_at
        candle_stats = None
        if (
            candles and activation is not None and path_end is not None
            and timeline.actual_entry is not None and sl
        ):
            candle_stats = candle_path(
                candles, activation, path_end, timeline.actual_entry, sl, position.target
            )

        observation = classify_observation(
            position, points, paper_pnl, live_execution, gaps, None, bool(features),
            sl_problem is None, None,
            kline_verdict=verdict, candle_path_available=candle_stats is not None,
            partial_error_overlap=partial_error, activation_at=activation,
        )

        # --- R on the ACTUAL position, against the PLANNED risk ------------
        r = RMultiple("UNAVAILABLE", sl_problem or "NO_OUTCOME")
        if sl_problem is None and observation.outcome_source != "NONE":
            if observation.outcome_source == "EXCHANGE":
                entry = _decimal(live_execution.get("exchange_fill_entry"))
                exit_ = _decimal(live_execution.get("exchange_fill_exit"))
            elif observation.outcome_source == "KLINES":
                entry = timeline.actual_entry
                exit_ = (
                    compute_fill_price(replay.exit_price, position.direction,
                                       risk_limits.spread_pct, risk_limits.slippage_pct, "exit")
                    if risk_limits is not None and replay and replay.exit_price
                    else (replay.exit_price if replay else None)
                )
            else:
                entry = timeline.actual_entry
                exit_ = position.simulated_fill_exit
            if entry is None:
                observation.reasons.append("ACTUAL_ENTRY_UNAVAILABLE")
            else:
                r = compute_r(position, sl, None, exit_price=exit_, entry_price=entry,
                              fee_pct=fee_pct)
        if r.status != "AVAILABLE" and observation.status != "UNOBSERVABLE":
            observation.status = "PARTIAL"
        if (
            r.r_net is not None and r.r_net < -STOP_OVERSHOOT_R
            and "STOP_OVERSHOOT" not in observation.reasons
        ):
            observation.reasons.append("STOP_OVERSHOOT")
        drift = timeline.drift_during_analysis_pct
        if drift is not None and abs(drift) > Decimal("1"):
            observation.reasons.append("ACTUAL_ENTRY_DRIFT_GT_1PCT")

        book.append(TradeContext(
            position=position,
            candidate=candidate,
            opportunity_screen=screen,
            gate_decision=gate_decision,
            observations=observations,
            points=points,
            pnl=paper_pnl if observation.paper_exit_status == "VERIFIED" else None,
            regime=regime,
            features=features,
            features_valid=valid,
            live=live_execution is not None,
            live_execution=live_execution,
            r=r,
            observation=observation,
            timeline=timeline,
            candle_path=candle_stats,
            kline_replay=replay,
            kline_verdict=verdict,
        ))
    book.sort(key=lambda t: t.position.opened_at)
    return book
