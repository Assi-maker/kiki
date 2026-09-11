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


def test_complete_run_persists_instruments_scanned_when_given(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "discovery", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    repo.complete_run(
        "run-1", datetime(2026, 8, 27, 12, 5, tzinfo=UTC), "ok", [], instruments_scanned=1119
    )

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["instruments_scanned"] == 1119


def test_complete_run_leaves_instruments_scanned_null_when_not_given(tmp_path):
    """monitoring/notify-runs (och en discovery-tick som kraschade innan
    snapshoten byggdes) skickar aldrig instruments_scanned - ska förbli
    NULL, aldrig 0 eller en gissning."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "monitoring", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    repo.complete_run("run-1", datetime(2026, 8, 27, 12, 5, tzinfo=UTC), "ok", [])

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["instruments_scanned"] is None


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


def test_complete_run_redacts_secrets_in_error_strings_before_persisting(tmp_path):
    """Fas 6-fynd (code review 2026-08-29): complete_run() gick tidigare
    förbi redact() helt - en secret i ett undantagsmeddelande (t.ex. från
    en icke-httpx.HTTPError-avvikelse i TelegramNotifier.send() som
    slinker förbi dess egen except-sats) skulle persisteras rått i
    runs.errors, och kunde sedan visas i klartext över Telegram via
    format_debug_error_message() på debug-nivå. Detta test använder ett
    FEJKAT, ofarligt "secret"-mönster - aldrig en riktig hemlighet."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "notify", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    repo.complete_run(
        "run-1",
        datetime(2026, 8, 27, 12, 0, 30, tzinfo=UTC),
        "error",
        [
            "request failed: token=FAKE-NOT-A-REAL-SECRET-123&other=1",
            "request to https://api.telegram.org/bot123456789:FAKEBOTTOKENFAKEFAKEFAKE/"
            "sendMessage failed",
        ],
    )

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    stored_errors = json.loads(row["errors"])
    assert "FAKE-NOT-A-REAL-SECRET-123" not in stored_errors[0]
    assert "123456789:FAKEBOTTOKENFAKEFAKEFAKE" not in stored_errors[1]
    assert "***REDACTED***" in stored_errors[0]
    assert "***REDACTED***" in stored_errors[1]


def test_find_latest_completed_run_returns_none_when_no_runs_exist(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.find_latest_completed_run("monitoring") is None


def test_find_latest_completed_run_ignores_a_row_still_running(tmp_path):
    """A row with status='running'/completed_at=NULL means the process
    died mid-tick - never a safe catch-up anchor."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "monitoring", datetime(2026, 9, 11, 12, 0, tzinfo=UTC))

    assert repo.find_latest_completed_run("monitoring") is None


def test_find_latest_completed_run_returns_the_most_recent_completed_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "monitoring", datetime(2026, 9, 11, 12, 0, tzinfo=UTC))
    repo.complete_run("run-1", datetime(2026, 9, 11, 12, 0, 30, tzinfo=UTC), "ok", [])
    repo.start_run("run-2", "monitoring", datetime(2026, 9, 11, 12, 1, 0, tzinfo=UTC))
    repo.complete_run("run-2", datetime(2026, 9, 11, 12, 1, 30, tzinfo=UTC), "error", ["boom"])
    repo.start_run("run-3", "monitoring", datetime(2026, 9, 11, 12, 2, 0, tzinfo=UTC))  # still running

    result = repo.find_latest_completed_run("monitoring")

    assert result["run_id"] == "run-2"  # most recent COMPLETED row, ignoring the still-running one
    assert result["status"] == "error"  # 'error' still counts as completed - it finished, it just failed


def test_find_latest_completed_run_filters_by_run_type(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "discovery", datetime(2026, 9, 11, 12, 0, tzinfo=UTC))
    repo.complete_run("run-1", datetime(2026, 9, 11, 12, 0, 30, tzinfo=UTC), "ok", [])

    assert repo.find_latest_completed_run("monitoring") is None
