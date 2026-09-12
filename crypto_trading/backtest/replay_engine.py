from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.backtest.guardian_replay import copy_guardian_history
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


def replay_position(
    target: BacktestTarget,
    connector: HistoricalMarketDataSource,
    source_repo: Repository,
    backtest_repo: Repository,
    settings: Settings,
    cache_dir: Path,
    run_id: str,
) -> None:
    """Writes ONLY into backtest_repo. source_repo is read from exactly
    once (Guardian history copy) and never written to - see
    guardian_replay.py::copy_guardian_history and this function's own
    test_replay_position_never_writes_to_the_source_repo."""
    now_utc = datetime.now(UTC)
    window_end = min(now_utc, target.opened_at + timedelta(hours=24))

    klines = fetch_historical_klines(
        connector, target.instrument, _KLINE_INTERVAL, target.opened_at, window_end, cache_dir
    )
    funding_rates = fetch_historical_funding(
        connector, target.instrument, target.opened_at, window_end, cache_dir
    )
    copy_guardian_history(source_repo, backtest_repo, target.position_id)

    position = Position(
        position_id=target.position_id, candidate_id=target.position_id,
        instrument=target.instrument, direction=_DIRECTION, status="OPEN_POSITION",
        theoretical_entry=target.entry_price, simulated_fill_entry=target.simulated_fill_entry,
        stop_loss=target.stop_loss, target=target.target, size=BACKTEST_NOTIONAL,
        fill_model_version="backtest-tier1", opened_at=target.opened_at,
    )
    backtest_repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{target.position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=target.position_id,
            occurred_at=target.opened_at, run_id=run_id, schema_version=1, payload={},
        ),
    )
    seed_shadows_for_position(backtest_repo, position, activated_at=_EPOCH, now=target.opened_at)

    evaluable = [k for k in klines if k.observed_at > target.opened_at]  # entry candle itself is never re-evaluated
    for kline in evaluable:  # already ascending (fetch_historical_klines sorts) - never process out of order
        funding_rate = _latest_funding_rate(funding_rates, kline.observed_at)
        price_lookup = {
            target.instrument: (kline.low, kline.high, kline.close, funding_rate)
        }
        close_triggered_positions(
            backtest_repo, price_lookup, kline.observed_at, settings.risk_limits,
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

        baseline_still_open = backtest_repo.get_position(target.position_id).status == "OPEN_POSITION"
        if not baseline_still_open and not any_shadow_open:
            break


def _latest_funding_rate(funding_rates, as_of: datetime) -> Decimal:
    visible = [f for f in funding_rates if f.observed_at <= as_of]
    return max(visible, key=lambda f: f.observed_at).funding_rate if visible else Decimal("0")
