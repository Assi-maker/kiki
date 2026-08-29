from __future__ import annotations

from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.schemas.trade import Position

_TELEGRAM_API_BASE = "https://api.telegram.org"


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
            retry=retry_if_exception_type(httpx.HTTPError),
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
