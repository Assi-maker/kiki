from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime

from fastapi import FastAPI

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


def test_run_notify_forever_constructs_its_own_repository_inside_the_worker_thread(
    tmp_path, monkeypatch
):
    """Fas 6: notify_loop.run_forever() följer samma trådbundna-anslutning-
    fix som discovery/monitoring - se testerna ovan."""
    settings = _settings_with_db(tmp_path)
    errors: list[BaseException] = []
    tick_thread_ids: list[int] = []

    def fake_run_forever(notifier, repo, settings):
        try:
            repo.start_run("run-3", "notify", datetime.now(UTC))
            tick_thread_ids.append(threading.get_ident())
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(run_module.notify_loop, "run_forever", fake_run_forever)

    worker = threading.Thread(
        target=run_module._run_notify_forever,
        args=(object(), settings),
    )
    worker.start()
    worker.join(timeout=5)

    assert errors == [], f"repo built outside the worker thread raised: {errors}"
    assert tick_thread_ids == [worker.ident]

    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT status FROM runs WHERE run_id = 'run-3'").fetchone()
    assert row is not None, "the tick's write never reached the database"


def test_run_dashboard_forever_catches_and_logs_a_real_uvicorn_bind_failure(tmp_path, monkeypatch):
    """Code-review-fynd (2026-08-30): en _run_dashboard_forever() utan
    try/except lät ett uvicorn.run()-fel (t.ex. porten redan upptagen) dö
    tyst - synligt bara i uvicorns egen stderr-loggning, aldrig via
    projektets egen strukturerade log_event()-konvention (samma disciplin
    som redan gäller run_discovery_tick()/run_monitoring_tick()/
    run_notify_tick()).

    Mockar `uvicorn.run()` med `SystemExit(3)`, INTE en generisk `OSError` -
    empiriskt verifierat (manuell körning mot en redan upptagen riktig
    port, se kod-granskningen 2026-08-30) att det är EXAKT vad uvicorns
    `Server.run()` faktiskt kastar vid ett bindningsfel, aldrig ett vanligt
    `Exception`-undantag. Ett test som bara mockade `OSError` hade sett ut
    att bevisa fixen men aldrig täckt det verkliga felfallet."""
    settings = _settings_with_db(tmp_path)
    captured_events: list[dict] = []

    def fake_log_event(run_id, **fields):
        captured_events.append({"run_id": run_id, **fields})

    def fake_uvicorn_run(*args, **kwargs):
        raise SystemExit(3)

    monkeypatch.setattr(run_module, "log_event", fake_log_event)
    monkeypatch.setattr(run_module.uvicorn, "run", fake_uvicorn_run)

    # Anropas direkt (inte i en tråd) - funktionen ska fånga felet och
    # returnera normalt, aldrig propagera det till anroparen (varken som
    # SystemExit eller något annat).
    run_module._run_dashboard_forever(FastAPI(), settings)

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event["event"] == "dashboard_server_failed"
    assert event["error_type"] == "SystemExit"
    assert event["error"] == "3"


def test_run_dashboard_forever_also_catches_a_generic_exception(tmp_path, monkeypatch):
    """Försvar-i-djupet: `except (Exception, SystemExit)` fångar även en
    vanlig `Exception`-subklass, inte bara det specifika `SystemExit`-fallet
    ovan - ifall ett framtida uvicorn/Starlette-fel någon gång kastas som
    ett vanligt undantag istället."""
    settings = _settings_with_db(tmp_path)
    captured_events: list[dict] = []

    def fake_log_event(run_id, **fields):
        captured_events.append({"run_id": run_id, **fields})

    def fake_uvicorn_run(*args, **kwargs):
        raise OSError("[Errno 10048] address already in use")

    monkeypatch.setattr(run_module, "log_event", fake_log_event)
    monkeypatch.setattr(run_module.uvicorn, "run", fake_uvicorn_run)

    run_module._run_dashboard_forever(FastAPI(), settings)

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event["event"] == "dashboard_server_failed"
    assert event["error_type"] == "OSError"
    assert "address already in use" in event["error"]
