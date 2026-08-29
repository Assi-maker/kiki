from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol

from crypto_trading.config.loader import Settings
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.notify.telegram import (
    TelegramSendError,
    format_closed_message,
    format_confirmed_message,
)
from crypto_trading.storage.repository import Repository


class NotifierProtocol(Protocol):
    def send(self, text: str) -> None: ...


def run_notify_tick(notifier: NotifierProtocol, repo: Repository, settings: Settings) -> int:
    """En notifierings-tick (Fas 6, SPEC §12): läser bara redan-persisterade
    candidates/positions, formaterar och skickar en Telegram-notis per rad
    som inte redan skickats (telegram_events, idempotens SPEC §8.6). Läser
    ALDRIG evidence/gate-beslut på egen hand och fattar aldrig ett beslut -
    Risk/Signal Gate har redan avgjort CONFIRMED/NO_TRADE/REJECTED innan en
    rad ens kan dyka upp här (SPEC §1 kärnprincip 1).

    Minimal implementation (Fas 6, första versionen): skickar alltid
    CONFIRMED- och CLOSED-notiser oavsett `settings.notify.notification_level`
    - notisnivåerna decisions/debug (NO_TRADE, detaljerade pipeline-events)
    är INTE implementerade ännu, medvetet utanför denna första leverans.

    Två fail-safe-lager, samma mönster som discovery_loop.py/
    monitoring_loop.py: ett enskilt sändningsfel (TelegramSendError) hoppar
    bara över den raden (försöks igen nästa tick, aldrig persisterad i
    telegram_events förrän den faktiskt lyckats skickas), och ett OVÄNTAT
    fel kraschar aldrig run_forever()."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "notify", now)
    sent_count = 0
    errors: list[str] = []
    try:
        for candidate in repo.find_candidates_pending_notification("CONFIRMED"):
            telegram_event_id = f"CONFIRMED:{candidate.candidate_id}"
            position = repo.get_position(candidate.candidate_id)
            if position is None:
                continue  # position_opening.py hann inte ännu - försöks igen nästa tick
            try:
                notifier.send(format_confirmed_message(candidate, position))
                repo.record_telegram_event(telegram_event_id, "CONFIRMED", datetime.now(UTC))
                sent_count += 1
            except TelegramSendError as exc:
                errors.append(f"{telegram_event_id}: {exc}")

        for position in repo.find_positions_pending_notification():
            telegram_event_id = f"CLOSED:{position.position_id}"
            forecast = repo.get_forecast_record(position.candidate_id)
            try:
                notifier.send(format_closed_message(position, forecast))
                repo.record_telegram_event(telegram_event_id, "CLOSED", datetime.now(UTC))
                sent_count += 1
            except TelegramSendError as exc:
                errors.append(f"{telegram_event_id}: {exc}")

        repo.complete_run(
            run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors
        )
        return sent_count
    except Exception as exc:
        log_event(run_id, event="notify_tick_failed", error_type=type(exc).__name__, error=str(exc))
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])
        return sent_count


def run_forever(notifier: NotifierProtocol, repo: Repository, settings: Settings) -> None:
    while True:
        run_notify_tick(notifier, repo, settings)
        time.sleep(settings.notify.notify_interval_seconds)
