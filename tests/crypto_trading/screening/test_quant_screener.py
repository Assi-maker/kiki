from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.schemas.evidence import CandidateEvidenceRecord
from crypto_trading.schemas.market import FundingRate, Kline
from crypto_trading.screening.quant_screener import (
    build_funding_oi_evidence,
    build_momentum_breakout_evidence,
    build_price_volatility_evidence,
    build_volume_evidence,
    evaluate_candidate,
)

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _kline(close: str, offset_hours: int, high=None, low=None, volume="100") -> Kline:
    close_dec = Decimal(close)
    return Kline(
        instrument="BTCUSDT",
        interval="1h",
        open=close_dec,
        high=Decimal(high) if high else close_dec,
        low=Decimal(low) if low else close_dec,
        close=close_dec,
        volume=Decimal(volume),
        observed_at=_NOW - timedelta(hours=offset_hours),
    )


def _flat_klines(n: int, price: str = "100") -> list[Kline]:
    return [_kline(price, offset_hours=n - i) for i in range(n)]


def test_price_volatility_triggers_when_change_exceeds_threshold():
    klines = _flat_klines(21, price="100")
    klines.append(_kline("110", offset_hours=0))  # +10% senaste steget
    evidence = build_price_volatility_evidence(
        klines, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "pct_change"
    assert evidence.value == pytest.approx(10.0)
    assert evidence.threshold == 2.0


def test_price_volatility_does_not_trigger_on_flat_prices():
    klines = _flat_klines(22, price="100")
    evidence = build_price_volatility_evidence(
        klines, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is False
    assert evidence.value == 0.0


def test_price_volatility_ignores_klines_after_evaluated_at():
    """SPEC §8.4: ingen framtida data får läcka in i beslutet."""
    klines = _flat_klines(21, price="100")
    future_kline = Kline(
        instrument="BTCUSDT",
        interval="1h",
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=Decimal("1"),
        observed_at=_NOW + timedelta(hours=1),
    )
    with_future = [*klines, future_kline]
    without_future = klines

    result_with = build_price_volatility_evidence(
        with_future, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    result_without = build_price_volatility_evidence(
        without_future, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    assert result_with == result_without


def test_momentum_breakout_triggers_on_high_rsi():
    # monotont stigande closes -> RSI mot 100 (inga losses i fönstret)
    klines = [_kline(str(100 + i), offset_hours=15 - i) for i in range(15)]
    evidence = build_momentum_breakout_evidence(
        klines, rsi_period=14, overbought_threshold=Decimal("70"), evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "rsi"
    assert evidence.value > 70.0
    assert evidence.baseline == 50.0


def test_momentum_breakout_does_not_trigger_on_flat_prices():
    klines = _flat_klines(15, price="100")
    evidence = build_momentum_breakout_evidence(
        klines, rsi_period=14, overbought_threshold=Decimal("70"), evaluated_at=_NOW
    )
    assert evidence.triggered is False


def _funding(rate: str, offset_hours: int) -> FundingRate:
    return FundingRate(
        instrument="BTCUSDT",
        funding_rate=Decimal(rate),
        mark_price=Decimal("50000"),
        observed_at=_NOW - timedelta(hours=offset_hours),
    )


def test_volume_evidence_triggers_on_high_zscore():
    klines = _flat_klines(20, price="100")
    spike = _kline("100", offset_hours=0, volume="10000")
    evidence = build_volume_evidence(
        [*klines, spike], zscore_threshold=Decimal("2.5"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "volume_zscore"


def test_volume_evidence_does_not_trigger_on_stable_volume():
    klines = _flat_klines(21, price="100")  # samtliga volume="100" (se _kline-default)
    evidence = build_volume_evidence(
        klines, zscore_threshold=Decimal("2.5"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is False


def test_funding_oi_evidence_triggers_on_high_abs_funding_rate():
    history = [_funding("0.001", offset_hours=8 * i) for i in range(1, 6)]
    latest = _funding("0.08", offset_hours=0)
    evidence = build_funding_oi_evidence(
        [*history, latest], threshold_pct=Decimal("0.05"), evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "funding_rate_pct"


def test_funding_oi_evidence_does_not_trigger_on_low_funding_rate():
    history = [_funding("0.0001", offset_hours=8 * i) for i in range(0, 5)]
    evidence = build_funding_oi_evidence(history, threshold_pct=Decimal("0.05"), evaluated_at=_NOW)
    assert evidence.triggered is False


def test_evaluate_candidate_never_exposes_a_direction_field():
    """AC1: screenern uttalar sig aldrig om riktning."""
    fields = set(CandidateEvidenceRecord.model_fields.keys())
    forbidden = {"direction", "side", "signal", "buy", "sell", "long", "short"}
    assert not (fields & forbidden)
    for sub in (
        CandidateEvidenceRecord.model_fields["price_volatility_evidence"].annotation,
        CandidateEvidenceRecord.model_fields["momentum_breakout_evidence"].annotation,
        CandidateEvidenceRecord.model_fields["volume_evidence"].annotation,
        CandidateEvidenceRecord.model_fields["funding_oi_evidence"].annotation,
    ):
        assert not (set(sub.model_fields.keys()) & forbidden)


def test_evaluate_candidate_is_deterministic():
    """AC2: samma indata given två gånger ger identisk candidate_score och
    identiska trigger_reasons."""
    klines = _flat_klines(21, price="100")
    klines.append(_kline("110", offset_hours=0))
    funding = [_funding("0.001", offset_hours=8 * i) for i in range(1, 6)]

    kwargs = dict(
        instrument="BTCUSDT",
        timeframes=["1h"],
        klines=klines,
        funding_rates=funding,
        data_quality_status="ok",
        evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"),
        lookback=20,
        rsi_period=14,
        rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"),
        funding_rate_threshold_pct=Decimal("0.05"),
    )
    first = evaluate_candidate(**kwargs)
    second = evaluate_candidate(**kwargs)

    assert first.candidate_score == second.candidate_score
    assert first.trigger_reasons == second.trigger_reasons
    assert first == second


def test_evaluate_candidate_sets_worth_deeper_analysis_when_a_signal_triggers():
    klines = _flat_klines(21, price="100")
    klines.append(_kline("110", offset_hours=0))
    funding = [_funding("0.001", offset_hours=8 * i) for i in range(1, 6)]
    record = evaluate_candidate(
        instrument="BTCUSDT",
        timeframes=["1h"],
        klines=klines,
        funding_rates=funding,
        data_quality_status="ok",
        evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"),
        lookback=20,
        rsi_period=14,
        rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"),
        funding_rate_threshold_pct=Decimal("0.05"),
    )
    assert record.outcome == "worth_deeper_analysis"
    assert "price_volatility" in record.trigger_reasons
    assert record.data_quality_status == "ok"


def test_evaluate_candidate_sets_not_a_candidate_when_nothing_triggers():
    klines = _flat_klines(21, price="100")
    funding = [_funding("0.0001", offset_hours=8 * i) for i in range(0, 5)]
    record = evaluate_candidate(
        instrument="BTCUSDT",
        timeframes=["1h"],
        klines=klines,
        funding_rates=funding,
        data_quality_status="ok",
        evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"),
        lookback=20,
        rsi_period=14,
        rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"),
        funding_rate_threshold_pct=Decimal("0.05"),
    )
    assert record.outcome == "not_a_candidate"
    assert record.trigger_reasons == []


def test_evaluate_candidate_short_circuits_on_invalid_data_quality():
    record = evaluate_candidate(
        instrument="BTCUSDT",
        timeframes=["1h"],
        klines=[],
        funding_rates=[],
        data_quality_status="invalid",
        evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"),
        lookback=20,
        rsi_period=14,
        rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"),
        funding_rate_threshold_pct=Decimal("0.05"),
    )
    assert record.data_quality_status == "invalid"
    assert record.outcome == "not_a_candidate"
    assert record.trigger_reasons == []
    assert record.candidate_score == 0.0
