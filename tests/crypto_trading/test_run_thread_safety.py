from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime

from crypto_trading import run as run_module
from tests.crypto_trading.test_market_snapshot import _settings


def _settings_with_db(tmp_path):
    return _settings().model_copy(update={"db_path": tmp_path / "t.db"}, deep=True)


def test_run_discovery_forever_constructs_its_own_repository_inside_the_worker_thread(
    tmp_path, monkeypatch
):
    """AC3-live-körningen 2026-08-28 kraschade omedelbart med
    sqlite3.ProgrammingError: 'SQLite objects created in a thread can only
    be used in that same thread' - main() konstruerade SQLiteRepository i
    huvudtråden och skickade den till en threading.Thread som körde
    discovery_loop.run_forever(). Detta test bevisar att
    _run_discovery_forever() istället konstruerar sin egen
    Repository-instans FÖRST, inne i den tråd som anropar den - exakt samma
    redan etablerade mönster som
    tests/crypto_trading/storage/test_repository_concurrency.py använder."""
    settings = _settings_with_db(tmp_path)
    errors: list[BaseException] = []
    tick_thread_ids: list[int] = []

    def fake_run_forever(connector, repo, runner, settings, **kwargs):
        try:
            repo.start_run("run-1", "discovery", datetime.now(UTC))
            tick_thread_ids.append(threading.get_ident())
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(run_module.discovery_loop, "run_forever", fake_run_forever)

    worker = threading.Thread(
        target=run_module._run_discovery_forever,
        args=(object(), object(), settings, None, None),
    )
    worker.start()
    worker.join(timeout=5)

    assert errors == [], f"repo built outside the worker thread raised: {errors}"
    assert tick_thread_ids == [worker.ident]

    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT status FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row is not None, "the tick's write never reached the database"


def test_run_monitoring_forever_constructs_its_own_repository_inside_the_worker_thread(
    tmp_path, monkeypatch
):
    """Samma AC3-regression (se testet ovan), monitoring-sidan av
    run.py:main()."""
    settings = _settings_with_db(tmp_path)
    errors: list[BaseException] = []
    tick_thread_ids: list[int] = []

    def fake_run_forever(connector, repo, settings):
        try:
            repo.start_run("run-2", "monitoring", datetime.now(UTC))
            tick_thread_ids.append(threading.get_ident())
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(run_module.monitoring_loop, "run_forever", fake_run_forever)

    worker = threading.Thread(
        target=run_module._run_monitoring_forever,
        args=(object(), settings),
    )
    worker.start()
    worker.join(timeout=5)

    assert errors == [], f"repo built outside the worker thread raised: {errors}"
    assert tick_thread_ids == [worker.ident]

    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT status FROM runs WHERE run_id = 'run-2'").fetchone()
    assert row is not None, "the tick's write never reached the database"
