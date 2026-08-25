from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.trade import Position


def test_position_theoretical_and_simulated_fill_are_separate_decimal_fields():
    position = Position(
        position_id="pos-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000.00"),
        simulated_fill_entry=Decimal("50005.25"),
        stop_loss=Decimal("49000.00"),
        target=Decimal("53000.00"),
        size=Decimal("0.1"),
        fill_model_version="v1",
        opened_at=datetime.now(UTC),
    )
    assert isinstance(position.theoretical_entry, Decimal)
    assert isinstance(position.simulated_fill_entry, Decimal)
    assert position.theoretical_entry != position.simulated_fill_entry
    assert position.closed_at is None


def test_position_decimal_precision_is_exact_not_float():
    position = Position(
        position_id="pos-2",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="SHORT",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("0.1"),
        simulated_fill_entry=Decimal("0.100001"),
        stop_loss=Decimal("0.11"),
        target=Decimal("0.09"),
        size=Decimal("123456789.123456789"),
        fill_model_version="v1",
        opened_at=datetime.now(UTC),
    )
    # 0.1 som float är inte exakt 0.1 - detta bevisar att Decimal-vägen aldrig passerar float
    assert position.theoretical_entry == Decimal("0.1")
    assert str(position.size) == "123456789.123456789"
