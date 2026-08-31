from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.logging import redact_error_list
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.schemas.trade import Position

_TELEGRAM_API_BASE = "https://api.telegram.org"

# Duplicate-send-fynd (code review 2026-08-29): sendMessage är en
# side-effecting POST utan idempotenskyckel - att blint retrya på VARJE
# httpx.HTTPError (som tidigare) riskerar att skicka samma meddelande två
# gånger om felet inträffade EFTER att requesten redan nått Telegram (t.ex.
# en timeout medan vi väntade på svaret). Bara fel som garanterat betyder
# att requesten ALDRIG lämnade klienten - ett misslyckat
# anslutningsförsök - är säkra att retrya automatiskt här. Allt annat
# (timeout efter att requesten skickats, ett trasigt svar, en definitiv
# HTTP-felstatus som en 5xx) rapporteras som ETT sändningsfel, aldrig ett
# blint nytt försök inom samma send()-anrop - en genuin, opersisterad rad
# försöks ändå igen på notify_loop.py:s NÄSTA schemalagda tick, ett mycket
# säkrare intervall (minuter, inte millisekunder) för en eventuell
# retry än ett omedelbart nytt anrop.
_SAFE_TO_RETRY_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)


class TelegramSendError(Exception):
    """Ett Telegram sendMessage-anrop misslyckades. Bär ALDRIG den råa
    request-URL:en (som har boten-token inbäddad i path:en, inte som en
    key=value-parameter - crypto_trading/logging.py::redact()s befintliga
    mönster fångar inte det) eller det råa httpx-undantaget - bara typ/
    statuskod, samma disciplin som ConnectorUnavailableError
    (connectors/base.py)."""


def _risk_reward_ratio(entry: Decimal, stop_loss: Decimal, target: Decimal) -> str:
    risk = entry - stop_loss
    reward = target - entry
    if risk <= 0:
        return "n/a"
    return f"{(reward / risk):.2f}"


def format_confirmed_message(candidate: Candidate, position: Position) -> str:
    """SPEC §12 CONFIRMED-notis. Tar redan hämtade Candidate/Position-objekt
    som argument - gör ALDRIG en egen DB-fråga (Fas 6 AC4: samma underlag
    som en framtida dashboard-vy skulle läsa, aldrig en andra beräkning).
    candidate.bull_thesis/forecast/risk kan strukturellt inte vara None för
    en CONFIRMED candidate (Risk/Signal Gate kräver alla sju assessments
    status='ok' innan CONFIRMED, se gate/risk_signal_gate.py)."""
    evidence = candidate.evidence_record
    dominant_scenario = max(
        candidate.forecast.scenario_probabilities,
        key=candidate.forecast.scenario_probabilities.get,
    )
    rr = _risk_reward_ratio(position.simulated_fill_entry, position.stop_loss, position.target)
    return "\n".join(
        [
            f"✅ CONFIRMED — {candidate.instrument} {position.direction}",
            f"Entry: {position.simulated_fill_entry}",
            f"Stop-loss: {position.stop_loss}",
            f"Target: {position.target}",
            f"Risk/reward: {rr}",
            f"Candidate score: {evidence.candidate_score:.2f}",
            f"Evidence: {', '.join(evidence.trigger_reasons)}",
            f"AI team: {candidate.bull_thesis.hypothesis}",
            f"Forecast ({candidate.forecast.horizon}): {dominant_scenario} "
            f"({candidate.forecast.scenario_probabilities[dominant_scenario]:.0%})",
            f"Time: {candidate.updated_at.isoformat()}",
        ]
    )


def format_closed_message(position: Position, forecast: ForecastRecord | None) -> str:
    """SPEC §12 CLOSED-notis. `forecast` kan vara None (fail-safe - ska
    strukturellt aldrig hända för en position som gick via CONFIRMED, men
    formateringen kraschar aldrig om den ändå saknas, se
    test_format_closed_message_handles_missing_forecast_record_gracefully)."""
    pnl = compute_pnl(position).quantize(Decimal("0.01"))
    hold_hours = (position.closed_at - position.opened_at).total_seconds() / 3600
    lines = [
        f"🔒 CLOSED — {position.instrument} {position.direction}",
        f"Entry: {position.simulated_fill_entry}  Exit: {position.simulated_fill_exit}",
        f"PnL: {pnl}",
        f"Fees: {position.fees}  Funding: {position.funding}",
        f"Hold time: {hold_hours:.1f}h",
        f"Exit reason: {position.exit_reason}",
    ]
    if forecast is not None:
        dominant_scenario = max(
            forecast.scenario_probabilities, key=forecast.scenario_probabilities.get
        )
        actual_direction = "vinst" if pnl > 0 else "förlust"
        lines.append(
            f"Forecast vs actual: predicted {dominant_scenario}, outcome {actual_direction}"
        )
    return "\n".join(lines)


