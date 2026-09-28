"""Process-wide Anthropic API health (2026-09-28).

On 2026-09-27 04:13-06:37 UTC the account ran out of credit: 399 calls came
back HTTP 400 "credit balance is too low", every candidate fell through to
the Gate's missing_or_failed_assessment NO_TRADE (fail-closed, correct) - and
nobody was told. This module classifies every Anthropic failure and keeps
one thread-safe health state that the watchdog turns into an alert.

It never decides anything about a trade. A failed AI call still returns a
status="failed" assessment exactly as before, so no candidate can become
CONFIRMED without real AI output.
"""

from __future__ import annotations

import threading
from datetime import datetime

import anthropic

# Failures no retry can fix while they last: the account has no credit or
# the key is rejected. Retrying only burns the tick.
NON_RETRYABLE_KINDS = frozenset({"CREDIT_EXHAUSTED", "AUTH"})


def classify_api_error(exc: Exception) -> str:
    """One stable class per failure mode, from the exception type and HTTP
    status - never from a guess about the text, except the one message
    Anthropic uses for credit exhaustion (it arrives as an ordinary 400)."""
    if isinstance(exc, anthropic.APITimeoutError):
        return "TIMEOUT"
    if isinstance(exc, anthropic.APIConnectionError):
        return "CONNECTION"
    status = getattr(exc, "status_code", None)
    if status == 400 and "credit balance" in str(exc).lower():
        return "CREDIT_EXHAUSTED"
    if status in (401, 403):
        return "AUTH"
    if status == 429:
        return "RATE_LIMITED"
    if status is not None and (status >= 500 or status == 529):
        return "OVERLOADED"
    if status == 400:
        return "BAD_REQUEST"
    return "OTHER"


class AIHealth:
    """OK / DEGRADED / DOWN.

    DOWN: the latest failure since the last success is non-retryable
    (credit, auth) - the AI part is not working at all.
    DEGRADED: `degraded_after` or more failures in a row of any other kind.
    A single success resets everything to OK."""

    def __init__(self, degraded_after: int = 5):
        self._lock = threading.Lock()
        self._degraded_after = degraded_after
        self._consecutive_failures = 0
        self._last_error_kind: str | None = None
        self._last_error_agent: str | None = None
        self._failing_since: datetime | None = None
        self._last_success_at: datetime | None = None
        self._down = False

    def record_failure(self, kind: str, agent_name: str, now: datetime) -> None:
        with self._lock:
            if self._consecutive_failures == 0:
                self._failing_since = now
            self._consecutive_failures += 1
            self._last_error_kind = kind
            self._last_error_agent = agent_name
            if kind in NON_RETRYABLE_KINDS:
                self._down = True

    def record_success(self, now: datetime) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._failing_since = None
            self._last_success_at = now
            self._down = False

    def snapshot(self, now: datetime) -> dict:
        with self._lock:
            if self._down:
                status = "DOWN"
            elif self._consecutive_failures >= self._degraded_after:
                status = "DEGRADED"
            else:
                status = "OK"
            return {
                "status": status,
                "consecutive_failures": self._consecutive_failures,
                "last_error_kind": self._last_error_kind,
                "last_error_agent": self._last_error_agent,
                "failing_since": self._failing_since.isoformat() if self._failing_since else None,
                "last_success_at": (
                    self._last_success_at.isoformat() if self._last_success_at else None
                ),
                "checked_at": now.isoformat(),
            }


AI_HEALTH = AIHealth()
