from datetime import UTC, datetime

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.config.loader import DetectiveConfig
from crypto_trading.detective_loop import run_detective_tick
from crypto_trading.schemas.detective import DetectiveBatchAnalysis
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _settings_with_batch_size(n: int):
    settings = _settings()
    return settings.model_copy(
        update={
            "detective": DetectiveConfig(
                batch_size=n, check_interval_seconds=300, min_history_for_win_loss_comparison=20
            )
        }
    )


def _mock_runner() -> MockAgentRunner:
    fixture = DetectiveBatchAnalysis(
        agent_name="crypto-detective",
        run_id="run-1",
        created_at=_NOW,
        status="ok",
        observations=["obs"],
        winning_patterns=[],
        losing_patterns=[],
    )
    return MockAgentRunner(fixtures={"crypto-detective": fixture})


def test_run_detective_tick_persists_a_runs_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    run_detective_tick(repo, _mock_runner(), _settings_with_batch_size(10))

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'detective'").fetchone()
    assert row is not None
    assert row["status"] == "ok"


def test_run_detective_tick_never_crashes_on_unexpected_exception(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "t.db")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("crypto_trading.detective_loop.run_detective_batch", _boom)

    run_detective_tick(repo, _mock_runner(), _settings_with_batch_size(10))  # ska aldrig kasta

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'detective'").fetchone()
    assert row["status"] == "error"
    assert "RuntimeError" in row["errors"]
