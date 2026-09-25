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
    """Everything that happened after entry, for one trade."""
    position = trade.position
    entry = position.simulated_fill_entry
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

    if not trade.points or entry == _ZERO:
        return profile

    minutes = [p.minutes_in_trade for p in trade.points]
    moves = [excursion(p.price, entry) * _HUNDRED for p in trade.points]
    final = (
        excursion(position.theoretical_exit, entry) * _HUNDRED
        if position.theoretical_exit is not None else moves[-1]
    )
    all_moves = [*moves, final]
    all_minutes = [*minutes, close if close is not None else minutes[-1]]
    mfe = max(all_moves)

    def _first(predicate) -> float | None:
        return next((m for m, x in zip(all_minutes, all_moves, strict=True) if predicate(x)), None)

    profile["minutes_to_first_favorable"] = _first(lambda x: x >= Decimal("0.5"))
    profile["levels_reached"] = [str(level) for level in LEVELS_PCT if mfe >= level]
    target_pct = excursion(position.target, entry) * _HUNDRED
    sl_pct = excursion(position.stop_loss, entry) * _HUNDRED
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
        if trade.pnl is None or trade.position.size == _ZERO or trade.position.closed_at is None:
            continue
        metrics = compute_path_metrics(
            trade.position, trade.points,
            settings.guardian.watch_decay_threshold, settings.guardian.exit_decay_threshold,
        )
        samples.append(ExperienceSample(
            position_id=trade.position.position_id,
            closed_at=trade.position.closed_at,
            pnl=trade.pnl,
            mfe_pct=metrics.mfe_pct,
            mae_pct=metrics.mae_pct,
            minutes_to_mfe=metrics.minutes_to_mfe,
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


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row["position_id"]), []).append(row)
    return out


def build_samples_from_repo(
    repo: Repository, settings: Settings, book: list[TradeContext] | None = None
) -> list[ExperienceSample]:
    book = book if book is not None else load_book(repo)
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
    book = load_book(repo)
    zero_size = [t.position.position_id for t in book if t.position.size == _ZERO]
    removed = repo.delete_godfather_prediction_errors_for_positions(zero_size) if persist else 0
    samples = build_samples_from_repo(repo, settings, book)
    patterns = build_experience_memory(samples, now, run_id, experience_config(settings))
    if persist:
        repo.replace_godfather_experience_patterns(patterns)
    return book, samples, patterns, {"zero_size_prediction_errors_removed": removed}
