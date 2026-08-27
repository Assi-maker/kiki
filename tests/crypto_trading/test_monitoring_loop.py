from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.monitoring_loop import run_monitoring_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _ms, _raw_funding, _raw_kline, _raw_ticker
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings


def _settings():
    return _market_settings(top_n=1)


def _seed_open_position(
    repo,
    instrument: str = "BTCUSDT",
    stop_loss: Decimal = Decimal("49000"),
    target: Decimal = Decimal("60000"),
    position_id: str = "pos-1",
) -> Position:
    opened_at = datetime.now(UTC)
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument=instrument,
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=stop_loss,
        target=target,
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=opened_at,
        run_id="seed",
        schema_version=1,
        payload={},
    )
    repo.create_position_with_event(position, event)
    return position


class _MonitoringStubConnector:
    """Minimal connector-stub för monitoring_loop - anropar bara
    get_ticker/get_klines/get_funding_rate, aldrig get_contracts/
    get_open_interest (de hör bara till discovery/Task 6)."""

    def __init__(
        self, tickers=None, klines=None, funding_rates=None, raise_for=None, malformed_for=None
    ):
        self._tickers = tickers or {}
        self._klines = klines or {}
        self._funding_rates = funding_rates or {}
        self._raise_for = raise_for or {}
        self._malformed_for = malformed_for or set()

    def get_ticker(self, symbol):
        if symbol in self._raise_for:
            raise self._raise_for[symbol]
        raw = self._tickers[symbol]
        if symbol in self._malformed_for:
            raw = dict(raw)
            del raw["lastPrice"]  # genuint saknad nyckel, inte None
        return raw

    def get_klines(self, symbol, interval, limit=1):
        return self._klines[symbol][-limit:]

    def get_funding_rate(self, symbol, limit=1):
        return self._funding_rates.get(symbol, [])[-limit:]


def test_run_monitoring_tick_closes_a_triggered_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    closed = run_monitoring_tick(connector, repo, _settings())

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"


def test_run_monitoring_tick_skips_instrument_on_connector_failure_without_crashing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    connector = _MonitoringStubConnector(raise_for={"BTCUSDT": ConnectorUnavailableError("nere")})

    closed = run_monitoring_tick(connector, repo, _settings())

    assert closed == []
    # kvar öppen, aldrig gissad stängning
    assert repo.find_open_positions()[0].status == "OPEN_POSITION"


def test_run_monitoring_tick_persists_a_runs_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _MonitoringStubConnector()  # inga öppna positioner - inga anrop görs alls

    run_monitoring_tick(connector, repo, _settings())

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row is not None


def test_run_monitoring_tick_does_not_crash_on_unexpected_malformed_payload(tmp_path):
    """Conflict-fix (2026-08-27): en genuint ofullständig rå-ticker (saknar
    lastPrice) ger Ticker.from_raw() ett KeyError - INTE ConnectorUnavailableError,
    så det inre except-blocket fångar det inte. Den nya yttre
    except Exception (samma mönster som discovery_loop.run_discovery_tick())
    ska fånga detta, markera runs.status='error', och ALDRIG låta undantaget
    nå anroparen (vilket annars skulle krascha run_forever())."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
        malformed_for={"BTCUSDT"},
    )

    closed = run_monitoring_tick(connector, repo, _settings())  # ska aldrig kasta

    assert closed == []
    assert repo.find_open_positions()[0].status == "OPEN_POSITION"
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row["status"] == "error"
    assert "KeyError" in row["errors"]
