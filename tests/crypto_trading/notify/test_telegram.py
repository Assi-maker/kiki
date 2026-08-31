from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from httpx import Response

from crypto_trading.notify.telegram import (
    TelegramNotifier,
    TelegramSendError,
    format_closed_message,
    format_confirmed_message,
    format_daily_report_message,
    format_debug_error_message,
    format_no_trade_message,
)
from crypto_trading.schemas.assessments import (
    BullThesisAssessment,
    ForecastAssessment,
    RiskAssessment,
)
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.schemas.trade import Position

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _evidence() -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.82,
        trigger_reasons=["price_volatility", "volume"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _confirmed_candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        idempotency_key="key-1",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CONFIRMED",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
        bull_thesis=BullThesisAssessment(
            agent_name="crypto-bull-thesis",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            hypothesis="Momentum breakout following a volume spike",
            catalyst="Volume z-score anomaly",
            setup="Breakout continuation",
        ),
        forecast=ForecastAssessment(
            agent_name="crypto-forecast-agent",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            scenario_probabilities={"bullish": 0.6, "neutral": 0.3, "bearish": 0.1},
            horizon="4h",
            forecast_version="v1",
        ),
        risk=RiskAssessment(
            agent_name="crypto-risk-agent",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            suggested_stop_loss="49000",
            suggested_target="52000",
            downside="d",
            liquidity_risk="l",
            model_risk="m",
            timing_risk="t",
        ),
    )


def _open_position() -> Position:
    return Position(
        position_id="cand-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_NOW,
    )


def _closed_position() -> Position:
    return Position(
        position_id="cand-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="CLOSED",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_NOW,
        theoretical_exit="52000",
        simulated_fill_exit="51980",
        exit_reason="target",
        fees="2",
        funding="1",
        closed_at=_NOW,
    )


def _forecast_record() -> ForecastRecord:
    return ForecastRecord(
        forecast_id="cand-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        forecast_timestamp=_NOW,
        horizon="4h",
        scenario_probabilities={"bullish": 0.6, "neutral": 0.3, "bearish": 0.1},
        forecast_version="v1",
        market_state_metadata={},
    )


def test_format_confirmed_message_includes_all_spec_fields():
    text = format_confirmed_message(_confirmed_candidate(), _open_position())

    assert "BTCUSDT" in text
    assert "LONG" in text
    assert "50025" in text  # entry (simulated fill, den faktiska handelspriset)
    assert "49000" in text  # stop-loss
    assert "52000" in text  # target
    assert "0.82" in text  # candidate score
    assert "price_volatility" in text  # viktigaste evidensen
    assert "volume" in text
    assert "Momentum breakout" in text  # AI-teamets slutsats (bull thesis hypothesis)
    assert "bullish" in text  # forecast-scenario
    assert "4h" in text  # forecast horizon
    assert "2026-08-29" in text  # timestamp


def test_format_confirmed_message_includes_risk_reward_ratio():
    text = format_confirmed_message(_confirmed_candidate(), _open_position())
    # reward = 52000-50025=1975, risk = 50025-49000=1025, ratio ~1.93
    assert "1.9" in text or "R:R" in text.upper() or "risk/reward" in text.lower()


def test_format_closed_message_includes_all_spec_fields():
    text = format_closed_message(_closed_position(), _forecast_record())

    assert "BTCUSDT" in text
    assert "LONG" in text
    assert "50025" in text  # entry
    assert "51980" in text  # exit
    assert "target" in text  # exit reason
    # PnL = (51980-50025)/50025 * 5000 - fees(2) - funding(1) = 192.40...
    assert "192.4" in text
    assert "fees" in text.lower() or "avgift" in text.lower()
    assert "funding" in text.lower()
    assert "4h" in text or "hold" in text.lower() or "hål" in text.lower()  # hold time present


def test_format_closed_message_includes_forecast_vs_actual_outcome():
    text = format_closed_message(_closed_position(), _forecast_record())
    assert "bullish" in text.lower()


def test_format_closed_message_handles_missing_forecast_record_gracefully():
    """Fail-safe: saknad forecast-record (bör aldrig hända för en CONFIRMED-
    ledd position, men om det gör det ska formateringen aldrig krascha)."""
    text = format_closed_message(_closed_position(), None)
    assert "BTCUSDT" in text


