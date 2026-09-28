"""Persistent file logging (2026-09-28): the bot's structured events and any
uncaught exception must survive a restart/crash in a rotated file on disk,
not only in the console window of start_bot.bat."""

import json
import threading

import pytest

from crypto_trading.logging import (
    configure_persistent_logging,
    log_event,
    shutdown_persistent_logging,
)


@pytest.fixture
def log_dir(tmp_path):
    yield tmp_path
    shutdown_persistent_logging()


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_log_event_is_written_to_the_log_file_with_a_timestamp(log_dir):
    log_file = configure_persistent_logging(log_dir)
    log_event("run-1", event="unit_test_event", value=3)
    lines = [line for line in _lines(log_file) if "unit_test_event" in line]
    assert len(lines) == 1
    timestamp, level, payload = lines[0].split(" ", 2)
    assert timestamp.endswith("Z") and "T" in timestamp
    assert level == "INFO"
    assert json.loads(payload) == {
        "run_id": "run-1",
        "event": "unit_test_event",
        "value": 3,
    }


def test_configuring_twice_does_not_duplicate_lines(log_dir):
    configure_persistent_logging(log_dir)
    log_file = configure_persistent_logging(log_dir)
    log_event("run-1", event="written_once")
    assert sum("written_once" in line for line in _lines(log_file)) == 1


def test_uncaught_exception_in_a_thread_is_logged_with_its_traceback(log_dir):
    log_file = configure_persistent_logging(log_dir)

    def _boom():
        raise RuntimeError("thread died here")

    thread = threading.Thread(target=_boom, name="doomed-loop")
    thread.start()
    thread.join()
    records = [
        json.loads(line.split(" ", 2)[2])
        for line in _lines(log_file)
        if "uncaught_exception" in line
    ]
    assert len(records) == 1
    assert records[0]["thread"] == "doomed-loop"
    assert records[0]["error_type"] == "RuntimeError"
    assert "thread died here" in records[0]["traceback"]
    assert "_boom" in records[0]["traceback"]


def test_log_files_are_rotated_and_bounded(log_dir):
    log_file = configure_persistent_logging(log_dir, max_bytes=2_000, backup_count=3)
    for i in range(400):
        log_event("run-1", event="filler", i=i, padding="x" * 50)
    files = sorted(p.name for p in log_dir.glob(log_file.name + "*"))
    assert files == [
        log_file.name,
        f"{log_file.name}.1",
        f"{log_file.name}.2",
        f"{log_file.name}.3",
    ]
    assert all((log_dir / name).stat().st_size <= 2_500 for name in files)


def test_secrets_are_redacted_in_the_file_too(log_dir):
    log_file = configure_persistent_logging(log_dir)
    log_event("run-1", event="secret_check", bot_token="abc123")
    text = log_file.read_text(encoding="utf-8")
    assert "abc123" not in text
    assert "***REDACTED***" in text
