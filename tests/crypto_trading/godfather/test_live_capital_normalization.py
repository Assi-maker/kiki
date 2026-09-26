"""LIVE capital level (2026-09-26: 10 -> 100 USDT margin, x10 leverage).

GODFATHER compares edges in R / %, and reports USDT at each LIVE
execution's OWN exchange size - a 10x larger position must never read as
a 10x better or worse edge, and the paper size must never stand in for the
LIVE size."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather.portfolio import live_capital_profile
from crypto_trading.godfather.risk_units import compute_r, live_usdt_outcome
from crypto_trading.schemas.trade import Position

_T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _position(size: str = "500") -> Position:
    return Position(
        position_id="p1", candidate_id="c1", instrument="BTC-USDT", direction="LONG",
        status="CLOSED", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("98"), target=Decimal("104"), size=Decimal(size),
        fill_model_version="v1", opened_at=_T0,
    )


def _live_row(margin: str, qty: str, exit_: str = "103") -> dict:
    notional = str(Decimal(margin) * 10)
    return {
        "margin_usdt": margin, "notional_usdt": notional, "leverage": "10",
        "entry_quantity": qty, "exchange_fill_entry": "100", "exchange_fill_exit": exit_,
        "realized_fees_usdt": None, "realized_funding_usdt": None,
        "claimed_at": _T0.isoformat(), "closed_at": (_T0 + timedelta(hours=1)).isoformat(),
    }


def _r(position: Position, row: dict):
    return compute_r(
        position, position.stop_loss, None,
        exit_price=Decimal(row["exchange_fill_exit"]),
        entry_price=Decimal(row["exchange_fill_entry"]), fee_pct=Decimal("0.0004"),
    )


def test_r_is_identical_for_a_100_and_a_1000_usdt_live_position():
    position = _position()
    small, large = _live_row("10", "1"), _live_row("100", "10")
    assert _r(position, small).r_net == _r(position, large).r_net
    assert _r(position, large).r_net == (Decimal("0.03") - Decimal("0.0004")) / Decimal("0.02")


def test_live_usdt_uses_the_executions_own_size_not_the_paper_size():
    position = _position(size="500")  # paper size must not leak into LIVE USDT
    row = _live_row("100", "10")
    out = live_usdt_outcome(row, _r(position, row), "LONG", Decimal("0.0004"))
    assert out["notional_usdt"] == Decimal("1000")
    assert out["margin_usdt"] == Decimal("100")
    assert out["gross_pnl_usdt"] == Decimal("30")
    assert out["planned_risk_usdt"] == Decimal("20")  # 1000 x 2% planned SL distance
    assert out["fees_usdt"] == Decimal("0.4")
    assert out["fees_source"] == "MODELLED"
    assert out["funding_source"] == "UNKNOWN"
    assert out["net_pnl_usdt"] == Decimal("29.6")
    # net USDT / planned risk USDT is exactly the size-free net R
    assert out["net_pnl_usdt"] / out["planned_risk_usdt"] == _r(position, row).r_net


def test_live_usdt_scales_with_size_while_r_does_not():
    position = _position()
    small_row, large_row = _live_row("10", "1", "97"), _live_row("100", "10", "97")
    small = live_usdt_outcome(small_row, _r(position, small_row), "LONG", Decimal("0.0004"))
    large = live_usdt_outcome(large_row, _r(position, large_row), "LONG", Decimal("0.0004"))
    assert large["net_pnl_usdt"] == small["net_pnl_usdt"] * 10
    assert large["planned_risk_usdt"] == small["planned_risk_usdt"] * 10
    assert _r(position, small_row).r_net == _r(position, large_row).r_net


def test_live_usdt_prefers_recorded_exchange_fees():
    row = _live_row("100", "10") | {"realized_fees_usdt": "1.1", "realized_funding_usdt": "-0.2"}
    out = live_usdt_outcome(row, None, "LONG", Decimal("0.0004"))
    assert out["fees_source"] == "EXCHANGE"
    assert out["net_pnl_usdt"] == Decimal("30") - Decimal("1.1") - Decimal("0.2")
    assert out["planned_risk_usdt"] is None  # no R, no invented planned risk


def test_live_usdt_is_none_for_a_paper_only_trade():
    assert live_usdt_outcome(None, None, "LONG", Decimal("0.0004")) is None


def test_live_capital_profile_keeps_tiers_apart_and_compares_them_in_r():
    position = _position()
    trades = []
    for margin, qty in (("10", "1"), ("100", "10")):
        row = _live_row(margin, qty)
        r = _r(position, row)
        trades.append((_T0, _T0 + timedelta(hours=1),
                       live_usdt_outcome(row, r, "LONG", Decimal("0.0004")), r.r_net))
    profile = live_capital_profile(trades)
    tiers = profile["by_margin_tier_usdt"]
    assert set(tiers) == {"10", "100"}
    assert Decimal(tiers["100"]["net_pnl_usdt"]) == Decimal(tiers["10"]["net_pnl_usdt"]) * 10
    assert tiers["100"]["expectancy_r"] == tiers["10"]["expectancy_r"]


def test_live_capital_profile_peak_is_four_positions_at_the_new_size():
    row = _live_row("100", "10")
    trades = [
        (_T0 + timedelta(minutes=i), _T0 + timedelta(hours=2),
         live_usdt_outcome(row, None, "LONG", Decimal("0.0004")), None)
        for i in range(4)
    ]
    profile = live_capital_profile(trades)
    assert Decimal(profile["peak_concurrent_notional_usdt"]) == Decimal("4000")
    assert Decimal(profile["peak_concurrent_margin_usdt"]) == Decimal("400")


def test_live_capital_profile_ignores_executions_that_never_filled():
    failed = {"margin_usdt": "100", "notional_usdt": "1000", "leverage": "10",
              "entry_quantity": None, "exchange_fill_entry": None, "exchange_fill_exit": None}
    filled = _live_row("100", "10")
    profile = live_capital_profile([
        (_T0, None, live_usdt_outcome(failed, None, "LONG", Decimal("0.0004")), None),
        (_T0, _T0 + timedelta(hours=1),
         live_usdt_outcome(filled, None, "LONG", Decimal("0.0004")), None),
    ])
    assert profile["by_margin_tier_usdt"]["100"]["trades"] == 1
    assert Decimal(profile["peak_concurrent_notional_usdt"]) == Decimal("1000")
