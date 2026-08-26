from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.paper_trading.monitoring import check_exit_trigger
from crypto_trading.schemas.trade import Position

_OPENED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _position(stop_loss="49000", target="52000") -> Position:
    return Position(
        position_id="pos-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss=stop_loss,
        target=target,
        size="5000",
        fill_model_version="v1",
        opened_at=_OPENED_AT,
    )


def test_no_trigger_when_price_stays_within_range():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49500"),
        candle_high=Decimal("50500"),
        current_price=Decimal("50000"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result is None


def test_stop_loss_triggers_at_exact_touch():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49000"),
        candle_high=Decimal("50100"),
        current_price=Decimal("49500"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("stop_loss", Decimal("49000"))


def test_target_triggers_at_exact_touch():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49900"),
        candle_high=Decimal("52000"),
        current_price=Decimal("51500"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("target", Decimal("52000"))


def test_time_limit_triggers_after_max_hold_hours():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49900"),
        candle_high=Decimal("50100"),
        current_price=Decimal("50050"),
        now=_OPENED_AT + timedelta(hours=25),
        max_position_hold_hours=24,
    )
    assert result == ("time_limit", Decimal("50050"))  # current_price är referenspriset


def test_no_time_limit_trigger_before_max_hold_hours():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49900"),
        candle_high=Decimal("50100"),
        current_price=Decimal("50050"),
        now=_OPENED_AT + timedelta(hours=23),
        max_position_hold_hours=24,
    )
    assert result is None


def test_gap_down_through_stop_loss_fills_at_candle_low_not_stop_level():
    """AC5, gap ned: priset gappar långt under stop_loss (49000) mellan två
    övervakningstillfällen - fill-priset ska vara candle_low (49500... nej,
    lägre än stop), aldrig den exakta stop-nivån."""
    result = check_exit_trigger(
        _position(stop_loss="49000"),
        candle_low=Decimal("47500"),  # gappade långt under stop
        candle_high=Decimal("49800"),
        current_price=Decimal("47600"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("stop_loss", Decimal("47500"))
    exit_reason, trigger_price = result
    assert trigger_price != Decimal("49000")  # aldrig exakt SL-nivån vid gap
    assert trigger_price < Decimal("49000")  # strikt sämre än stop, konservativt


def test_gap_up_through_target_fills_at_target_not_candle_high():
    """AC5, gap upp: priset gappar långt över target (52000) mellan två
    övervakningstillfällen - fill-priset ska vara target, aldrig det
    gynnsamma extremvärdet (candle_high)."""
    result = check_exit_trigger(
        _position(target="52000"),
        candle_low=Decimal("51800"),
        candle_high=Decimal("54000"),  # gappade långt över target
        current_price=Decimal("53900"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("target", Decimal("52000"))
    exit_reason, trigger_price = result
    assert trigger_price != Decimal("54000")  # aldrig det gynnsamma extremvärdet
    assert trigger_price == Decimal("52000")  # konservativt: aldrig bättre än target


def test_fill_model_version_is_available_for_the_resulting_position():
    """Påminnelse: fill_model_version sätts på Position-objektet av
    position_opening.py/position_closing.py (Task 7/8) - check_exit_trigger
    själv rör inte Position-persistens, bara beslutet."""
    from crypto_trading.paper_trading.execution import FILL_MODEL_VERSION

    assert FILL_MODEL_VERSION == "v1"


def test_stop_loss_checked_before_time_limit_when_both_true():
    """Deterministisk prioritetsordning: SL/TP kollas alltid före tidsgräns."""
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("48000"),  # gappar under stop
        candle_high=Decimal("50100"),
        current_price=Decimal("48500"),
        now=_OPENED_AT + timedelta(hours=25),  # också över tidsgränsen
        max_position_hold_hours=24,
    )
    exit_reason, _trigger_price = result
    assert exit_reason == "stop_loss"