def format_no_trade_message(candidate: Candidate, reasons: list[str]) -> str:
    """Fas 6 `decisions`-notisnivå (Beslut 6, 2026-08-29): "relevant"
    NO_TRADE - samtliga sju assessments var status='ok' och QA godkände,
    men den DETERMINISTISKA Risk/Signal Gate blockerade ändå (t.ex.
    max_concurrent_positions nått). `reasons` kommer direkt från redan
    persisterade `gate_decisions.reasons` - ingen egen tolkning här."""
    evidence = candidate.evidence_record
    return "\n".join(
        [
            f"⏸ NO_TRADE — {candidate.instrument}",
            f"Candidate score: {evidence.candidate_score:.2f}",
            f"Evidence: {', '.join(evidence.trigger_reasons)}",
            f"Gate blocked: {'; '.join(reasons)}",
            f"Time: {candidate.updated_at.isoformat()}",
        ]
    )


def _format_pnl_value(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _format_optional_pnl_value(value: Decimal | None) -> str:
    return "n/a" if value is None else _format_pnl_value(value)


def _format_optional_win_rate(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def format_daily_report_message(
    report_date: date,
    instruments_scanned: int,
    candidates_created: int,
    ai_analyses: int,
    confirmed: int,
    no_trade: int,
    rejected: int,
    open_positions: int,
    system_errors: int,
    cumulative_pnl: Decimal,
    win_rate: Decimal | None,
    expectancy: Decimal | None,
    drawdown: Decimal | None,
) -> str:
    """Fas 6/9 daily report (SPEC §12). Operativa räknetal plus de fyra
    performance-mått §12 kräver (cumulative PnL, win rate, expectancy,
    drawdown) - beräknade av anroparen via performance/metrics.py
    (2026-08-31 beslut: ingen duplicerad beräkningslogik här, samma
    princip som candidate/position redan följer i denna fil).
    win_rate/expectancy/drawdown är None för en tom handelshistorik
    (odefinierat, se metrics.py) och visas då som "n/a" - aldrig 0, som
    skulle påstå en känd nollprestanda. cumulative_pnl är däremot alltid
    ett giltigt tal (0 för tom historik är ett faktiskt värde, inte en
    gap-markering, se performance/metrics.py::compute_cumulative_pnl)."""
    return "\n".join(
        [
            f"📊 Daily report — {report_date.isoformat()}",
            f"Instruments scanned: {instruments_scanned}",
            f"Candidates created: {candidates_created}",
            f"AI analyses: {ai_analyses}",
            f"Confirmed: {confirmed}",
            f"No trade: {no_trade}",
            f"Rejected: {rejected}",
            f"Open positions: {open_positions}",
            f"System errors: {system_errors}",
            f"Cumulative PnL: {_format_pnl_value(cumulative_pnl)}",
            f"Win rate: {_format_optional_win_rate(win_rate)}",
            f"Expectancy: {_format_optional_pnl_value(expectancy)}",
            f"Max drawdown: {_format_optional_pnl_value(drawdown)}",
        ]
    )


def format_debug_error_message(run: dict) -> str:
    """Fas 6 `debug`-notisnivå (Beslut 7): en `runs`-rad med status='error'.
    Tar redan hämtad rad-data (dict från Repository.
    find_error_runs_pending_notification()) - ingen egen DB-fråga.

    `redact_error_list()` här är ett ANDRA skyddslager (code review-fynd
    2026-08-29): Repository.complete_run() redigerar redan errors innan
    persistering, men denna funktion redigerar ändå igen defensivt - en
    redan existerande, historisk rad (skapad innan complete_run()s egen
    fix fanns) ska aldrig kunna visa en oredigerad secret bara för att den
    redan låg oredigerad i databasen."""
    raw_errors = json.loads(run["errors"]) if run["errors"] else []
    safe_errors = redact_error_list(raw_errors)
    return "\n".join(
        [
            f"🐛 DEBUG error — run {run['run_id']} ({run['run_type']})",
            f"Started at: {run['started_at']}",
            f"Errors: {'; '.join(safe_errors) if safe_errors else 'none'}",
        ]
    )


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
    ):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def send(self, text: str) -> None:
        url = f"{_TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"

        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(_SAFE_TO_RETRY_EXCEPTIONS),
            reraise=True,
        )
        def _do() -> None:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(url, json={"chat_id": self._chat_id, "text": text})
                response.raise_for_status()

        try:
            _do()
        except httpx.HTTPError as exc:
            # ALDRIG str(exc)/exc.request/exc.response här - httpx bäddar in
            # hela request-URL:en (med boten-token i path:en) i sin egen
            # felmeddelande-sträng. Bara typ + statuskod, om tillgänglig.
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status_code}" if status_code is not None else type(exc).__name__
            raise TelegramSendError(f"Telegram sendMessage misslyckades: {detail}") from exc