def _no_trade_candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-no-trade",
        idempotency_key="key-no-trade",
        instrument="ETHUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="NO_TRADE",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_format_no_trade_message_includes_instrument_score_evidence_and_reasons():
    text = format_no_trade_message(_no_trade_candidate(), ["max_concurrent_positions reached: 5/5"])
    assert "ETHUSDT" in text
    assert "0.82" in text  # candidate score
    assert "price_volatility" in text  # trigger_reasons
    assert "max_concurrent_positions reached: 5/5" in text


def test_format_daily_report_message_includes_all_minimal_counts():
    from datetime import date

    text = format_daily_report_message(
        report_date=date(2026, 8, 29),
        instruments_scanned=1119,
        candidates_created=7,
        ai_analyses=21,
        confirmed=2,
        no_trade=3,
        rejected=1,
        open_positions=4,
        system_errors=0,
        cumulative_pnl=Decimal("192.40"),
        win_rate=Decimal("0.67"),
        expectancy=Decimal("48.10"),
        drawdown=Decimal("15.20"),
    )
    assert "2026-08-29" in text
    assert "1119" in text
    assert "7" in text
    assert "21" in text
    assert "2" in text
    assert "3" in text
    assert "4" in text
    # system_errors=0 ska visas explicit, inte utelämnas
    assert "0" in text


def test_format_daily_report_message_includes_performance_metrics_spec_section_12():
    """SPEC §12 (2026-08-31 beslut): daily report ska innehålla EXAKT dessa
    fyra performance-mått, beräknade av crypto_trading/performance/metrics.py
    - inte profit factor/trade count (de hör till dashboardens §13, inte
    Telegrams §12)."""
    from datetime import date

    text = format_daily_report_message(
        report_date=date(2026, 8, 29),
        instruments_scanned=1119,
        candidates_created=7,
        ai_analyses=21,
        confirmed=2,
        no_trade=3,
        rejected=1,
        open_positions=4,
        system_errors=0,
        cumulative_pnl=Decimal("192.40"),
        win_rate=Decimal("0.67"),
        expectancy=Decimal("48.10"),
        drawdown=Decimal("15.20"),
    )
    assert "192.40" in text
    assert "67%" in text
    assert "48.10" in text
    assert "15.20" in text
    assert "profit factor" not in text.lower()


def test_format_daily_report_message_shows_n_a_for_undefined_metrics_on_empty_history():
    """Tom handelshistorik: win_rate/expectancy/drawdown är odefinierade
    (metrics.py returnerar None), ska visas som "n/a" - ALDRIG 0, som skulle
    påstå att en känd nollprestanda existerar (2026-08-31 beslut).
    cumulative_pnl=0 är däremot ett giltigt, faktiskt värde (metrics.py:
    "en tom historik har verkligen noll kumulativ PnL") och ska visas som
    ett tal, inte "n/a"."""
    from datetime import date

    text = format_daily_report_message(
        report_date=date(2026, 8, 29),
        instruments_scanned=0,
        candidates_created=0,
        ai_analyses=0,
        confirmed=0,
        no_trade=0,
        rejected=0,
        open_positions=0,
        system_errors=0,
        cumulative_pnl=Decimal("0"),
        win_rate=None,
        expectancy=None,
        drawdown=None,
    )
    assert "0.00" in text  # cumulative_pnl, ett giltigt tal, inte n/a
    assert text.count("n/a") == 3  # win_rate, expectancy, drawdown


def test_format_debug_error_message_includes_run_details():
    run = {
        "run_id": "run-abc",
        "run_type": "discovery",
        "started_at": "2026-08-29T10:00:00+00:00",
        "errors": '["ConnectorUnavailableError: BingX otillg\\u00e4nglig"]',
    }
    text = format_debug_error_message(run)
    assert "run-abc" in text
    assert "discovery" in text
    assert "ConnectorUnavailableError" in text


def test_format_debug_error_message_redacts_secrets_as_defense_in_depth():
    """Andra skyddslagret (code review-fynd 2026-08-29): Repository.
    complete_run() redigerar redan errors innan persistering, men denna
    formatteringsfunktion redigerar ÄNDÅ igen defensivt - skyddar även mot
    en redan existerande, oredigerad historisk rad (skapad innan
    complete_run()s egen fix fanns) som annars skulle visas rått över
    Telegram på debug-nivå. FEJKAT, ofarligt secret-mönster, aldrig en
    riktig hemlighet."""
    run = {
        "run_id": "run-abc",
        "run_type": "discovery",
        "started_at": "2026-08-29T10:00:00+00:00",
        "errors": (
            '["request to https://api.telegram.org/bot123456789:'
            'FAKEBOTTOKENFAKEFAKEFAKE/sendMessage failed"]'
        ),
    }
    text = format_debug_error_message(run)
    assert "123456789:FAKEBOTTOKENFAKEFAKEFAKE" not in text
    assert "***REDACTED***" in text


