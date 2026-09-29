"""Deterministic Safety Kernel (P0 2026-09-28; fixed sizing 2026-09-29).

The 2026-09-26..28 forensic: risk per LIVE trade ranged 2.8-20 % of equity,
up to 45 % of equity was at risk at once, and one stop sat ~0.2-0.7 % from
the 10x liquidation price.

User decision 2026-09-29: LIVE size is FIXED again - 100 USDT margin x 10 =
~1000 USDT notional - and is never reduced. The kernel APPROVES that full
size or REJECTS it: total worst-case risk of all open LIVE positions <= 10 %
of current equity, per correlated group <= 5 %, plus the liquidation guard.
It never enlarges, never shrinks."""
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
    max_portfolio_risk_pct=D("0.10"), max_group_risk_pct=D("0.05"),
    entry_price_buffer_pct=D("0.003"),
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


def test_production_config_loads_the_fixed_sizing_caps():
    safety = get_settings().safety
    assert safety.max_portfolio_risk_pct == D("0.10")
    # user decision 2026-09-29: correlated-group cap back to 5 % (was 10 % at 22ee411)
    assert safety.max_group_risk_pct == D("0.05")
    assert safety.max_group_risk_pct <= safety.max_portfolio_risk_pct
    assert safety.min_liquidation_buffer_pct >= D("0.01")
    # the per-trade % cap and the notional multiple are gone - they could only REDUCE
    for removed in ("max_risk_per_trade_pct", "max_risk_per_trade_usdt", "max_total_notional_multiple"):
        assert not hasattr(safety, removed)


def test_worst_case_risk_includes_stop_slippage_and_round_trip_fees():
    risk = worst_case_risk_usdt(D("100"), D("10"), D("9.5"), LIMITS)
    # 100 x (10 - 9.5 x 0.997) + 100 x 10 x 0.001 = 52.85 + 1.0
    assert risk == D("53.8500")


def _full(last=D("1.0"), stop=D("0.99"), **overrides):
    """A fixed-size entry: 1000 notional at `last`."""
    return _size(symbol="X-USDT", last_price=last, stop_loss=stop, target=last * D("1.05"),
                 base_quantity=(D(1000) / last).quantize(D("1")), **overrides)


def test_example_approved_full_size_position():
    """420 equity, stop 1 % below: worst-case risk ~17 USDT <= 21 (group 5 %)
    and <= 42 (portfolio 10 %) -> APPROVE at the full 1000 quantity."""
    decision = _full()
    assert decision.action == "APPROVE"
    assert decision.quantity == D("1000")
    assert D("16") < decision.risk_usdt < D("18")
    assert decision.binding_limits == []


def test_example_rejected_full_size_position_is_never_reduced():
    """The real ONDO trade (lost 53.26 at ~1000 notional): stop 5.2 % below,
    worst-case risk ~59 USDT > 21 group / 42 portfolio -> REJECT, quantity 0.
    Before 2026-09-29 this was REDUCED to ~4 USDT risk."""
    decision = _size()
    assert decision.action == "REJECT"
    assert decision.quantity == 0
    assert "PORTFOLIO_RISK_CAP" in decision.reasons and "GROUP_RISK_CAP" in decision.reasons


def test_the_kernel_never_returns_reduce():
    for last, stop in ((D("1"), D("0.999")), (D("1"), D("0.99")), (D("1"), D("0.97")), (D("1"), D("0.95"))):
        for equity in (D("50"), D("420"), D("5000")):
            decision = _full(last=last, stop=stop, equity=equity)
            assert decision.action in ("APPROVE", "REJECT")
            assert decision.quantity in (D("0"), D("1000"))


def test_a_trade_already_within_every_budget_is_approved_unchanged():
    decision = _size(base_quantity=D("100"))  # ~59 USDT notional, ~3.5 USDT risk
    assert decision.action == "APPROVE"
    assert decision.quantity == D("100")


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


def test_portfolio_cap_rejects_when_the_sum_would_exceed_ten_percent():
    open_ = [Exposure("BTC-USDT", "crypto_major", D("15"), D("1000")),
             Exposure("NCSKX-USDT", "tokenized_equity", D("15"), D("1000"))]
    decision = _full(open_exposures=open_)  # 30 + ~17 > 42
    assert decision.action == "REJECT"
    assert decision.reasons == ["PORTFOLIO_RISK_CAP"]
    assert decision.quantity == 0


def test_group_cap_rejects_a_second_correlated_alt():
    open_ = [Exposure("A-USDT", "crypto_alt", D("10"), D("1000"))]
    decision = _full(open_exposures=open_)  # alt group 10 + ~17 > 21, portfolio 27 <= 42
    assert decision.action == "REJECT"
    assert decision.reasons == ["GROUP_RISK_CAP"]


