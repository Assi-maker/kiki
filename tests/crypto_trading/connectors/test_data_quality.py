from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.connectors.data_quality import (
    check_completeness,
    check_kline_consistency,
    check_staleness,
    classify,
)
from crypto_trading.schemas.market import Kline


def _kline(close: str, high: str = "100", low: str = "1", volume: str = "10") -> Kline:
    return Kline(
        instrument="BTC-USDT",
        interval="1h",
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        observed_at=datetime.now(UTC),
    )


# --- completeness (§8.1 "ofullständig") ---


def test_completeness_ok_when_all_required_fields_present():
    raw = {"lastPrice": "1", "askPrice": "1", "bidPrice": "1"}
    assert check_completeness(raw, required_fields=["lastPrice", "askPrice", "bidPrice"]) == "ok"


def test_completeness_invalid_when_a_required_field_is_missing():
    raw = {"lastPrice": "1", "askPrice": "1"}
    assert (
        check_completeness(raw, required_fields=["lastPrice", "askPrice", "bidPrice"]) == "invalid"
    )


def test_completeness_invalid_when_required_field_is_none():
    raw = {"lastPrice": "1", "askPrice": None, "bidPrice": "1"}
    assert (
        check_completeness(raw, required_fields=["lastPrice", "askPrice", "bidPrice"]) == "invalid"
    )


def test_completeness_threshold_is_config_driven_not_hardcoded():
    """Ändra vilka fält som krävs via parametern (=config i praktiken) och
    bevisa att beteendet ändras i takt - inget hårdkodat i Python (AC4)."""
    raw = {"lastPrice": "1"}
    assert check_completeness(raw, required_fields=["lastPrice"]) == "ok"
    assert check_completeness(raw, required_fields=["lastPrice", "askPrice"]) == "invalid"


# --- staleness (§8.1 "stale") ---


def test_staleness_ok_within_max_age():
    observed_at = datetime.now(UTC) - timedelta(seconds=10)
    assert check_staleness(observed_at, datetime.now(UTC), max_age_seconds=30) == "ok"


def test_staleness_invalid_beyond_max_age():
    observed_at = datetime.now(UTC) - timedelta(seconds=61)
    assert check_staleness(observed_at, datetime.now(UTC), max_age_seconds=30) == "invalid"


def test_staleness_invalid_for_future_timestamp():
    observed_at = datetime.now(UTC) + timedelta(seconds=5)
    assert check_staleness(observed_at, datetime.now(UTC), max_age_seconds=30) == "invalid"


def test_staleness_threshold_is_config_driven_not_hardcoded():
    observed_at = datetime.now(UTC) - timedelta(seconds=45)
    now = datetime.now(UTC)
    assert check_staleness(observed_at, now, max_age_seconds=30) == "invalid"
    assert check_staleness(observed_at, now, max_age_seconds=3600) == "ok"


# --- consistency (§8.1 "inkonsekvent") ---


def test_kline_consistency_ok_for_sane_data():
    klines = [_kline("100"), _kline("101"), _kline("99")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "ok"


def test_kline_consistency_invalid_when_high_below_low():
    klines = [_kline(close="50", high="10", low="20")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "invalid"


def test_kline_consistency_invalid_for_negative_volume():
    klines = [_kline(close="50", volume="-1")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "invalid"


def test_kline_consistency_invalid_for_outlier_beyond_tolerance():
    klines = [_kline("100"), _kline("101"), _kline("99"), _kline("100000")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "invalid"


def test_kline_consistency_tolerance_is_config_driven_not_hardcoded():
    klines = [_kline("100"), _kline("100"), _kline("100"), _kline("140")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.2")) == "invalid"
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.9")) == "ok"


# --- classify (kombinerar + typnivå-garanti) ---


def test_classify_ok_when_all_ok():
    assert classify("ok", "ok", "ok") == "ok"


def test_classify_invalid_if_any_invalid():
    assert classify("ok", "invalid", "ok") == "invalid"


def test_classify_return_type_excludes_degraded():
    """Typnivå-garanti: Literal['ok','invalid'] gör 'degraded' omöjligt att
    returnera från Phase 1:s klassificering överhuvudtaget - all BingX-data
    är kritisk (SPEC §14), degraded blir bara möjligt i senare faser."""
    import typing

    from crypto_trading.connectors.data_quality import DataQualityResult

    assert typing.get_args(DataQualityResult) == ("ok", "invalid")
