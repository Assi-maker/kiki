from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.backtest.full_chain_replay import (
    HistoricalDataset,
    HistoricalDataSource,
    build_historical_snapshot,
    historical_replay_budget_exhausted,
    run_full_chain_historical_replay,
    select_historical_universe,
)
from crypto_trading.schemas.assessments import (
    GodfatherPriorityStrategistAssessment,
    GodfatherStrategistAssessment,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.market import FundingRate, Kline
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings

_START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _kline(symbol, interval, minute_offset, price="100", volume="100"):
    return Kline(
        instrument=symbol, interval=interval,
        open=Decimal(price), high=Decimal(price), low=Decimal(price), close=Decimal(price),
        volume=Decimal(volume), observed_at=_START + timedelta(minutes=minute_offset),
    )


def _funding(symbol, minute_offset, rate="0.0001"):
    return FundingRate(
        instrument=symbol, funding_rate=Decimal(rate), mark_price=Decimal("100"),
        observed_at=_START + timedelta(minutes=minute_offset),
    )


def _dataset(symbols, span_minutes=120):
    dataset = HistoricalDataset()
    for symbol in symbols:
        dataset.contracts_raw[symbol] = {
            "symbol": symbol, "status": 1, "pricePrecision": 2, "quantityPrecision": 3,
            "tradeMinUSDT": "5",
        }
        dataset.klines[(symbol, "1h")] = [
            _kline(symbol, "1h", m) for m in range(0, span_minutes, 60)
        ]
        dataset.klines[(symbol, "1m")] = [
            _kline(symbol, "1m", m) for m in range(0, span_minutes)
        ]
        dataset.funding[symbol] = [_funding(symbol, m) for m in range(0, span_minutes, 60)]
    return dataset


class _NullConnector:
    """HistoricalMarketDataSource stub that returns nothing - used only to
    prove select_historical_universe's plumbing without a real network call
    (real-connector integration is exercised in the actual data-prep run,
    not in this unit test)."""

    def get_contracts(self):
        return []

    def get_all_tickers(self):
        return []


# --------------------------------------------------------------------------
# No-look-ahead guarantee - the single most safety-critical property here.
# --------------------------------------------------------------------------
def test_historical_data_source_never_returns_a_kline_after_its_cursor():
    dataset = _dataset(["BTCUSDT"], span_minutes=120)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    cursor = _START + timedelta(minutes=59)
    source.advance_to(cursor)

    raw = source.get_klines("BTCUSDT", "1m", limit=1000)
    observed_ats = [datetime.fromtimestamp(r["time"] / 1000, tz=UTC) for r in raw]
    assert observed_ats, "expected at least one visible candle"
    assert all(ts <= cursor for ts in observed_ats)
    assert len(raw) == 60  # minutes 0..59 inclusive, nothing from minute 60 onward


def test_historical_data_source_never_returns_funding_after_its_cursor():
    dataset = _dataset(["BTCUSDT"], span_minutes=120)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    source.advance_to(_START + timedelta(minutes=61))

    raw = source.get_funding_rate("BTCUSDT", limit=100)
    observed_ats = [datetime.fromtimestamp(r["fundingTime"] / 1000, tz=UTC) for r in raw]
    assert observed_ats
    assert all(ts <= _START + timedelta(minutes=61) for ts in observed_ats)


def test_historical_data_source_ticker_never_reflects_a_future_candle():
    dataset = _dataset(["BTCUSDT"], span_minutes=120)
    # Overwrite the candle at minute 90 with a distinctive future price -
    # a ticker built with a cursor BEFORE minute 90 must never see it.
    future_candle = _kline("BTCUSDT", "1m", 90, price="999999")
    dataset.klines[("BTCUSDT", "1m")] = [
        k if k.observed_at != future_candle.observed_at else future_candle
        for k in dataset.klines[("BTCUSDT", "1m")]
    ]
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    source.advance_to(_START + timedelta(minutes=30))

    ticker_raw = source.get_ticker("BTCUSDT")
    assert Decimal(ticker_raw["lastPrice"]) != Decimal("999999")


def test_historical_data_source_advances_monotonically_reveals_more_data():
    dataset = _dataset(["BTCUSDT"], span_minutes=120)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])

    source.advance_to(_START + timedelta(minutes=10))
    early = len(source.get_klines("BTCUSDT", "1m", limit=1000))
    source.advance_to(_START + timedelta(minutes=100))
    later = len(source.get_klines("BTCUSDT", "1m", limit=1000))

    assert later > early


