from decimal import Decimal

from crypto_trading.paper_trading.live_profit_protection import unrealized_profit_pct


def test_unrealized_profit_pct_positive_when_mark_above_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("101")) == Decimal("0.01")


def test_unrealized_profit_pct_negative_when_mark_below_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("98")) == Decimal("-0.02")


def test_unrealized_profit_pct_zero_at_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("100")) == Decimal("0")
