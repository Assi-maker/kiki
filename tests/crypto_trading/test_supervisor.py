"""P1 (2026-10-01): supervisor (service wrapper) and bot lifecycle.

Real child processes, but harmless ones (`python -c ...`): never the bot."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from crypto_trading import lifecycle, supervisor
from crypto_trading.run import startup_problems
from crypto_trading.single_instance import AlreadyRunningError, InstanceLock


@pytest.fixture
def paths(tmp_path):
    return {
        "stop": tmp_path / "STOP_BOT",
        "log": tmp_path / "supervisor.log",
        "hb": tmp_path / "hb.json",
        "out": tmp_path / "out.log",
    }


def _supervise(command, paths, **kw):
    return supervisor.supervise(
        command, paths["stop"], log_file=paths["log"], heartbeat_file=paths["hb"],
        output_file=paths["out"], cwd=Path.cwd(), **kw,
    )


def _py(code):
    return [sys.executable, "-c", code]


# --------------------------------------------------------------- policy (pure)

def test_policy_stops_on_clean_exit_or_stop_request():
    assert supervisor.RestartPolicy().after_exit(0, 5, False) is None
    assert supervisor.RestartPolicy().after_exit(1, 5, True) is None


def test_policy_backs_off_on_repeated_crashes_and_resets_after_healthy_run():
    policy = supervisor.RestartPolicy()
    delays = [policy.after_exit(1, 5, False) for _ in range(8)]
    assert delays == [10, 30, 60, 120, 300, 600, 600, 600]
    assert policy.after_exit(1, supervisor.HEALTHY_RESET_SECONDS + 1, False) == 10


def test_policy_never_hot_loops_on_config_or_duplicate_instance():
    policy = supervisor.RestartPolicy()
    assert policy.after_exit(lifecycle.EXIT_CONFIG, 1, False) == supervisor.CONFIG_BACKOFF_SECONDS
    assert (policy.after_exit(lifecycle.EXIT_ALREADY_RUNNING, 1, False)
            == supervisor.ALREADY_RUNNING_BACKOFF_SECONDS)
    assert supervisor.CONFIG_BACKOFF_SECONDS >= 600


# ------------------------------------------------------------ real children

class _FastPolicy(supervisor.RestartPolicy):
    def after_exit(self, exit_code, ran_seconds, stop_requested):
        delay = super().after_exit(exit_code, ran_seconds, stop_requested)
        return None if delay is None else 0.1


def test_a_crashing_child_is_restarted_and_every_exit_is_logged(paths):
    starts = _supervise(_py("import sys; sys.exit(1)"), paths, max_starts=3, policy=_FastPolicy())
    assert starts == 3
    text = paths["log"].read_text(encoding="utf-8")
    assert text.count("START bot") == 3 and text.count("code=1") == 3
    assert text.count("RESTART in") == 3


def test_a_clean_exit_is_not_restarted(paths):
    assert _supervise(_py("pass"), paths, max_starts=5, policy=_FastPolicy()) == 1


def test_the_stop_file_makes_the_child_exit_and_the_supervisor_return(paths):
    # child = a stand-in bot that honours the same stop-file contract
    child = _py(
        "import os, time, pathlib, sys\n"
        "p = pathlib.Path(os.environ['CRYPTO_TRADING_STOP_FILE'])\n"
        "while not p.exists(): time.sleep(0.1)\n"
        "sys.exit(0)\n"
    )
    timer = threading.Timer(1.5, lambda: paths["stop"].write_text("stop"))
    timer.start()
    started = time.monotonic()
    starts = _supervise(child, paths, max_starts=5, policy=_FastPolicy())
    assert starts == 1 and time.monotonic() - started < 20
    assert "stop_requested=True" in paths["log"].read_text(encoding="utf-8")
    assert '"state": "stopped"' in paths["hb"].read_text(encoding="utf-8")


def test_a_child_that_ignores_the_stop_is_terminated_after_the_grace(paths, monkeypatch):
    monkeypatch.setattr(supervisor, "STOP_GRACE_SECONDS", 1)
    threading.Timer(1.0, lambda: paths["stop"].write_text("stop")).start()
    starts = _supervise(_py("import time; time.sleep(60)"), paths, max_starts=3,
                        policy=_FastPolicy())
    assert starts == 1
    assert "did not stop in time - terminating" in paths["log"].read_text(encoding="utf-8")


def test_an_existing_stop_file_prevents_any_start(paths):
    paths["stop"].write_text("stop")
    assert _supervise(_py("pass"), paths, max_starts=3) == 0


def test_child_output_is_captured_to_the_log_file(paths):
    _supervise(_py("print('hello from bot')"), paths, max_starts=1)
    assert "hello from bot" in paths["out"].read_text(encoding="utf-8")


# ------------------------------------------------------------ single instance

def test_a_second_lock_holder_in_another_process_is_refused(tmp_path):
    lock_path = tmp_path / "x.lock"
    holder = subprocess.Popen(_py(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from crypto_trading.single_instance import InstanceLock\n"
        f"lock = InstanceLock(Path(r'{lock_path}'))\n"  # kept referenced, as in run.main
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    ), stdout=subprocess.PIPE, text=True, cwd=Path.cwd())
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunningError):
            InstanceLock(lock_path).acquire()
    finally:
        holder.kill()
        holder.wait()
    # The OS releases it with the process - a crash never leaves a stale lock -
    # but on Windows the release after a hard kill can lag a moment, which is
    # why the supervisor backs off on EXIT_ALREADY_RUNNING instead of failing.
    deadline = time.monotonic() + 15
    while True:
        lock = InstanceLock(lock_path)
        try:
            lock.acquire()
            break
        except AlreadyRunningError:
            assert time.monotonic() < deadline, "lock never released after the holder died"
            time.sleep(0.2)
    lock.release()


def test_a_second_bot_exits_with_already_running_and_is_backed_off(tmp_path, monkeypatch):
    policy = supervisor.RestartPolicy()
    assert policy.after_exit(lifecycle.EXIT_ALREADY_RUNNING, 0.5, False) >= 60


# --------------------------------------------------------------- bot startup

def test_startup_refuses_without_ai_key_but_not_without_live_keys():
    assert startup_problems({}) == ["ANTHROPIC_API_KEY missing"]
    assert startup_problems({"ANTHROPIC_API_KEY": "x"}) == []  # no BINGX keys: LIVE just off


def test_stop_file_watch_sets_shutdown(tmp_path):
    lifecycle.SHUTDOWN.clear()
    stop = tmp_path / "STOP"
    t = threading.Thread(target=lifecycle.watch_stop_file, args=(stop, 0.05), daemon=True)
    t.start()
    stop.write_text("x")
    assert lifecycle.SHUTDOWN.wait(5)
    lifecycle.SHUTDOWN.clear()


def test_shutdown_waits_for_a_running_live_tick():
    lifecycle.LIVE_TICK_LOCK.acquire()
    try:
        assert lifecycle.wait_for_live_tick_to_finish(0.2) is False  # tick still running
    finally:
        lifecycle.LIVE_TICK_LOCK.release()
    assert lifecycle.wait_for_live_tick_to_finish(0.2) is True
    lifecycle.LIVE_TICK_LOCK.release()


def test_live_loop_never_starts_a_tick_after_shutdown(monkeypatch):
    from crypto_trading import live_execution_loop

    calls = []
    monkeypatch.setattr(live_execution_loop, "run_live_execution_tick",
                        lambda *a, **k: calls.append(1))
    lifecycle.SHUTDOWN.set()
    try:
        live_execution_loop.run_forever(None, None, None, {}, {}, None)
    finally:
        lifecycle.SHUTDOWN.clear()
    assert calls == []


def test_safe_mode_forces_every_arm_flag_off_even_when_env_arms_them(monkeypatch):
    from crypto_trading.config import loader
    from crypto_trading.run import build_notifier_from_env

    for name in ("CRYPTO_TRADING_LIVE_EXECUTION_ENABLED", "CRYPTO_TRADING_GUARDIAN_ENABLED",
                 "CRYPTO_TRADING_DEMO_EXECUTION_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "1")
    assert loader.is_live_execution_enabled() and loader.is_guardian_enabled()
    monkeypatch.setenv("CRYPTO_TRADING_SAFE_MODE", "1")
    assert not loader.is_live_execution_enabled()
    assert not loader.is_guardian_enabled()
    assert not loader.is_demo_execution_enabled()
    assert build_notifier_from_env() is None