def test_get_klines_before_any_data_exists_returns_empty():
    dataset = _dataset(["BTCUSDT"], span_minutes=120)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    source.advance_to(_START - timedelta(minutes=1))
    assert source.get_klines("BTCUSDT", "1m", limit=10) == []


# --------------------------------------------------------------------------
# select_historical_universe
# --------------------------------------------------------------------------
def test_select_historical_universe_with_no_eligible_tickers_returns_empty(tmp_path):
    symbols, contracts = select_historical_universe(_NullConnector(), _settings(top_n=5), size=5)
    assert symbols == []
    assert contracts == {}


# --------------------------------------------------------------------------
# build_historical_snapshot
# --------------------------------------------------------------------------
def test_build_historical_snapshot_only_includes_symbols_with_visible_data():
    dataset = _dataset(["BTCUSDT"], span_minutes=200)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    source.advance_to(_START + timedelta(minutes=150))
    settings = _settings(top_n=5)

    snapshot = build_historical_snapshot(source, settings, source.now)

    assert snapshot.simulated_now == source.now
    assert "BTCUSDT" in snapshot.instruments
    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert all(k.observed_at <= source.now for k in snapshot.klines["BTCUSDT"])


def test_build_historical_snapshot_before_any_data_marks_symbol_invalid():
    dataset = _dataset(["BTCUSDT"], span_minutes=200)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    source.advance_to(_START - timedelta(minutes=5))
    settings = _settings(top_n=5)

    snapshot = build_historical_snapshot(source, settings, source.now)

    assert snapshot.tickers == {}
    assert snapshot.data_quality_status.get("BTCUSDT") == "invalid"


