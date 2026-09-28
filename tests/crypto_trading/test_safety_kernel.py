"""Deterministic Safety Kernel (P0, 2026-09-28).

The 2026-09-26..28 forensic: risk per LIVE trade ranged 2.8-20 % of equity
(fixed ~1000 USDT notional x an AI-chosen stop distance), up to 45 % of
equity was at risk at once, and one stop sat ~0.2-0.7 % from the 10x
liquidation price. The kernel sizes every LIVE entry so that its real
worst-case loss (stop distance + stop slippage + round-trip fees) fits a
per-trade, per-group, portfolio and notional budget, and refuses stops too
close to liquidation. It can only REDUCE or REJECT - never enlarge."""
from decimal import Decimal as D

import pytest

from crypto_trading.config.loader import SafetyKernelConfig, get_settings
from crypto_trading.safety_kernel import (
    Exposure,
    check_stop_move,
    estimated_liquidation_price,
    exposure_for_open_position,
    group_for,
    size_entry,
    worst_case_risk_usdt,
)

LIMITS = SafetyKernelConfig(
    max_risk_per_trade_pct=D("0.01"), max_risk_per_trade_usdt=D("10"),
    max_portfolio_risk_pct=D("0.03"), max_group_risk_pct=D("0.025"),
    max_total_notional_multiple=D("3"), entry_price_buffer_pct=D("0.003"),
    stop_slippage_buffer_pct=D("0.003"), round_trip_fee_pct=D("0.001"),
    maintenance_margin_rate=D("0.01"), min_liquidation_buffer_pct=D("0.02"),
    symbol_groups={"BTC-USDT": "crypto_major", "ETH-USDT": "crypto_major"},
    group_prefixes={"NCSK": "tokenized_equity", "NCCO": "commodity_fx", "NCFX": "commodity_fx"},
    default_group="crypto_alt",
)


def _size(**overrides):
    args = dict(
        symbol="ONDO-USDT", equity=D("420"), last_price=D("0.5888"), stop_loss=D("0.5580"),
        target=D("0.6200"), leverage=10, base_quantity=D("1698"), quantity_precision=0,
        min_notional=D("2"), open_exposures=[], limits=LIMITS,
    )
    args.update(overrides)
    return size_entry(**args)


def test_production_config_loads_with_the_documented_p0_values():
    safety = get_settings().safety
    assert safety.max_risk_per_trade_pct == D("0.01")
    assert safety.max_portfolio_risk_pct <= D("0.05")
    assert safety.min_liquidation_buffer_pct >= D("0.01")


def test_worst_case_risk_includes_stop_slippage_and_round_trip_fees():
    risk = worst_case_risk_usdt(D("100"), D("10"), D("9.5"), LIMITS)
    # 100 x (10 - 9.5 x 0.997) + 100 x 10 x 0.001 = 52.85 + 1.0
    assert risk == D("53.8500")


def test_the_real_ondo_trade_is_reduced_to_one_percent_of_equity():
    """ONDO 58f1a3 lost 53.26 USDT at ~1000 notional. At 420 equity the
    kernel allows at most 4.20 USDT worst-case risk."""
    decision = _size()
    assert decision.action == "REDUCE"
    assert decision.risk_usdt <= D("4.20")
    assert decision.quantity < D("1698")
    assert decision.quantity > 0
    assert "PER_TRADE_RISK_CAP" in decision.binding_limits


def test_a_trade_already_within_every_budget_is_approved_unchanged():
    decision = _size(base_quantity=D("100"))  # ~59 USDT notional, ~3.3 USDT risk
    assert decision.action == "APPROVE"
    assert decision.quantity == D("100")


def test_the_absolute_usdt_cap_binds_on_a_large_account():
    decision = _size(equity=D("100000"), base_quantity=D("100000"))
    assert decision.risk_usdt <= D("10")
    assert "PER_TRADE_RISK_CAP" in decision.binding_limits


def test_missing_or_non_positive_equity_rejects():
    for equity in (None, D("0"), D("-5")):
        decision = _size(equity=equity)
        assert decision.action == "REJECT"
        assert decision.reasons == ["EQUITY_UNKNOWN"]
        assert decision.quantity == 0


def test_stop_at_or_above_the_entry_price_rejects():
    decision = _size(last_price=D("0.5580"))
    assert decision.action == "REJECT"
    assert "STOP_NOT_BELOW_ENTRY" in decision.reasons


def test_target_already_reached_rejects():
    decision = _size(last_price=D("0.6300"))
    assert decision.action == "REJECT"
    assert "TARGET_NOT_ABOVE_ENTRY" in decision.reasons


def test_liquidation_price_is_estimated_conservatively():
    # isolated long: entry x (1 - 1/L + mmr)
    assert estimated_liquidation_price(D("100"), 10, D("0.01")) == D("91.00")


