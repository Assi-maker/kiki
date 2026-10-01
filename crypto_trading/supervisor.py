"""Bot supervisor - the service wrapper (P1, 2026-10-01).

Keeps exactly one bot process running and restarts it when it dies, without
a console window, independent of any terminal or Claude Code session:

    pythonw -m crypto_trading.supervisor run      # what Task Scheduler starts
    python  -m crypto_trading.supervisor stop     # orderly stop (bot + supervisor)
    python  -m crypto_trading.supervisor status

Guarantees:
- one supervisor per machine user (its own OS lock, data/supervisor.lock), and
  one bot per database (the bot's own instance lock - a second bot exits with
  EXIT_ALREADY_RUNNING and the supervisor backs off instead of fighting);
- restart after a crash with backoff 10 s, 30 s, 60 s ... 10 min (reset after
  30 min of healthy running); a configuration/credential refusal
  (EXIT_CONFIG) is retried only every 15 min - never a hot loop;
- an orderly stop: the stop file asks the bot to finish its LIVE tick and exit
  (crypto_trading/lifecycle.py); a bot that does not exit within 150 s is
  terminated. Nothing on the exchange is touched - the exchange-side SL/TP
  stay, and the next start reconciles from the exchange (source of truth);
- every start/exit/restart is logged to logs/supervisor.log and the current
  state to logs/supervisor_heartbeat.json (the bot keeps writing its own
  logs/heartbeat.json, watched by the existing external watchdog task).

The supervisor never imports trading code and holds no exchange credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading import lifecycle
from crypto_trading.single_instance import AlreadyRunningError, InstanceLock

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "data" / "supervisor.lock"
# Same log directory as the bot itself (CRYPTO_TRADING_LOG_DIR, default logs/).
_LOG_DIR = Path(os.environ.get("CRYPTO_TRADING_LOG_DIR") or ROOT / "logs")
LOG_FILE = _LOG_DIR / "supervisor.log"
HEARTBEAT_FILE = _LOG_DIR / "supervisor_heartbeat.json"
CHILD_OUTPUT = _LOG_DIR / "bot_stdout.log"
STOP_GRACE_SECONDS = 150
CRASH_BACKOFF = (10, 30, 60, 120, 300, 600)
CONFIG_BACKOFF_SECONDS = 900
ALREADY_RUNNING_BACKOFF_SECONDS = 120
HEALTHY_RESET_SECONDS = 1800


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(message: str, log_file: Path = LOG_FILE) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"{_now()} {message}\n")


@dataclass
class RestartPolicy:
    """Pure decision: how long to wait before the next start, or None = stop."""

    crash_streak: int = 0
    history: list[tuple[int, float]] = field(default_factory=list)

    def after_exit(self, exit_code: int, ran_seconds: float, stop_requested: bool) -> float | None:
        self.history.append((exit_code, ran_seconds))
        if stop_requested or exit_code == lifecycle.EXIT_OK:
            return None
        if exit_code == lifecycle.EXIT_CONFIG:
            return CONFIG_BACKOFF_SECONDS
        if exit_code == lifecycle.EXIT_ALREADY_RUNNING:
            return ALREADY_RUNNING_BACKOFF_SECONDS
        if ran_seconds >= HEALTHY_RESET_SECONDS:
            self.crash_streak = 0
        delay = CRASH_BACKOFF[min(self.crash_streak, len(CRASH_BACKOFF) - 1)]
        self.crash_streak += 1
        return delay


def bot_command(python: str | None = None) -> list[str]:
    return [python or sys.executable, "-u", "-m", "crypto_trading.run"]


def write_heartbeat(state: dict, path: Path = HEARTBEAT_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**state, "written_at": _now()}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _sleep_unless_stopped(seconds: float, stop_file: Path) -> bool:
    """True when a stop was requested during the wait."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_file.exists():
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return stop_file.exists()


