"""Tests for crypto_trading/godfather/objective.py.

The single most important behaviour here is negative: a book that wins
more often must NOT automatically score higher. Optimising win rate is
how a trading system learns to take tiny profits and hold catastrophic
losses, and requirement 10 exists to make that trade unprofitable to the
optimiser rather than merely discouraged in a comment.
"""

from decimal import Decimal

from crypto_trading.godfather.objective import (
    TradeOutcome,
    compare_books,
    evaluate_objective,
)


def _outcome(
    pnl: str,
    *,
    mfe: str | None = None,
    mae: str | None = None,
    notional: str = "500",
    hold_minutes: float = 120.0,
    fees: str = "0.2",
    funding: str = "0",
) -> TradeOutcome:
    return TradeOutcome(
        pnl=Decimal(pnl),
        fees=Decimal(fees),
        funding=Decimal(funding),
        mfe_pnl=Decimal(mfe) if mfe is not None else None,
        mae_pnl=Decimal(mae) if mae is not None else None,
        notional=Decimal(notional),
        hold_minutes=hold_minutes,
        entry_slippage_pct=Decimal("0.05"),
        exit_slippage_pct=Decimal("-0.05"),
    )


def test_an_empty_book_scores_zero_without_raising():
    metrics = evaluate_objective([])

    assert metrics.trade_count == 0
    assert metrics.composite_score == 0.0
    assert metrics.expectancy_usdt is None


def test_the_headline_metrics_are_computed_from_the_trades_themselves():
    metrics = evaluate_objective([_outcome("10"), _outcome("-4"), _outcome("6")])

    assert metrics.trade_count == 3
    assert metrics.net_pnl_usdt == Decimal("12")
    assert metrics.expectancy_usdt == Decimal("4")
    assert metrics.profit_factor == Decimal("4")
    assert metrics.win_rate == 2 / 3


def test_drawdown_is_measured_peak_to_trough_on_the_realised_equity_curve():
    metrics = evaluate_objective(
        [_outcome("100"), _outcome("-30"), _outcome("-50"), _outcome("20")]
    )

    assert metrics.max_drawdown_usdt == Decimal("80")


def test_loss_severity_reports_average_loss_relative_to_average_win():
    metrics = evaluate_objective([_outcome("10"), _outcome("10"), _outcome("-40")])

    assert metrics.avg_loss_usdt == Decimal("40")
    assert metrics.loss_severity_ratio == Decimal("4")


def test_mfe_capture_shows_how_much_of_the_available_move_was_kept():
    metrics = evaluate_objective([_outcome("5", mfe="10"), _outcome("8", mfe="10")])

    assert metrics.mfe_capture_ratio == Decimal("0.65")


def test_mfe_capture_goes_negative_when_a_favourable_move_became_a_loss():
    """Not a defect in the metric - a real and important reading: the
    system ended below zero on a trade that was in profit."""
    metrics = evaluate_objective([_outcome("-20", mfe="10")])

    assert metrics.mfe_capture_ratio == Decimal("-2")


def test_costs_and_turnover_are_tracked_as_first_class_metrics():
    metrics = evaluate_objective([_outcome("10", fees="0.5"), _outcome("-4", fees="0.5")])

    assert metrics.total_costs_usdt == Decimal("1.0")
    assert metrics.turnover_notional_usdt == Decimal("1000")
    assert metrics.cost_share_of_gross is not None


def test_capital_efficiency_measures_profit_per_capital_hour():
    metrics = evaluate_objective([_outcome("10", notional="1000", hold_minutes=60)])

    # 10 USDT earned on 1000 USDT held for one hour = 10 per 1k-hour.
    assert metrics.capital_efficiency_usdt_per_1k_hour == Decimal("10")


def test_a_higher_win_rate_does_not_automatically_win_the_composite_score():
    """The core defence of requirement 10. `frequent` wins 90% of its
    trades and still loses money; `rare` wins 30% and makes money. The
    objective must prefer the second."""
    frequent = evaluate_objective([_outcome("1")] * 9 + [_outcome("-40")])
    rare = evaluate_objective([_outcome("-2")] * 7 + [_outcome("20")] * 3)

    assert frequent.win_rate > rare.win_rate
    assert frequent.net_pnl_usdt < rare.net_pnl_usdt
    assert rare.composite_score > frequent.composite_score


def test_the_composite_score_penalises_a_deeper_drawdown_for_the_same_profit():
    smooth = evaluate_objective([_outcome("10"), _outcome("10"), _outcome("10")])
    jagged = evaluate_objective([_outcome("60"), _outcome("-45"), _outcome("15")])

    assert smooth.net_pnl_usdt == jagged.net_pnl_usdt
    assert smooth.composite_score > jagged.composite_score


def test_a_candidate_that_earns_more_by_risking_more_is_not_objectively_better():
    """Requirement 9's rule, made mechanical: more P/L bought with more
    drawdown is a different risk appetite, not an improvement."""
    baseline = evaluate_objective([_outcome("10"), _outcome("10")])
    riskier = evaluate_objective([_outcome("70"), _outcome("-45")])

    comparison = compare_books(baseline, riskier)

    assert Decimal(comparison["net_pnl_delta_usdt"]) > 0
    assert comparison["objectively_better"] is False


def test_a_candidate_that_earns_more_with_no_extra_risk_is_objectively_better():
    baseline = evaluate_objective([_outcome("10"), _outcome("10")])
    better = evaluate_objective([_outcome("20"), _outcome("20")])

    comparison = compare_books(baseline, better)

    assert comparison["objectively_better"] is True
    assert comparison["composite_score_delta"] > 0
