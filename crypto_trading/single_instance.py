"""One bot process per database - enforced by the operating system.

Found 2026-09-25 while auditing observation integrity: on 2026-09-12 two
bot processes ran against the same database at the same time. Guardian
wrote 635 near-duplicate observations (629 overlapping runs), and every
other loop - including LIVE execution, protected only by its claim
idempotency - ran twice.

The lock is an OS-level byte-range lock on a file next to the database
(`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), NOT a "pid file":
the operating system releases it the instant the holding process exits,
however it exits. A crash can therefore never leave a stale lock that
blocks the next start - which a pid file would, and which would be the
worse failure for a trading process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]
try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        """Raises AlreadyRunningError if another process holds the lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")  # noqa: SIM115 - held for the process lifetime
        try:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError(
                f"another crypto_trading process already holds {self._path}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if msvcrt is not None:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def lock_path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".instance.lock")
