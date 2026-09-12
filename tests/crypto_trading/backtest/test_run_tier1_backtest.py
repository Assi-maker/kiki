from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.backtest.run_tier1_backtest import run_tier1_backtest
from crypto_trading.config.loader import get_settings
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class _StubConnector:
    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        return [{"open": "50100", "high": "50100", "low": "50100", "close": "50100",
                  "volume": "1", "time": int((_NOW + timedelta(minutes=1)).timestamp() * 1000)}]

    def get_funding_rate(self, symbol, limit=1, start_time_ms=None, end_time_ms=None):
        return []


class _StubConnectorWithFailure:
    """Connector that raises for a specific symbol but works for others."""
    def __init__(self, failing_symbol):
        self.failing_symbol = failing_symbol

    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        if symbol == self.failing_symbol:
            raise RuntimeError(f"Instrument {symbol} is currently unavailable")
        return [{"open": "50100", "high": "50100", "low": "50100", "close": "50100",
                  "volume": "1", "time": int((_NOW + timedelta(minutes=1)).timestamp() * 1000)}]

    def get_funding_rate(self, symbol, limit=1, start_time_ms=None, end_time_ms=None):
        if symbol == self.failing_symbol:
            raise RuntimeError(f"Instrument {symbol} is currently unavailable")
        return []


def _seed(source_repo, position_id, opened_at, instrument="BTCUSDT"):
    source_repo.create_position_with_event(
        Position(
            position_id=position_id, candidate_id=position_id, instrument=instrument,
            direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
            simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
            target=Decimal("60000"), size=Decimal("0"), fill_model_version="v1", opened_at=opened_at,
        ),
        Event(event_id=f"e-{position_id}", event_type="POSITION_OPENED", aggregate_type="position",
              aggregate_id=position_id, occurred_at=opened_at, run_id="seed", schema_version=1, payload={}),
    )


def test_run_tier1_backtest_routes_positions_by_split_cutoff(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    _seed(source, "pos-train", _NOW)
    _seed(source, "pos-test", _NOW + timedelta(days=2))
    cutoff = _NOW + timedelta(days=1)
    connector = _StubConnector()
    settings = get_settings()

    report = run_tier1_backtest(source, connector, settings, cutoff, tmp_path / "out")

    train_ids = {row["position_id"] for row in report["per_position_table"] if row["position_id"] == "pos-train"}
    test_ids = {row["position_id"] for row in report["per_position_table"] if row["position_id"] == "pos-test"}
    assert "pos-train" in train_ids
    assert "pos-test" in test_ids
    assert (tmp_path / "out" / "tier1_report.json").exists()


def test_run_tier1_backtest_never_writes_to_the_source_repo(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    _seed(source, "pos-1", _NOW)
    original_position = source.get_position("pos-1")
    connector = _StubConnector()
    settings = get_settings()

    run_tier1_backtest(source, connector, settings, _NOW + timedelta(days=1), tmp_path / "out")

    assert source.get_position("pos-1") == original_position  # byte-identical, untouched
    assert source.find_all_positions(limit=100) == [original_position]  # no extra rows added


def test_run_tier1_backtest_isolates_per_position_connector_failures(tmp_path):
    """Verify that a connector failure for one position does not block others.
    One bad item in a batch must never stop the remaining positions from being replayed."""
    source = SQLiteRepository(tmp_path / "source.db")
    # Seed two positions with different instruments
    _seed(source, "pos-fail", _NOW, instrument="NCFXEUR2USD-USDT")  # will fail
    _seed(source, "pos-success", _NOW, instrument="BTCUSDT")  # will succeed
    cutoff = _NOW + timedelta(days=1)
    # Connector that raises for the failing instrument but works for others
    connector = _StubConnectorWithFailure(failing_symbol="NCFXEUR2USD-USDT")
    settings = get_settings()

    # This should not raise - the failure should be caught and logged
    report = run_tier1_backtest(source, connector, settings, cutoff, tmp_path / "out")

    # Verify the successful position made it into the report
    success_ids = {row["position_id"] for row in report["per_position_table"] if row["position_id"] == "pos-success"}
    assert "pos-success" in success_ids, "Successful position should be in report"

    # Verify the skip count reflects the one failure
    assert report["n_positions_skipped_due_to_fetch_error"] == 1
    assert report["n_positions_total"] == 2

    # Final whole-branch review, Important Fix 4: the count alone is named
    # `..._due_to_fetch_error`, which specifically claims "the exchange
    # was unavailable" - but EVERY exception lands in it, a genuine logic
    # bug included. The skipped_positions list makes the real cause
    # visible per position instead of hiding it behind that name.
    assert report["skipped_positions"] == [{
        "position_id": "pos-fail",
        "instrument": "NCFXEUR2USD-USDT",
        "error_type": "RuntimeError",
        "error": "Instrument NCFXEUR2USD-USDT is currently unavailable",
    }]