def test_a_stop_too_close_to_liquidation_rejects_the_real_soon_case():
    """SOON 290f60: entry 0.2924, SL 0.266 (-9.03 %) at 10x."""
    decision = _size(symbol="SOON-USDT", last_price=D("0.2924"), stop_loss=D("0.266"),
                     target=D("0.315"), base_quantity=D("3486"))
    assert decision.action == "REJECT"
    assert "LIQUIDATION_TOO_CLOSE" in decision.reasons
    assert decision.liquidation_price is not None


def test_portfolio_ceiling_limits_the_sum_of_open_risk():
    open_ = [Exposure("A-USDT", "crypto_alt", D("4.2"), D("100")),
             Exposure("B-USDT", "crypto_alt", D("4.2"), D("100")),
             Exposure("BTC-USDT", "crypto_major", D("4.0"), D("100"))]
    decision = _size(open_exposures=open_)
    # 3 % of 420 = 12.6; 12.4 already open -> only 0.2 USDT headroom
    assert decision.action in ("REJECT", "REDUCE")
    assert decision.portfolio_risk_after <= D("12.6")
    if decision.action == "REDUCE":
        assert "PORTFOLIO_RISK_CAP" in decision.binding_limits


def test_a_full_portfolio_rejects_a_new_entry():
    open_ = [Exposure(f"S{i}-USDT", "crypto_alt", D("4.2"), D("100")) for i in range(3)]
    decision = _size(open_exposures=open_)
    assert decision.action == "REJECT"
    assert "PORTFOLIO_RISK_CAP" in decision.reasons


def test_group_ceiling_binds_before_the_portfolio_ceiling():
    open_ = [Exposure("A-USDT", "crypto_alt", D("4.2"), D("100")),
             Exposure("B-USDT", "crypto_alt", D("4.2"), D("100"))]
    decision = _size(open_exposures=open_)
    # group cap 2.5 % of 420 = 10.5 -> 2.1 headroom < portfolio headroom 4.2
    assert decision.risk_usdt <= D("2.1") + D("0.0001")
    assert "GROUP_RISK_CAP" in decision.binding_limits


def test_total_notional_ceiling():
    open_ = [Exposure("A-USDT", "crypto_major", D("0.1"), D("1250"))]
    decision = _size(open_exposures=open_)
    # 3 x 420 = 1260 notional allowed; 1250 open -> ~10 USDT notional left
    assert decision.quantity * D("0.5888") * D("1.003") <= D("10") + D("0.01")


def test_below_exchange_minimum_after_reduction_rejects():
    decision = _size(min_notional=D("500"))
    assert decision.action == "REJECT"
    assert "BELOW_EXCHANGE_MINIMUM_AFTER_RISK_SIZING" in decision.reasons


def test_the_kernel_never_enlarges_a_position():
    decision = _size(equity=D("10000000"), base_quantity=D("5"))
    assert decision.quantity <= D("5")


def test_quantity_respects_the_exchange_precision():
    decision = _size(quantity_precision=2, base_quantity=D("1698.00"))
    assert decision.quantity == decision.quantity.quantize(D("0.01"))


def test_groups():
    assert group_for("BTC-USDT", LIMITS) == "crypto_major"
    assert group_for("NCSKCRCL2USD-USDT", LIMITS) == "tokenized_equity"
    assert group_for("2Z-USDT", LIMITS) == "crypto_alt"


def test_open_exposure_uses_the_current_stop_after_profit_protection():
    before = exposure_for_open_position("X-USDT", D("100"), D("10"), D("9.5"), LIMITS)
    after_be = exposure_for_open_position("X-USDT", D("100"), D("10"), D("10"), LIMITS)
    assert before.risk_usdt > after_be.risk_usdt
    assert after_be.risk_usdt == D("100") * D("10") * D("0.003") + D("1.000")
    assert after_be.notional_usdt == D("1000")


# --- stop moves (PP / Guardian Authority) ---

def test_tightening_a_stop_is_allowed():
    assert check_stop_move(D("10"), D("9.5"), D("10"), 10, LIMITS) == []


def test_loosening_a_stop_is_forbidden():
    assert check_stop_move(D("10"), D("9.5"), D("9.4"), 10, LIMITS) == ["STOP_LOOSENING_FORBIDDEN"]


def test_moving_a_stop_next_to_liquidation_is_forbidden():
    # even a "tightening" from a (legacy) stop beyond the guard is refused if
    # the new stop is itself still too close to liquidation
    assert "LIQUIDATION_TOO_CLOSE" in check_stop_move(D("10"), D("9.0"), D("9.1"), 10, LIMITS)


@pytest.mark.parametrize("bad", [None, D("0"), D("-1")])
def test_an_unparseable_new_stop_is_refused(bad):
    assert check_stop_move(D("10"), D("9.5"), bad, 10, LIMITS) == ["INVALID_STOP"]
