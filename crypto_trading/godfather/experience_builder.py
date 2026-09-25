"""Experience Builder: give Experience Memory the history the system has.

Fas 1 built Experience Memory (`experience.py`); it learned only from a
trade's pre-entry features and its final P/L. This module turns every
closed trade into a full experience sample - still classified on
pre-entry features only - carrying what happened AFTER entry:

* **price path**: MFE, MAE, time to first favourable move / MFE / target
  / SL, which favourable levels were reached, giveback, management
  capture - from Guardian's recorded path (`path.py`), no exchange fetch;
* **entry vs management**: "entry success" = +1% reached before -1%,
  decided before any exit rule acts. A bad entry rescued by a lucky exit
  is therefore not learned as a good entry;
* **prediction errors**: the forecast/thesis error magnitudes already in
  `godfather_prediction_errors`;
* **counterfactuals**: the per-policy deltas already in
  `godfather_counterfactuals` (engine v2, OBSERVED rows only);
* **live**: whether the trade was executed on the exchange.

Everything is deterministic SQL/Python over the database. No LLM call,
no fetch. Samples come from `book.load_book`, so zero-size positions and
unknown P/L are UNAVAILABLE and never enter as neutral trades.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.godfather.book import TradeContext, load_book
from crypto_trading.godfather.experience import (
    ExperienceConfig,
    ExperienceSample,
    build_experience_memory,
)
from crypto_trading.godfather.path import compute_path_metrics
from crypto_trading.godfather.stop_simulation import MAX_UNOBSERVED_MINUTES, excursion
from crypto_trading.schemas.godfather import ExperiencePattern
from crypto_trading.storage.repository import Repository

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")

LEVELS_PCT = (Decimal("0.5"), Decimal("1.0"), Decimal("2.0"), Decimal("3.0"))
ENTRY_SUCCESS_PCT = Decimal("1.0")
_NOT_POLICIES = {"BASELINE", "NO_INTERVENTION", "REJECT_ENTRY"}


def experience_config(settings: Settings) -> ExperienceConfig:
    return ExperienceConfig(
        min_sample_size=settings.godfather.experience_min_sample_size,
        min_support=settings.godfather.experience_min_support,
        fdr_q=settings.godfather.experience_fdr_q,
    )


def trade_profile(
    trade: TradeContext,
    counterfactual_rows: list[dict],
    prediction_errors: list[dict],
) -> dict:
    """Everything that happened after entry, for one trade - measured on
    the ACTUAL position (actual entry, from activation), not on the
    decision-time paper entry."""
    position = trade.position
    timeline = trade.timeline
    entry = (
        timeline.actual_entry
        if timeline is not None and timeline.actual_entry is not None
        else position.simulated_fill_entry
    )
    close = (
        (position.closed_at - position.opened_at).total_seconds() / 60
        if position.closed_at is not None else None
    )
    profile: dict = {
        "has_path": bool(trade.points),
        "exit_reason": (position.exit_reason or "unknown").lower(),
        "counterfactual_delta": {},
    }
    for row in counterfactual_rows:
        detail = row.get("detail_json") or "{}"
        detail = json.loads(detail) if isinstance(detail, str) else detail
        if (
            row["policy"] in _NOT_POLICIES
            or row.get("delta_pnl_usdt") is None
            or detail.get("observation_status", "OBSERVED") != "OBSERVED"
            or not row.get("triggered")
        ):
            continue
        profile["counterfactual_delta"][row["policy"]] = str(row["delta_pnl_usdt"])
    for error in prediction_errors:
        if error.get("magnitude") is None:
            continue
        if error["source"] == "forecast_agent":
            profile["forecast_error"] = float(error["magnitude"])
        elif error["source"] == "trade_thesis":
            profile["thesis_error"] = float(error["magnitude"])

    observation = trade.observation
    if observation is not None:
        profile["observation"] = observation.as_dict()
    if timeline is not None:
        profile["timeline"] = timeline.as_dict()
    if trade.r is not None and trade.r.return_pct is not None:
        profile["return_pct"] = str(trade.r.return_pct * _HUNDRED)
    if trade.candle_path is not None and (observation is None or observation.path_usable):
        # Real exchange candles over the whole life: the strongest source.
        stats = trade.candle_path
        profile["has_path"] = True
        profile["path_source"] = "EXCHANGE_KLINES"
        for key in ("minutes_to_first_favorable", "minutes_to_target", "minutes_to_sl",
                    "entry_success"):
            profile[key] = stats.get(key)
        mfe = stats["mfe_pct"]
        profile["levels_reached"] = [str(level) for level in LEVELS_PCT if mfe >= level]
        final = (
            trade.r.return_pct * _HUNDRED
            if trade.r and trade.r.return_pct is not None else None
        )
        if final is not None and mfe > _ZERO:
            profile["giveback_ratio"] = str((mfe - final) / mfe)
            profile["management_capture"] = str(final / mfe)
        return profile
    if not trade.points or entry == _ZERO:
        profile["has_path"] = False
        return profile
    if observation is not None and not observation.path_usable:
        # A path with holes yields a lower-bound MFE, an upper-bound MAE and
        # time-to-events that may be wrong. None of them is recorded - the
        # trade still counts for its (verified) outcome.
        profile["has_path"] = False
        profile["path_excluded"] = observation.path_status
        return profile

    # Time is measured from ACTIVATION (the position existing), not from
    # the decision timestamp the paper entry carries.
    profile["path_source"] = "GUARDIAN_TICKS"
    start = (
        observation.activation_minutes
        if observation is not None and observation.activation_minutes is not None
        else 0.0
    )
    start = min(start, trade.points[0].minutes_in_trade)
    minutes = [p.minutes_in_trade - start for p in trade.points]
    moves = [excursion(p.price, entry) * _HUNDRED for p in trade.points]
    final = (
        excursion(position.theoretical_exit, entry) * _HUNDRED
        if position.theoretical_exit is not None else moves[-1]
    )
    all_moves = [*moves, final]
    all_minutes = [*minutes, (close - start) if close is not None else minutes[-1]]
    mfe = max(all_moves)

    def _first(predicate) -> float | None:
        return next((m for m, x in zip(all_minutes, all_moves, strict=True) if predicate(x)), None)

    profile["minutes_to_first_favorable"] = _first(lambda x: x >= Decimal("0.5"))
    profile["levels_reached"] = [str(level) for level in LEVELS_PCT if mfe >= level]
    target_pct = excursion(position.target, entry) * _HUNDRED
    initial_sl = (
        trade.r.initial_stop_loss
        if trade.r is not None and trade.r.initial_stop_loss is not None
        else position.stop_loss
    )
    sl_pct = excursion(initial_sl, entry) * _HUNDRED
    profile["minutes_to_target"] = _first(lambda x: x >= target_pct)
    profile["minutes_to_sl"] = _first(lambda x: x <= sl_pct)
    if mfe > _ZERO:
        profile["giveback_ratio"] = str((mfe - final) / mfe)
        profile["management_capture"] = str(final / mfe)

    # Entry success: which came first, +1% or -1%? Unobservable when
    # Guardian was not watching right before the deciding move.
    index = next(
        (i for i, x in enumerate(all_moves)
         if x >= ENTRY_SUCCESS_PCT or x <= -ENTRY_SUCCESS_PCT),
        None,
    )
    if index is None:
        profile["entry_success"] = None
    else:
        gap = all_minutes[index] - all_minutes[index - 1] if index > 0 else all_minutes[0]
        profile["entry_success"] = (
            None if gap > MAX_UNOBSERVED_MINUTES else bool(all_moves[index] >= ENTRY_SUCCESS_PCT)
        )
    return profile


def build_samples(
    book: list[TradeContext],
    counterfactuals_by_position: dict[str, list[dict]],
    errors_by_position: dict[str, list[dict]],
    settings: Settings,
) -> list[ExperienceSample]:
    samples: list[ExperienceSample] = []
    for trade in book:
        if exclusion_reason(trade) is not None:
            continue
        path_ok = trade.observation is None or trade.observation.path_usable
        actual = trade.position
        if trade.timeline is not None and trade.timeline.actual_entry is not None:
            actual = trade.position.model_copy(
                update={"simulated_fill_entry": trade.timeline.actual_entry}
            )
        metrics = compute_path_metrics(
            actual, trade.points if path_ok else [],
            settings.guardian.watch_decay_threshold, settings.guardian.exit_decay_threshold,
        )
        mfe, mae, to_mfe = metrics.mfe_pct, metrics.mae_pct, metrics.minutes_to_mfe
        if path_ok and trade.candle_path is not None:
            mfe = trade.candle_path["mfe_pct"]
            mae = trade.candle_path["mae_pct"]
            to_mfe = trade.candle_path["minutes_to_mfe"]
        samples.append(ExperienceSample(
            position_id=trade.position.position_id,
            closed_at=trade.position.closed_at,
            pnl=trade.pnl,
            r=trade.outcome_r,
            observation=trade.observation.as_dict() if trade.observation else {},
            mfe_pct=mfe,
            mae_pct=mae,
            minutes_to_mfe=to_mfe,
            regime=trade.regime,
            features=trade.features,
            profile=trade_profile(
                trade,
                counterfactuals_by_position.get(trade.position.position_id, []),
                errors_by_position.get(trade.position.position_id, []),
            ),
            live=trade.live,
        ))
    return samples


def exclusion_reason(trade: TradeContext) -> str | None:
    """Why a closed trade is NOT experience - or None when it is. An
    outcome that was not seen, or that cannot be expressed in R, is never
    turned into evidence."""
    if trade.position.size == _ZERO:
        return "ZERO_SIZE"
    if trade.position.closed_at is None:
        return "NOT_CLOSED"
    if trade.observation is not None and not trade.observation.outcome_usable:
        return "UNOBSERVABLE_OUTCOME"
    if trade.outcome_r is None:
        return "R_UNAVAILABLE"
    return None


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row["position_id"]), []).append(row)
    return out


def build_samples_from_repo(
    repo: Repository, settings: Settings, book: list[TradeContext] | None = None
) -> list[ExperienceSample]:
    book = book if book is not None else load_book(repo, risk_limits=settings.risk_limits)
    return build_samples(
        book,
        _group(repo.find_godfather_counterfactuals()),
        _group(repo.find_godfather_prediction_errors()),
        settings,
    )


def run_experience_backfill(
    repo: Repository, settings: Settings, now: datetime, run_id: str, persist: bool = True
) -> tuple[list[TradeContext], list[ExperienceSample], list[ExperiencePattern], dict]:
    """Backfill Experience Memory from the whole history. Restates the
    pattern table (patterns that no longer exist are removed) and removes
    prediction-error rows that belong to zero-size positions."""
    book = load_book(repo, risk_limits=settings.risk_limits)
    zero_size = [t.position.position_id for t in book if t.position.size == _ZERO]
    removed = repo.delete_godfather_prediction_errors_for_positions(zero_size) if persist else 0
    samples = build_samples_from_repo(repo, settings, book)
    patterns = build_experience_memory(samples, now, run_id, experience_config(settings))
    if persist:
        repo.replace_godfather_experience_patterns(patterns)
    return book, samples, patterns, {"zero_size_prediction_errors_removed": removed}
