from datetime import UTC, datetime

import pytest
import respx
from httpx import Response

from crypto_trading.notify.telegram import (
    TelegramNotifier,
    TelegramSendError,
    format_closed_message,
    format_confirmed_message,
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
    respx.post("https://api.telegram.org/botFAKE_TOKEN/sendMessage").mock(
        return_value=Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    notifier = TelegramNotifier(bot_token="FAKE_TOKEN", chat_id="12345")

    with pytest.raises(TelegramSendError) as exc_info:
        notifier.send("test message")

    assert "FAKE_TOKEN" not in str(exc_info.value)
    assert "401" in str(exc_info.value)
