"""Process lifecycle: graceful shutdown and exit codes (P1, 2026-10-01).

The bot used to stop only by being killed: every loop is a daemon thread and
main() joined them forever. The LIVE path is designed to survive that (claim
rows, restart recovery, exchange-side SL/TP), but a supervised service also
needs an orderly stop:

- `SHUTDOWN` is set by a stop request: the stop file (written by the
  supervisor or `python -m crypto_trading.supervisor stop`), Ctrl-C, SIGTERM
  or SIGBREAK.
- The LIVE execution loop runs each tick while holding `LIVE_TICK_LOCK` and
  never starts a new tick once `SHUTDOWN` is set; main() takes that lock
  before exiting, so a stop never lands in the middle of an order sequence.
- Nothing is cancelled or closed on shutdown: exchange-side SL/TP stay where
  they are, and the next start reconciles against the exchange.

Exit codes the supervisor acts on (crypto_trading/supervisor.py).
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

SHUTDOWN = threading.Event()
LIVE_TICK_LOCK = threading.Lock()

EXIT_OK = 0  # stopped on request
EXIT_CRASH = 1  # unexpected (also Python's default for an uncaught exception)
EXIT_CONFIG = 2  # configuration / credentials invalid - retrying fast is pointless
EXIT_ALREADY_RUNNING = 3  # another bot holds the database instance lock

STOP_FILE_ENV = "CRYPTO_TRADING_STOP_FILE"
DEFAULT_STOP_FILE = Path("data/STOP_BOT")


def stop_file_path() -> Path:
    return Path(os.environ.get(STOP_FILE_ENV) or DEFAULT_STOP_FILE)


def request_stop(path: Path | None = None) -> Path:
    target = path or stop_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stop requested\n", encoding="utf-8")
    return target


def clear_stop_request(path: Path | None = None) -> None:
    (path or stop_file_path()).unlink(missing_ok=True)


def watch_stop_file(path: Path, poll_seconds: float = 1.0) -> None:
    """Thread target: sets SHUTDOWN once the stop file exists."""
    while not SHUTDOWN.is_set():
        if path.exists():
            SHUTDOWN.set()
            return
        SHUTDOWN.wait(poll_seconds)


def install_signal_handlers() -> None:
    def _handler(_signum, _frame) -> None:
        SHUTDOWN.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # not the main thread / unsupported
                pass


def wait_for_live_tick_to_finish(timeout_seconds: float) -> bool:
    """True when no LIVE tick is running (the lock is then held, so none can
    start); False if one is still running after `timeout_seconds`."""
    return LIVE_TICK_LOCK.acquire(timeout=timeout_seconds)