def test_format_debug_error_message_handles_empty_errors_list():
    run = {
        "run_id": "run-abc",
        "run_type": "discovery",
        "started_at": "2026-08-29T10:00:00+00:00",
        "errors": "[]",
    }
    text = format_debug_error_message(run)
    assert "run-abc" in text


@respx.mock
def test_telegram_notifier_send_posts_to_correct_endpoint():
    route = respx.post("https://api.telegram.org/botFAKE_TOKEN/sendMessage").mock(
        return_value=Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier(bot_token="FAKE_TOKEN", chat_id="12345")
    notifier.send("test message")

    assert route.called
    request = route.calls.last.request
    assert b"test message" in request.content
    assert b"12345" in request.content


@respx.mock
def test_telegram_notifier_send_raises_sanitized_error_never_exposing_token():
    route = respx.post("https://api.telegram.org/botFAKE_TOKEN/sendMessage").mock(
        return_value=Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    notifier = TelegramNotifier(bot_token="FAKE_TOKEN", chat_id="12345")

    with pytest.raises(TelegramSendError) as exc_info:
        notifier.send("test message")

    assert "FAKE_TOKEN" not in str(exc_info.value)
    assert "401" in str(exc_info.value)
    # Duplicate-send fynd (code review 2026-08-29): en definitiv HTTP-
    # felstatus betyder att Telegram FAKTISKT svarade - att retrya den
    # automatiskt inom samma send()-anrop riskerar ingenting extra här
    # (401 kommer alltid vara 401), men bekräftar att vi INTE gör onödiga
    # extra försök för ett redan definitivt svar.
    assert route.call_count == 1


@respx.mock
def test_telegram_notifier_retries_on_connect_error_and_eventually_succeeds():
    """Ett anslutningsfel (ConnectError/ConnectTimeout) betyder att
    request:en ALDRIG nådde Telegram - säkert att retrya automatiskt,
    ingen risk för en dubblettsändning."""
    route = respx.post("https://api.telegram.org/botFAKE_TOKEN/sendMessage").mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            Response(200, json={"ok": True}),
        ]
    )
    notifier = TelegramNotifier(bot_token="FAKE_TOKEN", chat_id="12345")

    notifier.send("test message")  # ska inte kasta - andra försöket lyckas

    assert route.call_count == 2


@respx.mock
def test_telegram_notifier_does_not_retry_on_ambiguous_timeout_after_request_sent():
    """Duplicate-send-fyndet (code review 2026-08-29): sendMessage är en
    side-effecting POST utan idempotenskyckel. En ReadTimeout inträffar
    EFTER att requesten redan skickats - Telegram kan redan ha tagit emot
    och behandlat den. Att automatiskt retrya här (som tidigare, då ALLA
    httpx.HTTPError retryades) riskerar en dubblettnotis. Detta test
    bevisar att send() INTE gör fler försök för denna feltyp - bara ETT
    anrop, sedan ett rapporterat, sanerat fel."""
    route = respx.post("https://api.telegram.org/botFAKE_TOKEN/sendMessage").mock(
        side_effect=httpx.ReadTimeout("timed out waiting for response")
    )
    notifier = TelegramNotifier(bot_token="FAKE_TOKEN", chat_id="12345")

    with pytest.raises(TelegramSendError) as exc_info:
        notifier.send("test message")

    assert route.call_count == 1  # INGEN retry - request:en kan redan ha nått Telegram
    assert "FAKE_TOKEN" not in str(exc_info.value)


@respx.mock
def test_telegram_notifier_does_not_retry_on_5xx_status():
    """En 5xx är ett definitivt, mottaget svar (till skillnad från en
    timeout) - men klassas ändå som INTE säker att retrya automatiskt här,
    eftersom vissa serverfel kan inträffa efter att meddelandet redan
    levererats. Konservativt: bara connection-nivå-fel (som aldrig når
    servern) retryas automatiskt."""
    route = respx.post("https://api.telegram.org/botFAKE_TOKEN/sendMessage").mock(
        return_value=Response(500, json={"ok": False, "description": "Internal Server Error"})
    )
    notifier = TelegramNotifier(bot_token="FAKE_TOKEN", chat_id="12345")

    with pytest.raises(TelegramSendError):
        notifier.send("test message")

    assert route.call_count == 1
