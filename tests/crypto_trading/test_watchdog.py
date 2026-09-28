"""Watchdog / heartbeat (2026-09-28).

Two failures went unnoticed on 2026-09-27: the process died silently at
07:13 UTC (down 4 h 44 min), and at 16:07-20:11 UTC every loop froze inside a
still-running process. The in-process watchdog detects stalled or dead loops
and AI failure; the heartbeat file lets an external, scheduled check detect a
dead or frozen process. Neither ever places, cancels or changes an order."""

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_trading import watchdog
from crypto_trading.ai_health import AIHealth
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_STARTED = _NOW - timedelta(hours=2)


class _Notifier:
    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail

    def send(self, text):
        if self._fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


class _Alive:
    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


def test_a_loop_that_ran_recently_is_not_stale():
    statuses = watchdog.evaluate_loops(
        {"live_execution": _NOW - timedelta(seconds=40)},
        {"live_execution": _Alive()},
        _STARTED,
        _NOW,
    )
    assert [(s.name, s.stale, s.thread_alive) for s in statuses] == [
        ("live_execution", False, True)
    ]


def test_a_loop_past_its_threshold_is_stale():
    statuses = watchdog.evaluate_loops(
        {"live_execution": _NOW - timedelta(minutes=7)},
        {"live_execution": _Alive()},
        _STARTED,
        _NOW,
    )
    assert statuses[0].stale is True
    assert statuses[0].age_seconds == 420


def test_a_loop_that_never_ran_since_start_gets_the_start_time_as_grace():
    statuses = watchdog.evaluate_loops(
        {"discovery": _NOW - timedelta(days=1)},
        {"discovery": _Alive()},
        _NOW - timedelta(minutes=10),
        _NOW,
    )
    assert statuses[0].stale is False  # 10 min since start < discovery threshold
    statuses = watchdog.evaluate_loops(
        {},
        {"live_execution": _Alive()},
        _NOW - timedelta(minutes=10),
        _NOW,
    )
    assert statuses[0].stale is True  # 10 min without a single live tick


def test_a_dead_thread_is_stale_even_if_it_ran_recently():
    statuses = watchdog.evaluate_loops(
        {"guardian": _NOW - timedelta(seconds=10)},
        {"guardian": _Alive(False)},
        _STARTED,
        _NOW,
    )
    assert statuses[0].stale is True
    assert statuses[0].thread_alive is False


def _tick(tmp_path, repo, notifier, health, now, alerts, threads=None):
    return watchdog.run_watchdog_tick(
        repo,
        notifier,
        health,
        threads or {"live_execution": _Alive()},
        _STARTED,
        tmp_path / "heartbeat.json",
        alerts,
        now,
    )


