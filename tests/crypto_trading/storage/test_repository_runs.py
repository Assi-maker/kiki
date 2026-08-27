import json
from datetime import UTC, datetime

from crypto_trading.storage.repository import SQLiteRepository


def test_start_run_persists_a_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "discovery", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["run_type"] == "discovery"
    assert row["status"] == "running"
    assert row["completed_at"] is None


def test_complete_run_updates_status_and_errors(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "discovery", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    repo.complete_run("run-1", datetime(2026, 8, 27, 12, 5, tzinfo=UTC), "ok", [])

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["errors"]) == []


def test_complete_run_persists_error_list_on_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "monitoring", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    repo.complete_run(
        "run-1",
        datetime(2026, 8, 27, 12, 0, 30, tzinfo=UTC),
        "error",
        ["ConnectorUnavailableError: BingX otillgänglig"],
    )

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["status"] == "error"
    assert "ConnectorUnavailableError" in json.loads(row["errors"])[0]
