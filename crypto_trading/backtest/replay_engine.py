from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.backtest.guardian_replay import _row_to_observation
from crypto_trading.backtest.historical_fetch import (
    HistoricalMarketDataSource,
    fetch_historical_funding,
    fetch_historical_klines,
)
from crypto_trading.config.loader import Settings
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _guardian_state_for,
    _shadow_id,
    advance_shadow,
    seed_shadows_for_position,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.market import FundingRate
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_DIRECTION = "LONG"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_KLINE_INTERVAL = "1m"

# Fixed synthetic notional for every backtest position, deliberately
# decoupled from whatever `size` (including 0) the real historical
# position actually got from the live PAPER exposure pool - see this
# plan's Global Constraints. Not read from config: this is a Tier 1
# backtest-only constant, never a tunable.
BACKTEST_NOTIONAL = Decimal("1000")


class _SinglePositionRepo:
    """Delegates every attribute/method to the real repo unmodified,
    except `find_open_positions()`, which is narrowed to just the one
    position this replay call is "about" (or `[]` once it has closed).

    Why this exists (review round 1, Critical Fix 2): `close_triggered_
    positions` (called unmodified below) does its own DB-WIDE `repo.
    find_open_positions()` scan internally, matching candles to positions
    purely by `position.instrument in price_lookup`. That's correct in
    production (one always-current, single-purpose DB) but wrong here:
    Task 7 intentionally replays MANY positions - including ones sharing
    an instrument - into ONE shared `backtest_repo` per train/test split,
    so Task 6's `build_report()` can aggregate across all of them. An
    unscoped call during position B's replay would also evaluate an
    earlier, already right-censored position A (still `OPEN_POSITION`,
    correctly, because A's own window ran out with no exit) against B's
    candles - candles from a completely different, unrelated time window
    - and could incorrectly close A. Scoping `find_open_positions()` to
    just the position currently being replayed makes that impossible,
    without touching `paper_trading/position_closing.py` at all."""

    def __init__(self, repo: Repository, position_id: str) -> None:
        self._repo = repo
        self._position_id = position_id

    def __getattr__(self, name):
        return getattr(self._repo, name)

    def find_open_positions(self) -> list[Position]:
        position = self._repo.get_position(self._position_id)
        if position is not None and position.status == "OPEN_POSITION":
            return [position]
        return []