def test_tick_writes_a_heartbeat_file(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("r1", "live_execution", _NOW - timedelta(seconds=20))
    _tick(tmp_path, repo, None, AIHealth(), _NOW, watchdog.AlertState())
    beat = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert beat["pid"] == os.getpid()
    assert beat["written_at"] == _NOW.isoformat()
    assert beat["process_started_at"] == _STARTED.isoformat()
    assert beat["loops"]["live_execution"]["stale"] is False
    assert beat["ai"]["status"] == "OK"


def test_stale_loop_alerts_once_then_recovers(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("r1", "live_execution", _NOW - timedelta(minutes=8))
    notifier, alerts = _Notifier(), watchdog.AlertState()
    _tick(tmp_path, repo, notifier, AIHealth(), _NOW, alerts)
    _tick(tmp_path, repo, notifier, AIHealth(), _NOW + timedelta(minutes=1), alerts)
    assert len(notifier.sent) == 1
    assert "live_execution" in notifier.sent[0]
    repo.start_run("r2", "live_execution", _NOW + timedelta(minutes=2))
    _tick(tmp_path, repo, notifier, AIHealth(), _NOW + timedelta(minutes=2), alerts)
    assert len(notifier.sent) == 2
    assert "live_execution" in notifier.sent[1] and "OK" in notifier.sent[1]


def test_a_still_stale_loop_is_re_alerted_after_the_repeat_interval(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("r1", "live_execution", _NOW - timedelta(minutes=8))
    notifier, alerts = _Notifier(), watchdog.AlertState()
    _tick(tmp_path, repo, notifier, AIHealth(), _NOW, alerts)
    _tick(tmp_path, repo, notifier, AIHealth(), _NOW + timedelta(minutes=61), alerts)
    assert len(notifier.sent) == 2


def test_ai_down_is_alerted_and_its_recovery_too(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("r1", "live_execution", _NOW)
    health, notifier, alerts = AIHealth(), _Notifier(), watchdog.AlertState()
    health.record_failure("CREDIT_EXHAUSTED", "crypto-risk-agent", _NOW)
    _tick(tmp_path, repo, notifier, health, _NOW, alerts)
    assert len(notifier.sent) == 1
    assert "CREDIT_EXHAUSTED" in notifier.sent[0]
    assert "ingen ny" in notifier.sent[0].lower()  # says entries are blocked, fail-closed
    health.record_success(_NOW + timedelta(minutes=5))
    _tick(tmp_path, repo, notifier, health, _NOW + timedelta(minutes=5), alerts)
    assert len(notifier.sent) == 2
    assert "AI" in notifier.sent[1] and "OK" in notifier.sent[1]


def test_a_failing_notifier_never_breaks_the_tick(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    health = AIHealth()
    health.record_failure("AUTH", "x", _NOW)
    _tick(tmp_path, repo, _Notifier(fail=True), health, _NOW, watchdog.AlertState())
    assert (tmp_path / "heartbeat.json").exists()


def test_external_check_alerts_on_a_stale_heartbeat_once_and_on_recovery(tmp_path):
    beat_path, state_path = tmp_path / "heartbeat.json", tmp_path / "watchdog_state.json"
    beat_path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "written_at": (_NOW - timedelta(minutes=30)).isoformat(),
                "loops": {},
                "ai": {"status": "OK"},
            }
        ),
        encoding="utf-8",
    )
    notifier = _Notifier()
    watchdog.check_heartbeat_file(beat_path, state_path, notifier, _NOW)
    watchdog.check_heartbeat_file(beat_path, state_path, notifier, _NOW + timedelta(minutes=5))
    assert len(notifier.sent) == 1
    assert "30 min" in notifier.sent[0]
    beat_path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "written_at": (_NOW + timedelta(minutes=9)).isoformat(),
                "loops": {},
                "ai": {"status": "OK"},
            }
        ),
        encoding="utf-8",
    )
    watchdog.check_heartbeat_file(beat_path, state_path, notifier, _NOW + timedelta(minutes=10))
    assert len(notifier.sent) == 2
    assert "OK" in notifier.sent[1]


def test_external_check_alerts_when_the_heartbeat_file_is_missing(tmp_path):
    notifier = _Notifier()
    watchdog.check_heartbeat_file(tmp_path / "missing.json", tmp_path / "s.json", notifier, _NOW)
    assert len(notifier.sent) == 1


def test_watchdog_has_no_path_to_an_order():
    """Structural: the module never imports a trading connector and never
    names an order-placing or position-closing call."""
    source = Path(watchdog.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "bingx_live_trading",
        "bingx_demo_trading",
        "place_",
        "close_position",
        "cancel_",
        "set_leverage",
        "live_execution import",
    ):
        assert forbidden not in source, forbidden


def test_repository_reports_the_latest_run_per_type(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("a", "monitoring", _NOW - timedelta(minutes=3))
    repo.start_run("b", "monitoring", _NOW - timedelta(minutes=1))
    repo.start_run("c", "notify", _NOW - timedelta(minutes=2))
    assert repo.latest_run_started_at_by_type() == {
        "monitoring": _NOW - timedelta(minutes=1),
        "notify": _NOW - timedelta(minutes=2),
    }


def test_run_forever_thread_is_daemon_safe_signature():
    """The in-process watchdog takes no trading connector at all."""
    import inspect

    params = inspect.signature(watchdog.run_watchdog_forever).parameters
    assert "connector" not in " ".join(params)
    assert threading  # imported for parity with run.py's thread wiring
