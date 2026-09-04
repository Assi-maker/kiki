from decimal import Decimal

from crypto_trading.paper_trading.position_sizing import compute_position_size


def test_position_size_matches_hand_calculation():
    # entry=50000, stop=49000 -> 2% stop-avstånd. capital=10000, risk=1% -> risk_amount=100.
    # size = 100 / 0.02 = 5000. max_total_exposure_pct=1.0 (100%) isolerar
    # risk-formeln från exponeringstaket - det testas separat nedan.
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("0"),
        max_total_exposure_pct=Decimal("1.0"),
    )
    assert size == Decimal("5000")


def test_position_size_capped_by_remaining_exposure():
    # max_exposure = 10000 * 0.25 = 2500. Redan 2000 använt -> bara 500 kvar.
    # Rå storlek (5000) klipps till 500.
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("2000"),
        max_total_exposure_pct=Decimal("0.25"),
    )
    assert size == Decimal("500")


def test_position_size_is_zero_when_exposure_already_full():
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("2500"),
        max_total_exposure_pct=Decimal("0.25"),
    )
    assert size == Decimal("0")


def test_position_size_is_zero_for_degenerate_zero_distance_stop():
    """Fail-closed: stop == entry ger odefinierat stop-avstånd, aldrig en gissad storlek."""
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("50000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("0"),
        max_total_exposure_pct=Decimal("0.25"),
    )
    assert size == Decimal("0")


def test_position_size_at_new_full_exposure_default_leaves_room_for_many_positions():
    """PAPER-kapacitet (2026-09-04): med max_total_exposure_pct höjt till
    1.00 (100%, config/risk_limits.yaml) ryms betydligt fler samtidiga
    icke-nollstora positioner innan exponeringspoolen är slut, jämfört med
    den gamla 0.25 (2500 USDT)-gränsen - se dess kommentar i
    config/risk_limits.yaml för den fulla räkningen."""
    # max_exposure = 10000 * 1.00 = 10000. Med 9500 redan använt av tidigare
    # positioner finns fortfarande 500 kvar (jämför: med gamla 0.25 hade
    # redan 2500 räckt för att helt tömma poolen).
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("9500"),
        max_total_exposure_pct=Decimal("1.00"),
    )
    assert size == Decimal("500")


def test_position_size_still_zero_when_new_full_exposure_pool_is_actually_exhausted():
    """`blocked_by_exposure` ska fortfarande kunna inträffa - bara när den
    NYA, högre poolen faktiskt är full, inte som en rutinmässig konsekvens
    av den gamla 2500 USDT-gränsen (explicit användarkrav)."""
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("10000"),
        max_total_exposure_pct=Decimal("1.00"),
    )
    assert size == Decimal("0")
