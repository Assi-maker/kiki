from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.monitoring_catchup import run_monitoring_catchup
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _ms, _raw_funding, _raw_kline
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings


def _settings():
    return _market_settings(top_n=1)


def _seed_open_position(
    repo, instrument="BTCUSDT", stop_loss=Decimal("49000"), target=Decimal("60000"),
    position_id="pos-1", opened_at=None,
) -> Position:
    opened_at = opened_at or datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=stop_loss, target=target,
        size=Decimal("1000"), fill_model_version="v1", opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    return position


class _CatchupStubConnector:
    def __init__(self, klines=None, funding_rates=None, raise_for=None):
        self._klines = klines or {}
        self._funding_rates = funding_rates or {}
        self._raise_for = raise_for or {}

    def get_klines(self, symbol, interval, limit=1):
        if symbol in self._raise_for:
            raise self._raise_for[symbol]
        return self._klines.get(symbol, [])[-limit:]

    def get_funding_rate(self, symbol, limit=1):
        return self._funding_rates.get(symbol, [])[-limit:]


def test_returns_empty_when_no_previous_monitoring_run_exists(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _CatchupStubConnector()

    result = run_monitoring_catchup(connector, repo, _settings(), datetime.now(UTC))

    assert result == []


def test_returns_empty_when_last_completed_run_is_not_before_now(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    repo.start_run("run-0", "monitoring", now)
    repo.complete_run("run-0", now, "ok", [])  # completed exactly at 'now' - no gap
    connector = _CatchupStubConnector()

    result = run_monitoring_catchup(connector, repo, _settings(), now)

    assert result == []


def test_replays_a_missed_candle_and_closes_on_stop_loss_using_the_candles_own_timestamp(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    since = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    now = datetime(2026, 9, 11, 12, 5, tzinfo=UTC)
    repo.start_run("run-0", "monitoring", since)
    repo.complete_run("run-0", since, "ok", [])
    missed_candle_time = since + timedelta(minutes=2)
    connector = _CatchupStubConnector(
        klines={
            "BTCUSDT": [_raw_kline("48000", _ms(missed_candle_time), high="48500", low="48000")]
        },
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    closed = run_monitoring_catchup(connector, repo, _settings(), now)

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].closed_at == missed_candle_time  # the candle's own time, not wall-clock 'now'


def test_only_the_earliest_triggering_missed_candle_closes_the_position(tmp_path):
    """Two missed candles: the earlier one breaches stop_loss, the later one
    would have hit target if the position were still open. Chronological
    per-candle replay must close on the FIRST trigger and never re-evaluate
    an already-closed position against the later candle."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(
        repo, instrument="BTCUSDT", stop_loss=Decimal("49000"), target=Decimal("52000")
    )
    since = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    now = datetime(2026, 9, 11, 12, 5, tzinfo=UTC)
    repo.start_run("run-0", "monitoring", since)
    repo.complete_run("run-0", since, "ok", [])
    first_candle_time = since + timedelta(minutes=1)
    second_candle_time = since + timedelta(minutes=2)
    connector = _CatchupStubConnector(
        klines={
            "BTCUSDT": [
                _raw_kline("48500", _ms(first_candle_time), high="49900", low="48500"),
                _raw_kline("53000", _ms(second_candle_time), high="53000", low="52500"),
            ]
        },
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    closed = run_monitoring_catchup(connector, repo, _settings(), now)

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].closed_at == first_candle_time


def test_skips_instrument_on_connector_failure_without_crashing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT")
    since = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    now = datetime(2026, 9, 11, 12, 5, tzinfo=UTC)
    repo.start_run("run-0", "monitoring", since)
    repo.complete_run("run-0", since, "ok", [])
    connector = _CatchupStubConnector(raise_for={"BTCUSDT": ConnectorUnavailableError("nere")})

    closed = run_monitoring_catchup(connector, repo, _settings(), now)  # must never raise

    assert closed == []
    assert repo.find_open_positions()[0].status == "OPEN_POSITION"


def test_persists_a_monitoring_catchup_runs_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    since = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    now = datetime(2026, 9, 11, 12, 5, tzinfo=UTC)
    repo.start_run("run-0", "monitoring", since)
    repo.complete_run("run-0", since, "ok", [])
    connector = _CatchupStubConnector()  # no open positions - loop body never runs

    run_monitoring_catchup(connector, repo, _settings(), now)

    row = repo._conn.execute(
        "SELECT * FROM runs WHERE run_type = 'monitoring_catchup'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "ok"
