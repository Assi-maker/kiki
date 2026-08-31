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
    format_daily_report_message,
    format_debug_error_message,
    format_no_trade_message,
)
from crypto_trading.performance.metrics import (
    compute_cumulative_pnl,
    compute_drawdown,
    compute_expectancy,
    compute_win_rate,
    trade_pnls,
)
from crypto_trading.storage.repository import Repository

_LEVEL_ORDER = {"important": 0, "decisions": 1, "debug": 2}


def _level_at_least(configured: str, minimum: str) -> bool:
    return _LEVEL_ORDER[configured] >= _LEVEL_ORDER[minimum]


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_relevant_no_trade(reasons: list[str]) -> bool:
    """Beslut 6 (2026-08-29): "relevant" NO_TRADE = samtliga sju assessments
    var status='ok' och QA godkände, men den deterministiska Risk/Signal
    Gate blockerade ändå (t.ex. max_concurrent_positions). En saknad/
    misslyckad assessment ger reasons som börjar med
    "missing_or_failed_assessment:" (se gate/risk_signal_gate.py) - det är
    INTE en relevant/intressant NO_TRADE, bara redan hanterat/loggat."""
    return not any(r.startswith("missing_or_failed_assessment:") for r in reasons)


class NotifierProtocol(Protocol):
    def send(self, text: str) -> None: ...


def run_notify_tick(notifier: NotifierProtocol, repo: Repository, settings: Settings) -> int:
    """En notifierings-tick (Fas 6, SPEC §12): läser bara redan-persisterade
    candidates/positions, formaterar och skickar en Telegram-notis per rad
    som inte redan skickats (telegram_events, idempotens SPEC §8.6). Läser
    ALDRIG evidence/gate-beslut på egen hand och fattar aldrig ett beslut -
    Risk/Signal Gate har redan avgjort CONFIRMED/NO_TRADE/REJECTED innan en
    rad ens kan dyka upp här (SPEC §1 kärnprincip 1).

    Notisnivåer (SPEC §12): `important` (default) skickar alltid CONFIRMED,
    CLOSED och daily report. `decisions` lägger till "relevanta" NO_TRADE
    (Beslut 6 - alla sju assessments ok + QA godkände, men gaten blockerade
    ändå). `debug` lägger till alla ÖVRIGA NO_TRADE (Beslut 7) samt
    `runs.status='error'`-rader. Daily report är INTE nivå-styrd - den
    skickas oavsett konfigurerad nivå, som bas-`important`-innehåll.
    Daily report innehåller entydiga operativa räknetal (instrument
    scannade, candidates, AI-analyser, CONFIRMED, NO_TRADE, REJECTED,
    öppna positioner, systemfel) plus de fyra performance-mått SPEC §12
    kräver (cumulative PnL, win rate, expectancy, drawdown) - beräknade via
    `repo.find_closed_positions()` + `performance/metrics.py`
    (2026-08-31 beslut), samma återanvända beräkningslogik som
    dashboardens `/api/performance` (Fas 8), aldrig en egen formel här.

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

        today = now.date()
        daily_report_id = f"daily_report:{today.isoformat()}"
        if not repo.has_telegram_event_been_sent(daily_report_id):
            day_start = _utc_day_start(now)
            closed_positions = repo.find_closed_positions()
            pnls = trade_pnls(closed_positions)
            message = format_daily_report_message(
                report_date=today,
                instruments_scanned=repo.sum_instruments_scanned_since(day_start),
                candidates_created=repo.count_candidates_created_since(day_start),
                ai_analyses=repo.count_ai_calls_since(day_start),
                confirmed=repo.count_candidates_by_status_since("CONFIRMED", day_start),
                no_trade=repo.count_candidates_by_status_since("NO_TRADE", day_start),
                rejected=repo.count_candidates_by_status_since("REJECTED", day_start),
                open_positions=repo.count_open_positions(),
                system_errors=repo.count_runs_by_status_since("error", day_start),
                cumulative_pnl=compute_cumulative_pnl(pnls),
                win_rate=compute_win_rate(pnls),
                expectancy=compute_expectancy(pnls),
                drawdown=compute_drawdown(closed_positions),
            )
            try:
                notifier.send(message)
                repo.record_telegram_event(daily_report_id, "daily_report", datetime.now(UTC))
                sent_count += 1
            except TelegramSendError as exc:
                errors.append(f"{daily_report_id}: {exc}")

        level = settings.notify.notification_level
        if _level_at_least(level, "decisions"):
            for candidate, reasons in repo.find_no_trade_candidates_pending_notification():
                relevant = _is_relevant_no_trade(reasons)
                if not (relevant or _level_at_least(level, "debug")):
                    continue  # "övrig" NO_TRADE - bara debug-nivå, hoppa över på decisions
                telegram_event_id = f"NO_TRADE:{candidate.candidate_id}"
                try:
                    notifier.send(format_no_trade_message(candidate, reasons))
                    repo.record_telegram_event(telegram_event_id, "NO_TRADE", datetime.now(UTC))
                    sent_count += 1
                except TelegramSendError as exc:
                    errors.append(f"{telegram_event_id}: {exc}")

        if _level_at_least(level, "debug"):
            for run in repo.find_error_runs_pending_notification():
                telegram_event_id = f"error_run:{run['run_id']}"
                try:
                    notifier.send(format_debug_error_message(run))
                    repo.record_telegram_event(telegram_event_id, "error_run", datetime.now(UTC))
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