def replay_position(
    target: BacktestTarget,
    connector: HistoricalMarketDataSource,
    source_repo: Repository,
    backtest_repo: Repository,
    settings: Settings,
    cache_dir: Path,
    run_id: str,
) -> None:
    """Writes ONLY into backtest_repo. source_repo is only ever READ from
    (Guardian history, via a one-shot fetch + local watermark pointer -
    see the loop below) and never written to - see this function's own
    test_replay_position_never_writes_to_the_source_repo."""
    window_end = target.opened_at + timedelta(hours=24)
    # Deliberately NOT clamped to datetime.now(UTC): the exchange already
    # returns no future candles on its own, so a wall-clock clamp buys
    # nothing, but it DOES poison fetch_historical_klines/funding's cache
    # key (Task 3) with a different `end` on every run for any position
    # opened <24h before the run - breaking the run-to-run cache hit and
    # this function's own determinism guarantee (review round 1, Bundled
    # Fix 1).

    now_utc = datetime.now(UTC)
    window_is_complete = now_utc >= window_end
    if window_is_complete:
        klines = fetch_historical_klines(
            connector, target.instrument, _KLINE_INTERVAL, target.opened_at, window_end, cache_dir
        )
        funding_rates = fetch_historical_funding(
            connector, target.instrument, target.opened_at, window_end, cache_dir
        )
    else:
        # Review round 2, Important Fix 2: `window_end` is fixed at
        # `opened_at + 24h` (never clamped to `now_utc` - see the comment
        # above), so for any position opened <24h before this run,
        # `window_end` is still in the future relative to real wall-clock
        # time. The exchange can only return whatever candles exist so
        # far for such a window - a genuinely PARTIAL result. Persisting
        # that partial result under `cache_dir` would be unsafe: the
        # cache key is (symbol, interval, start, end) only, indistinguishable
        # from a later run's key once the window has truly elapsed and
        # complete data exists - and fetch_historical_klines/funding only
        # ever re-fetch when no cache file is present, so a later run
        # would silently keep reading this run's truncated data forever.
        # Fetching into a throwaway temp directory instead of the real
        # `cache_dir` guarantees this run's partial fetch can never be
        # mistaken for a complete one by any future run (which looks only
        # in the real `cache_dir`), and the temp directory is cleaned up
        # automatically when this `with` block exits, so nothing
        # orphaned accumulates. This run's own replay still proceeds
        # against whatever partial data was returned - an honest,
        # inherent limitation of replaying a still-in-progress position,
        # not a bug.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_cache_dir = Path(tmp_dir)
            klines = fetch_historical_klines(
                connector, target.instrument, _KLINE_INTERVAL, target.opened_at, window_end, tmp_cache_dir
            )
            funding_rates = fetch_historical_funding(
                connector, target.instrument, target.opened_at, window_end, tmp_cache_dir
            )

    position = Position(
        position_id=target.position_id, candidate_id=target.position_id,
        instrument=target.instrument, direction=_DIRECTION, status="OPEN_POSITION",
        theoretical_entry=target.entry_price, simulated_fill_entry=target.simulated_fill_entry,
        stop_loss=target.stop_loss, target=target.target, size=BACKTEST_NOTIONAL,
        fill_model_version="backtest-tier1", opened_at=target.opened_at,
    )
    created = backtest_repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{target.position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=target.position_id,
            occurred_at=target.opened_at, run_id=run_id, schema_version=1, payload={},
        ),
    )
    if not created:
        # INSERT OR IGNORE silently no-ops on a pre-existing position_id -
        # if we proceeded anyway, the replay would run against whatever
        # stale row was already there instead of this target's real
        # entry/stop/target, and reader/caller would never know (review
        # round 1, Bundled Fix 2). Every position_id in a Tier 1 run must
        # be unique within its backtest_repo.
        raise ValueError(
            f"replay_position: position_id={target.position_id!r} already exists in backtest_repo "
            "- refusing to replay into a pre-existing row"
        )
    seed_shadows_for_position(backtest_repo, position, activated_at=_EPOCH, now=target.opened_at)

    scoped_repo = _SinglePositionRepo(backtest_repo, target.position_id)

    # One-shot fetch of the position's full source Guardian history, plus
    # a local watermark pointer into it (review round 2, Important Fix
    # 1): the round-1 fix (re-calling copy_guardian_history with a
    # growing `up_to` on every single tick) correctly eliminated the
    # look-ahead bug but was O(candles x observations) - it re-read and
    # re-filtered the ENTIRE source history, and re-attempted an INSERT
    # OR IGNORE for every already-copied row, on every tick (measured:
    # ~21s for one position at realistic 1440-candle/1440-observation
    # scale). find_guardian_observations_for_position already returns
    # rows sorted ascending by observed_at (see its own "ORDER BY
    # observed_at ASC" - Repository's implementation), so a single
    # forward-only pointer that only ever advances (never re-scans a row
    # it already copied) preserves the EXACT same no-look-ahead guarantee
    # - backtest_repo only ever contains observations with `observed_at
    # <= <this tick's candle timestamp>` - in O(total observations) work
    # per position instead of O(candles x observations).
    guardian_history = [
        (datetime.fromisoformat(row["observed_at"]), row)
        for row in source_repo.find_guardian_observations_for_position(target.position_id)
    ]
    guardian_cursor = 0

    evaluable = [k for k in klines if k.observed_at > target.opened_at]  # entry candle itself is never re-evaluated
    for kline in evaluable:  # already ascending (fetch_historical_klines sorts) - never process out of order
        while guardian_cursor < len(guardian_history) and guardian_history[guardian_cursor][0] <= kline.observed_at:
            backtest_repo.save_guardian_observation(_row_to_observation(guardian_history[guardian_cursor][1]))
            guardian_cursor += 1

        funding_rate = _latest_funding_rate(funding_rates, kline.observed_at)
        price_lookup = {
            target.instrument: (kline.low, kline.high, kline.close, funding_rate)
        }
        close_triggered_positions(
            scoped_repo, price_lookup, kline.observed_at, settings.risk_limits,
            run_id, guardian_config=settings.guardian,
        )

        any_shadow_open = False
        for threshold_pct in FROZEN_THRESHOLDS_PCT:
            shadow = backtest_repo.get_profit_protection_shadow(
                _shadow_id(target.position_id, threshold_pct)
            )
            if shadow is None or shadow["status"] != "OPEN":
                continue
            any_shadow_open = True
            guardian_state = (
                _guardian_state_for(backtest_repo, target.position_id, kline.observed_at, settings.guardian)
                if settings.guardian.assisted_exit_enabled
                else None
            )
            advance_shadow(
                shadow, kline.low, kline.high, kline.close, funding_rate, kline.observed_at,
                settings.risk_limits.max_position_hold_hours, guardian_state,
                settings.guardian.assisted_exit_enabled, settings.risk_limits, backtest_repo,
            )

        current_position = backtest_repo.get_position(target.position_id)
        baseline_still_open = current_position is not None and current_position.status == "OPEN_POSITION"
        if not baseline_still_open and not any_shadow_open:
            break


def _latest_funding_rate(funding_rates: list[FundingRate], as_of: datetime) -> Decimal:
    visible = [f for f in funding_rates if f.observed_at <= as_of]
    return max(visible, key=lambda f: f.observed_at).funding_rate if visible else Decimal("0")
