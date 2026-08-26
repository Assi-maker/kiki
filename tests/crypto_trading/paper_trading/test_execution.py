from decimal import Decimal

from crypto_trading.paper_trading.execution import (
    FILL_MODEL_VERSION,
    compute_fees,
    compute_fill_price,
    compute_funding,
)


def test_compute_fill_price_long_entry_is_worse_than_reference():
    # spread+slippage = 0.001 (0.0005+0.0005). LONG entry betalar MER.
    price = compute_fill_price(
        Decimal("50000"), "LONG", Decimal("0.0005"), Decimal("0.0005"), "entry"
    )
    assert price == Decimal("50000") * Decimal("1.001")


def test_compute_fill_price_long_exit_is_worse_than_reference():
    # LONG exit (säljer för att stänga) får MINDRE.
    price = compute_fill_price(
        Decimal("50000"), "LONG", Decimal("0.0005"), Decimal("0.0005"), "exit"
    )
    assert price == Decimal("50000") * Decimal("0.999")


def test_compute_fill_price_short_entry_is_worse_than_reference():
    # SHORT entry (säljer för att öppna) får MINDRE - motsatt LONG.
    price = compute_fill_price(
        Decimal("50000"), "SHORT", Decimal("0.0005"), Decimal("0.0005"), "entry"
    )
    assert price == Decimal("50000") * Decimal("0.999")


def test_compute_fill_price_short_exit_is_worse_than_reference():
    # SHORT exit (köper för att stänga) betalar MER - motsatt LONG.
    price = compute_fill_price(
        Decimal("50000"), "SHORT", Decimal("0.0005"), Decimal("0.0005"), "exit"
    )
    assert price == Decimal("50000") * Decimal("1.001")


def test_compute_fees_matches_hand_calculation():
    # Fees räknas på notional (size), inte fill_price*size - size är redan
    # ett USDT-notional-belopp (position_sizing.py).
    fees = compute_fees(size=Decimal("5000"), fee_pct=Decimal("0.0004"))
    assert fees == Decimal("5000") * Decimal("0.0004")
    assert fees == Decimal("2.0000")


def test_compute_funding_matches_hand_calculation():
    # 16h hold = 2 st hela 8h-funding-perioder.
    funding = compute_funding(
        size=Decimal("5000"), funding_rate=Decimal("0.0001"), hold_hours=Decimal("16")
    )
    assert funding == Decimal("5000") * Decimal("0.0001") * 2


def test_compute_funding_only_counts_whole_periods():
    # 20h hold = 2 hela perioder (16h), inte 2.5 - BingX-funding debiteras vid
    # fasta 8h-tidpunkter, inte prorata.
    funding = compute_funding(
        size=Decimal("5000"), funding_rate=Decimal("0.0001"), hold_hours=Decimal("20")
    )
    assert funding == Decimal("5000") * Decimal("0.0001") * 2


def test_compute_funding_is_zero_for_hold_under_one_period():
    funding = compute_funding(
        size=Decimal("5000"), funding_rate=Decimal("0.0001"), hold_hours=Decimal("7")
    )
    assert funding == Decimal("0")


def test_theoretical_and_simulated_fill_are_never_equal_when_spread_or_slippage_nonzero():
    """AC4: entry-priset (teoretiskt) och fill-priset (simulerat) är alltid
    explicit separata värden när spread/slippage är nollskild."""
    theoretical_entry = Decimal("50000")
    simulated_fill_entry = compute_fill_price(
        theoretical_entry, "LONG", Decimal("0.0005"), Decimal("0.0005"), "entry"
    )
    assert simulated_fill_entry != theoretical_entry


def test_fill_model_version_is_a_stable_string_constant():
    assert isinstance(FILL_MODEL_VERSION, str)
    assert FILL_MODEL_VERSION == "v1"