# --------------------------------------------------------------------------
# AI budget ceiling
# --------------------------------------------------------------------------
def test_historical_replay_budget_exhausted_trips_once_cumulative_cost_exceeds_headroom(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    started_at = _START
    assert historical_replay_budget_exhausted(repo, started_at, Decimal("10.00")) is False

    repo.record_ai_call_event(
        Event(
            event_id="AI_CALL_MADE:test:1", event_type="AI_CALL_MADE", aggregate_type="test",
            aggregate_id="1", occurred_at=started_at + timedelta(hours=1), run_id="run-1",
            schema_version=1, payload={"role": "test", "status": "ok", "cost_usd": "9.50"},
        )
    )
    assert historical_replay_budget_exhausted(repo, started_at, Decimal("10.00")) is True


def test_historical_replay_budget_exhausted_ignores_cost_before_replay_started(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    started_at = _START + timedelta(days=5)
    repo.record_ai_call_event(
        Event(
            event_id="AI_CALL_MADE:test:1", event_type="AI_CALL_MADE", aggregate_type="test",
            aggregate_id="1", occurred_at=_START, run_id="run-1",
            schema_version=1, payload={"role": "test", "status": "ok", "cost_usd": "999.00"},
        )
    )
    assert historical_replay_budget_exhausted(repo, started_at, Decimal("10.00")) is False


# --------------------------------------------------------------------------
# Full driver wiring (small synthetic dataset, MockAgentRunner, zero network)
# --------------------------------------------------------------------------
_STRATEGIST_NAME = "crypto-godfather-strategist"
_PRIORITY_STRATEGIST_NAME = "crypto-godfather-priority-strategist"


def _mock_runner():
    empty_strategist = GodfatherStrategistAssessment(
        agent_name=_STRATEGIST_NAME, run_id="run-1", created_at=_START, status="ok",
        proposed_heuristics=[],
    )
    empty_priority = GodfatherPriorityStrategistAssessment(
        agent_name=_PRIORITY_STRATEGIST_NAME, run_id="run-1", created_at=_START, status="ok",
        proposed_heuristics=[],
    )
    return MockAgentRunner(fixtures={
        _STRATEGIST_NAME: empty_strategist,
        _PRIORITY_STRATEGIST_NAME: empty_priority,
    })


def test_run_full_chain_historical_replay_completes_and_advances_self_improvement_daily(tmp_path):
    dataset = _dataset(["BTCUSDT"], span_minutes=60 * 30)  # 30 hours of data
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    settings = _settings(top_n=5)
    settings = settings.model_copy(
        update={
            "guardian": settings.guardian.model_copy(update={"authority_enabled": True}),
            "godfather": settings.godfather.model_copy(update={"priority_boost_enabled": True}),
        }
    )
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = _mock_runner()

    start = _START
    end = _START + timedelta(hours=25)  # spans 2 calendar days
    result = run_full_chain_historical_replay(
        repo, runner, settings, source, start, end, "run-1",
        discovery_interval_minutes=60,  # coarser here - correctness, not fidelity, is in scope
    )

    assert result["status"] == "completed"
    assert result["n_discovery_ticks"] > 0
    assert result["n_management_ticks"] > 0
    assert result["n_self_improvement_days"] == 2  # day 1 and day 2 of the 25h span


def test_run_full_chain_historical_replay_never_calls_live_execution_path(tmp_path, monkeypatch):
    """The user's explicit requirement: no part of this replay may touch or
    bypass LIVE risk limits. Structural proof: run_guardian_tick_body is
    always called with live_connector=None in this module - patch
    BingXLiveTradingConnector's constructor to raise if it is EVER
    instantiated anywhere reachable from this replay."""
    import crypto_trading.connectors.bingx_live_trading as live_module

    def _forbidden(*args, **kwargs):
        raise AssertionError("BingXLiveTradingConnector must never be constructed during replay")

    monkeypatch.setattr(live_module.BingXLiveTradingConnector, "__init__", _forbidden)

    dataset = _dataset(["BTCUSDT"], span_minutes=120)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    settings = _settings(top_n=5)
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = _mock_runner()

    result = run_full_chain_historical_replay(
        repo, runner, settings, source, _START, _START + timedelta(hours=1), "run-1",
        discovery_interval_minutes=60,
    )
    assert result["status"] == "completed"


def test_run_full_chain_historical_replay_stops_early_when_ai_budget_exhausted(tmp_path):
    dataset = _dataset(["BTCUSDT"], span_minutes=60 * 72)
    source = HistoricalDataSource(dataset, ["BTCUSDT"])
    settings = _settings(top_n=5)
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = _mock_runner()

    # Seed cost above any reasonable headroom before the replay's own start
    # time - historical_replay_budget_exhausted must trip on the very first
    # check, before a single tick runs.
    repo.record_ai_call_event(
        Event(
            event_id="AI_CALL_MADE:seed:1", event_type="AI_CALL_MADE", aggregate_type="test",
            aggregate_id="1", occurred_at=_START, run_id="run-1",
            schema_version=1, payload={"role": "test", "status": "ok", "cost_usd": "50.00"},
        )
    )

    result = run_full_chain_historical_replay(
        repo, runner, settings, source, _START, _START + timedelta(hours=48), "run-1",
        discovery_interval_minutes=60, total_ai_budget_usd=Decimal("10.00"),
    )

    assert result["status"] == "budget_exhausted_stopped_early"
    assert result["stopped_at"] == _START.isoformat()