def test_four_concurrent_full_size_positions_are_possible_when_risk_allows():
    """Four ~1000-notional positions with tight stops across groups fit: the
    count limit (max 4) is enforced by live_execution, not by the kernel."""
    open_ = [Exposure("BTC-USDT", "crypto_major", D("8"), D("1000")),
             Exposure("NCSKX-USDT", "tokenized_equity", D("8"), D("1000")),
             Exposure("NCCOX-USDT", "commodity_fx", D("8"), D("1000"))]
    decision = _full(stop=D("0.995"), open_exposures=open_)  # alt ~12 -> total ~36 <= 42
    assert decision.action == "APPROVE"
    assert decision.quantity == D("1000")
    assert decision.portfolio_risk_after <= D("42")


def test_there_is_no_notional_multiple_ceiling_any_more():
    open_ = [Exposure("BTC-USDT", "crypto_major", D("1"), D("3000"))]
    assert _full(open_exposures=open_).action == "APPROVE"


def test_below_exchange_minimum_rejects():
    decision = _size(min_notional=D("5000"))
    assert decision.action == "REJECT"
    assert "BELOW_EXCHANGE_MINIMUM" in decision.reasons


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



# ---------------------------------------------------------------------------
# 2026-09-29: production caps total 10 % / correlated group 5 % (the group cap
# was 10 % for a few hours at 22ee411). APPROVE the fixed size or REJECT.
# ---------------------------------------------------------------------------

PROD = SafetyKernelConfig(max_portfolio_risk_pct=D("0.10"), max_group_risk_pct=D("0.05"))


def _prod(**overrides):
    return _full(limits=PROD, **overrides)


def test_production_caps_match_the_yaml():
    safety = get_settings().safety
    assert (safety.max_portfolio_risk_pct, safety.max_group_risk_pct) == (
        PROD.max_portfolio_risk_pct,
        PROD.max_group_risk_pct,
    )


def test_a_trade_inside_the_5pct_group_cap_is_approved_at_the_fixed_size():
    """420 equity, stop 1 % -> worst case ~16.9 USDT <= 21 (5 %)."""
    d = _prod(stop=D("0.99"))
    assert d.action == "APPROVE"
    assert d.quantity == D("1000")                       # exactly 100 x 10


def test_a_trade_between_5_and_10_pct_is_rejected_by_the_group_cap_never_reduced():
    """420 equity, stop 2 % -> worst case ~26.9 USDT: > 21 (5 % group) but <= 42 total."""
    d = _prod(stop=D("0.98"))
    assert d.action == "REJECT"
    assert d.reasons == ["GROUP_RISK_CAP"]
    assert d.quantity == 0


def test_the_total_cap_binds_across_groups():
    open_ = [Exposure("BTC-USDT", "crypto_major", D("30"), D("1000"))]   # other group
    d = _prod(stop=D("0.99"), open_exposures=open_)       # group alt ~16.9 <= 21, total ~46.9 > 42
    assert d.action == "REJECT"
    assert d.reasons == ["PORTFOLIO_RISK_CAP"]


def test_a_second_correlated_trade_is_rejected_by_the_group_cap():
    open_ = [Exposure("A-USDT", "crypto_alt", D("16.9"), D("1000"))]
    d = _prod(stop=D("0.99"), open_exposures=open_)      # group 33.8 > 21, total 33.8 <= 42
    assert d.action == "REJECT"
    assert d.reasons == ["GROUP_RISK_CAP"]
    assert d.quantity == 0


def test_a_single_trade_above_ten_percent_is_rejected_by_both_caps_never_reduced():
    d = _prod(stop=D("0.95"))                              # ~56 USDT worst case
    assert d.action == "REJECT" and d.quantity == 0
    assert {"PORTFOLIO_RISK_CAP", "GROUP_RISK_CAP"} <= set(d.reasons)


def test_the_liquidation_guard_still_rejects_regardless_of_the_caps():
    d = _prod(stop=D("0.90"))  # 10 % stop at 10x: inside the liquidation buffer
    assert d.action == "REJECT"
    assert "LIQUIDATION_TOO_CLOSE" in d.reasons


def test_the_kernel_never_enlarges_or_shrinks_the_fixed_size_under_the_caps():
    for stop in (D("0.999"), D("0.99"), D("0.98"), D("0.97"), D("0.95")):
        for equity in (D("100"), D("420"), D("5000")):
            d = _prod(stop=stop, equity=equity)
            assert d.quantity in (D("0"), D("1000"))


def test_a_group_cap_above_the_total_cap_is_refused_at_config_load():
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        SafetyKernelConfig(max_portfolio_risk_pct=D("0.10"), max_group_risk_pct=D("0.15"))