def supervise(command: list[str], stop_file: Path, *, max_starts: int | None = None,
              policy: RestartPolicy | None = None, env: dict | None = None,
              cwd: Path = ROOT, log_file: Path = LOG_FILE,
              heartbeat_file: Path = HEARTBEAT_FILE, output_file: Path = CHILD_OUTPUT) -> int:
    """Run `command` until a stop is requested. Returns the number of starts."""
    policy = policy or RestartPolicy()
    child_env = {**(env if env is not None else os.environ),
                 lifecycle.STOP_FILE_ENV: str(stop_file)}
    starts = 0
    while max_starts is None or starts < max_starts:
        if stop_file.exists():
            log("stop requested before start - not starting", log_file)
            break
        starts += 1
        started = time.monotonic()
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("ab") as out:
            child = subprocess.Popen(
                command, cwd=cwd, env=child_env, stdout=out, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        log(f"START bot pid={child.pid} start#={starts}", log_file)
        write_heartbeat({"state": "running", "bot_pid": child.pid, "starts": starts,
                         "supervisor_pid": os.getpid()}, heartbeat_file)
        stop_requested = False
        while child.poll() is None:
            if stop_file.exists():
                stop_requested = True
                log(f"stop requested - waiting up to {STOP_GRACE_SECONDS}s for pid={child.pid}",
                    log_file)
                try:
                    child.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    log(f"bot pid={child.pid} did not stop in time - terminating", log_file)
                    child.terminate()
                    child.wait(timeout=30)
                break
            time.sleep(1.0)
        code = child.returncode
        ran = time.monotonic() - started
        stop_requested = stop_requested or stop_file.exists()
        log(f"EXIT bot pid={child.pid} code={code} ran={ran:.0f}s stop_requested={stop_requested}",
            log_file)
        delay = policy.after_exit(code, ran, stop_requested)
        if delay is None:
            break
        log(f"RESTART in {delay}s (exit code {code})", log_file)
        write_heartbeat({"state": "restart_wait", "last_exit_code": code, "starts": starts,
                         "restart_in_seconds": delay, "supervisor_pid": os.getpid()},
                        heartbeat_file)
        if _sleep_unless_stopped(delay, stop_file):
            log("stop requested during restart wait", log_file)
            break
    write_heartbeat({"state": "stopped", "starts": starts, "supervisor_pid": os.getpid()},
                    heartbeat_file)
    return starts


def cmd_run() -> int:
    lock = InstanceLock(LOCK_FILE)
    try:
        lock.acquire()
    except AlreadyRunningError:
        log("another supervisor is already running - exiting")
        return lifecycle.EXIT_ALREADY_RUNNING
    stop_file = (ROOT / lifecycle.DEFAULT_STOP_FILE).resolve()
    lifecycle.clear_stop_request(stop_file)  # a stale stop file must not block this start
    log(f"SUPERVISOR START pid={os.getpid()} python={sys.executable}")
    try:
        supervise(bot_command(), stop_file)
    finally:
        lifecycle.clear_stop_request(stop_file)
        log("SUPERVISOR EXIT")
        lock.release()
    return lifecycle.EXIT_OK


def cmd_stop() -> int:
    path = lifecycle.request_stop((ROOT / lifecycle.DEFAULT_STOP_FILE).resolve())
    print(f"stop requested ({path}); the bot finishes its LIVE tick and exits")
    return 0


def cmd_status() -> int:
    for path in (HEARTBEAT_FILE, _LOG_DIR / "heartbeat.json"):
        print(f"== {path}")
        print(path.read_text(encoding="utf-8") if path.exists() else "(missing)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="crypto_trading bot supervisor")
    parser.add_argument("command", choices=("run", "stop", "status"))
    args = parser.parse_args()
    raise SystemExit({"run": cmd_run, "stop": cmd_stop, "status": cmd_status}[args.command]())


if __name__ == "__main__":
    main()
