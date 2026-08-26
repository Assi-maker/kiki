from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.schemas.market import Kline
from crypto_trading.screening.quant_screener import (
    build_momentum_breakout_evidence,
    build_price_volatility_evidence,
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
