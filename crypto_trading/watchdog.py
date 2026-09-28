"""Watchdog and heartbeat (2026-09-28).

Two failures went unnoticed on 2026-09-27:
- the process died silently at 07:13 UTC and stayed down for 4 h 44 min;
- at 16:07-20:11 UTC every loop froze inside a still-running process, so the
  LIVE time limit fired up to 106 min late.

In-process (`run_watchdog_forever`, its own thread): once a minute it reads
the latest `runs.started_at` per loop - every loop already records a run per
tick, so no loop had to change - checks each loop thread is alive, folds in
AI health, writes `logs/heartbeat.json` and sends a Telegram alert on every
state change (and a reminder while a problem lasts).

Out-of-process (`python -m crypto_trading.watchdog`, run by Windows Task
Scheduler): alerts when the heartbeat file is missing or old, which is the
only way to notice a dead, frozen or sleeping process.

Alert-only by design. This module holds no trading connector and has no code
path that can open, modify or close anything on the exchange - there is no
automatic recovery that could add risk.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from crypto_trading.ai_health import AIHealth
from crypto_trading.logging import log_event, new_run_id

# Stale threshold per loop (`runs.run_type`), from the measured normal
# cadence 2026-09-27/28 (median / max gap): live_execution 0.5/0.7 min,
# monitoring 0.6/0.7, notify 1.0/1.0, guardian 1.2/1.5, detective 5.0/5.7,
# godfather_intelligence 15.4/16.8, discovery 23/28 (up to 42 with slow AI).
LOOP_STALE_AFTER_SECONDS: dict[str, int] = {
    "live_execution": 5 * 60,
    "monitoring": 5 * 60,
    "notify": 10 * 60,
    "guardian": 10 * 60,
    "detective": 20 * 60,
    "godfather_intelligence": 60 * 60,
    "discovery": 90 * 60,
    # P3-P6 shadow evaluator: every 30 min, a tick can take several minutes
    "shadow_evaluation": 90 * 60,
}
DEFAULT_STALE_AFTER_SECONDS = 30 * 60
REPEAT_ALERT_AFTER = timedelta(minutes=60)
HEARTBEAT_STALE_AFTER = timedelta(minutes=3)


class _Notifier(Protocol):
    def send(self, text: str) -> None: ...


class _ThreadLike(Protocol):
    def is_alive(self) -> bool: ...


@dataclass(frozen=True)
class LoopStatus:
    name: str
    last_run_at: datetime | None
    age_seconds: int
    threshold_seconds: int
    thread_alive: bool | None
    stale: bool


def evaluate_loops(
    last_runs: dict[str, datetime],
    threads: dict[str, _ThreadLike | None],
    process_started_at: datetime,
    now: datetime,
) -> list[LoopStatus]:
    """One status per expected loop. A run older than this process counts as
    "not yet run": the process start is the reference, so a slow first tick
    after a restart is not an alert, but a loop that never ticks is."""
    statuses = []
    for name, thread in threads.items():
        last = last_runs.get(name)
        reference = last if last is not None and last >= process_started_at else process_started_at
        age = int((now - reference).total_seconds())
        threshold = LOOP_STALE_AFTER_SECONDS.get(name, DEFAULT_STALE_AFTER_SECONDS)
        alive = thread.is_alive() if thread is not None else None
        statuses.append(
            LoopStatus(
                name=name,
                last_run_at=last,
                age_seconds=age,
                threshold_seconds=threshold,
                thread_alive=alive,
                stale=age > threshold or alive is False,
            )
        )
    return statuses


@dataclass
class AlertState:
    """Last alerted state per key ('loop:<name>', 'ai') and when it was sent."""

    bad_since_alert: dict[str, datetime] = field(default_factory=dict)


def _send(notifier: _Notifier | None, text: str, run_id: str) -> None:
    log_event(run_id, event="watchdog_alert", message=text)
    if notifier is None:
        return
    try:
        notifier.send(text)
    except Exception as exc:  # noqa: BLE001 - an alert failure must never stop the watchdog
        log_event(run_id, event="watchdog_alert_send_failed", error_type=type(exc).__name__)


def _transition(
    alerts: AlertState,
    key: str,
    bad: bool,
    now: datetime,
    notifier: _Notifier | None,
    bad_text: str,
    ok_text: str,
    run_id: str,
) -> None:
    last = alerts.bad_since_alert.get(key)
    if bad:
        if last is None or now - last >= REPEAT_ALERT_AFTER:
            alerts.bad_since_alert[key] = now
            _send(notifier, bad_text, run_id)
    elif last is not None:
        del alerts.bad_since_alert[key]
        _send(notifier, ok_text, run_id)


def _write_json_atomically(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def run_watchdog_tick(
    repo,
    notifier: _Notifier | None,
    health: AIHealth,
    threads: dict[str, _ThreadLike | None],
    process_started_at: datetime,
    heartbeat_path: Path,
    alerts: AlertState,
    now: datetime,
) -> list[LoopStatus]:
    run_id = new_run_id()
    statuses = evaluate_loops(
        repo.latest_run_started_at_by_type(), threads, process_started_at, now
    )
    ai = health.snapshot(now)
    _write_json_atomically(
        heartbeat_path,
        {
            "pid": os.getpid(),
            "written_at": now.isoformat(),
            "process_started_at": process_started_at.isoformat(),
            "loops": {
                s.name: {
                    "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                    "age_seconds": s.age_seconds,
                    "threshold_seconds": s.threshold_seconds,
                    "thread_alive": s.thread_alive,
                    "stale": s.stale,
                }
                for s in statuses
            },
            "ai": ai,
        },
    )
    for s in statuses:
        if s.stale:
            log_event(
                run_id,
                event="watchdog_loop_stale",
                loop=s.name,
                age_seconds=s.age_seconds,
                threshold_seconds=s.threshold_seconds,
                thread_alive=s.thread_alive,
            )
        why = (
            "tråden är död"
            if s.thread_alive is False
            else f"inget körning på {s.age_seconds // 60} min"
        )
        _transition(
            alerts,
            f"loop:{s.name}",
            s.stale,
            now,
            notifier,
            f"⚠️ BOT WATCHDOG: loopen {s.name} står still ({why}, gräns "
            f"{s.threshold_seconds // 60} min). Inga nya ordrar skickas av watchdog.",
            f"✅ BOT WATCHDOG: loopen {s.name} kör igen (OK).",
            run_id,
        )
    ai_bad = ai["status"] in ("DOWN", "DEGRADED")
    _transition(
        alerts,
        "ai",
        ai_bad,
        now,
        notifier,
        f"⚠️ AI {ai['status']}: {ai['last_error_kind']} ({ai['consecutive_failures']} fel i rad, "
        f"sedan {ai['failing_since']}). Ingen ny kandidat kan bli CONFIRMED utan AI - "
        f"ingen ny LIVE-entry tills AI fungerar. Öppna positioner skyddas av SL/TP på börsen.",
        "✅ AI OK igen: anropen till Anthropic lyckas.",
        run_id,
    )
    return statuses


def run_watchdog_forever(
    repo,
    notifier: _Notifier | None,
    health: AIHealth,
    threads: dict[str, _ThreadLike | None],
    process_started_at: datetime,
    heartbeat_path: Path,
    interval_seconds: int = 60,
) -> None:
    alerts = AlertState()
    while True:
        try:
            run_watchdog_tick(
                repo,
                notifier,
                health,
                threads,
                process_started_at,
                heartbeat_path,
                alerts,
                datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001 - the watchdog itself must never die
            log_event(
                "watchdog",
                event="watchdog_tick_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        time.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Out-of-process check (Windows Task Scheduler, every few minutes).
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_heartbeat_file(
    heartbeat_path: Path, state_path: Path, notifier: _Notifier | None, now: datetime
) -> bool:
    """True when the heartbeat is fresh. Alerts on the change to stale (and
    every REPEAT_ALERT_AFTER while it stays stale) and on recovery; the
    last alert time survives between runs in `state_path`."""
    run_id = new_run_id()
    beat = _read_json(heartbeat_path)
    written_at = None
    if beat is not None:
        try:
            written_at = datetime.fromisoformat(beat["written_at"])
        except (KeyError, TypeError, ValueError):
            written_at = None
    fresh = written_at is not None and now - written_at <= HEARTBEAT_STALE_AFTER
    state = _read_json(state_path) or {}
    alerts = AlertState()
    if state.get("bad_since_alert"):
        alerts.bad_since_alert["process"] = datetime.fromisoformat(state["bad_since_alert"])
    if written_at is None:
        bad_text = (
            f"🚨 BOT DÖD? Ingen läsbar heartbeat ({heartbeat_path.name}). Processen körs "
            "troligen inte. Öppna LIVE-positioner skyddas bara av SL/TP på börsen."
        )
    else:
        minutes = int((now - written_at).total_seconds() // 60)
        bad_text = (
            f"🚨 BOT STÅR STILL: senaste heartbeat för {minutes} min sedan (pid "
            f"{beat.get('pid')}). Processen är död, frusen eller datorn sover. Time limit, "
            "PP och Guardian körs inte; bara SL/TP på börsen skyddar."
        )
    _transition(
        alerts,
        "process",
        not fresh,
        now,
        notifier,
        bad_text,
        "✅ BOT OK igen: heartbeat är färsk.",
        run_id,
    )
    since = alerts.bad_since_alert.get("process")
    _write_json_atomically(
        state_path,
        {"bad_since_alert": since.isoformat() if since else None, "checked_at": now.isoformat()},
    )
    return fresh


def main() -> None:
    from dotenv import load_dotenv

    from crypto_trading.run import build_notifier_from_env, log_dir_from_env

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    log_dir = log_dir_from_env()
    check_heartbeat_file(
        log_dir / "heartbeat.json",
        log_dir / "watchdog_state.json",
        build_notifier_from_env(),
        datetime.now(UTC),
    )


if __name__ == "__main__":
    main()
