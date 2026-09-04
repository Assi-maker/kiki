from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.guardian_loop import run_guardian_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _raw_funding, _raw_kline, _raw_ticker, _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _StubConnector:
    def get_klines(self, symbol, interval, limit=100):
        return [
            _raw_kline("100", int(_NOW.timestamp() * 1000) - (29 - i) * 60000) for i in range(30)
        ]

    def get_funding_rate(self, symbol, limit=1):
        return [_raw_funding(symbol, "0.0001", int(_NOW.timestamp() * 1000))]

    def get_ticker(self, symbol):
        return _raw_ticker(symbol, "100", "1000000", int(_NOW.timestamp() * 1000))


class _FakeRunner:
    last_call_billed = True
    last_call_cost_usd = Decimal("0.01")

    def run(self, agent_def, context, response_model):
        return response_model(
            agent_name="crypto-guardian", run_id="run-1", created_at=_NOW, status="ok", reasoning="x",
        )


def test_run_guardian_tick_persists_a_runs_row_and_never_crashes(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    # no matching candidate row -> process_one_position() skips fail-safe -
    # this test proves the tick still completes and records a runs row,
    # never crashes, even though every position is skipped.

    run_guardian_tick(repo, _StubConnector(), _FakeRunner(), _settings(), _NOW)

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'guardian'").fetchone()
    assert row is not None
    assert row["status"] == "ok"
