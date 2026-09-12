from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.backtest.replay_engine import replay_position
from crypto_trading.config.loader import get_settings
from crypto_trading.paper_trading.profit_protection_experiment import _shadow_id
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class _StubConnector:
    """A tiny, fully deterministic in-memory stand-in for BingX - avoids
    HTTP mocking noise in this task's tests (Task 3 already covers the
    real fetch/pagination/cache mechanics against respx directly)."""

    def __init__(self, klines: list[dict], funding: list[dict] | None = None):
        self._klines = klines
        self._funding = funding or []

    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        return [
            k for k in self._klines
            if (start_time_ms is None or k["time"] >= start_time_ms)
            and (end_time_ms is None or k["time"] <= end_time_ms)
        ]

    def get_funding_rate(self, symbol, limit=1, start_time_ms=None, end_time_ms=None):
        return self._funding


def _kline(close: str, time_ms: int, high=None, low=None) -> dict:
    return {"open": close, "high": high or close, "low": low or close, "close": close,
            "volume": "1", "time": time_ms}


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _target(**overrides) -> BacktestTarget:
    defaults = dict(
        position_id="pos-1", instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("60000"), opened_at=_NOW, original_size=Decimal("0"),
        original_status="CLOSED", original_exit_reason="stop_loss",
        original_closed_at=_NOW + timedelta(hours=1),
        original_theoretical_exit=Decimal("49000"), original_simulated_fill_exit=Decimal("48975"),
    )
    defaults.update(overrides)
    return BacktestTarget(**defaults)


def test_replay_position_never_writes_to_the_source_repo(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    connector = _StubConnector([_kline("50000", _ms(_NOW + timedelta(minutes=1)))])
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    assert source.find_open_positions() == []
    assert source.get_position("pos-1") is None


def test_replay_position_seeds_both_frozen_thresholds(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    connector = _StubConnector([_kline("50000", _ms(_NOW + timedelta(minutes=1)))])
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    assert backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010"))) is not None
    assert backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.015"))) is not None


def test_replay_position_stop_loss_wins_on_same_candle_as_target(tmp_path):
    """SL always checked first (check_exit_trigger's fixed order, reused
    unmodified) - a candle whose low breaches stop AND whose high reaches
    target in the same bar must close stop_loss, never target."""
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    candle_time = _NOW + timedelta(minutes=1)
    connector = _StubConnector([_kline("55000", _ms(candle_time), high="61000", low="48000")])
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    position = backtest.get_position("pos-1")
    assert position.status == "CLOSED"
    assert position.exit_reason == "stop_loss"


def test_replay_position_threshold_touch_only_affects_the_next_candle(tmp_path):
    """Candle 1 touches the +1.0% threshold (50500) but does NOT breach
    the ORIGINAL stop (49000) - the shadow must still be OPEN after candle
    1, with threshold_reached=1. Only candle 2 (which dips to 49950, above
    the ORIGINAL stop but BELOW the new breakeven stop 50000) proves the
    breakeven activation took effect - i.e. the shadow closes on candle 2,
    not candle 1, and at the breakeven price, not the original stop."""
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    c1 = _NOW + timedelta(minutes=1)
    c2 = _NOW + timedelta(minutes=2)
    connector = _StubConnector([
        _kline("50600", _ms(c1), high="50600", low="50100"),
        _kline("49960", _ms(c2), high="50050", low="49950"),
    ])
    settings = get_settings()

    replay_position(_target(target=Decimal("60000")), connector, source, backtest, settings,
                     tmp_path / "cache", "run-1")

    shadow = backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    assert shadow["status"] == "CLOSED"
    assert shadow["exit_reason"] == "stop_loss"
    assert shadow["theoretical_exit"] == "49950"  # the NEW breakeven stop (50000) gap-filled to candle low
    assert shadow["threshold_reached"] == 1


def test_replay_position_is_deterministic_across_two_runs(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    connector = _StubConnector([
        _kline("50600", _ms(_NOW + timedelta(minutes=1)), high="50600", low="50100"),
        _kline("49960", _ms(_NOW + timedelta(minutes=2)), high="50050", low="49950"),
    ])
    settings = get_settings()

    backtest_a = SQLiteRepository(tmp_path / "a.db")
    backtest_b = SQLiteRepository(tmp_path / "b.db")
    replay_position(_target(), connector, source, backtest_a, settings, tmp_path / "cache", "run-1")
    replay_position(_target(), connector, source, backtest_b, settings, tmp_path / "cache", "run-2")

    shadow_a = backtest_a.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    shadow_b = backtest_b.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    for key in ("status", "exit_reason", "theoretical_exit", "mfe", "mae", "threshold_reached"):
        assert shadow_a[key] == shadow_b[key]


def test_replay_position_leaves_shadow_open_when_candles_run_out(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    connector = _StubConnector([_kline("50100", _ms(_NOW + timedelta(minutes=1)))])  # never touches SL/target
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    shadow = backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    assert shadow["status"] == "OPEN"  # right-censored, never silently dropped
