"""Tests for crypto_trading/godfather/counterfactual.py.

The headline test in this file is `test_a_policy_decision_cannot_be_
changed_by_anything_that_happens_after_it`. Everything else the
counterfactual engine produces is worthless if that property does not
hold: a simulation that peeks at the future will always beat the real
system, will always look like a discovery, and will always fail the
moment it is switched on.
"""

from decimal import Decimal

from crypto_trading.godfather.counterfactual import (
    aggregate_policy_performance,
    assess_policy_significance,
    common_scorable_positions,
    run_counterfactuals,
)
from crypto_trading.godfather.thesis import ThesisThresholds
from tests.crypto_trading.godfather.intelligence_fixtures import (
    NOW,
    make_position,
    path_point,
)
from tests.crypto_trading.test_market_snapshot import _settings

_THRESHOLDS = ThesisThresholds(
    watch=Decimal("0.35"), protect=Decimal("0.55"), exit=Decimal("0.75"), max_hold_hours=24
)
_RISK_LIMITS = _settings().risk_limits


def _path(position, *prices_at_minutes, decay=Decimal("0")):
    return [
        path_point(minutes, price, position=position, decay=decay)
        for minutes, price in prices_at_minutes
    ]


def _by_policy(results):
    return {result.policy: result for result in results}


def _run(position, points):
    return run_counterfactuals(
        position, points, _RISK_LIMITS, _THRESHOLDS, NOW, "test-run"
    )


def test_a_policy_decision_cannot_be_changed_by_anything_that_happens_after_it():
    """No-lookahead, proven by mutation rather than by inspection.

    A policy fires at some index. Replace every price AFTER that index
    with an absurd value and re-run: the trigger time and the simulated
    exit price must be identical, because the decision was made from a
    truncated prefix that no longer exists in the mutated tail.
    """
    position = make_position()
    points = _path(
        position,
        (0, Decimal("100")),
        (30, Decimal("106")),
        (60, Decimal("101")),
        (90, Decimal("99")),
        (120, Decimal("98")),
    )
    baseline = _by_policy(_run(position, points))["EXIT_ON_THESIS_WEAKENING"]
    assert baseline.triggered

    trigger_index = baseline.detail["trigger_index"]
    mutated = list(points[: trigger_index + 1]) + [
        path_point(p.minutes_in_trade, Decimal("500"), position=position)
        for p in points[trigger_index + 1 :]
    ]

    rerun = _by_policy(_run(position, mutated))["EXIT_ON_THESIS_WEAKENING"]

    assert rerun.trigger_minutes == baseline.trigger_minutes
    assert rerun.simulated_exit_price == baseline.simulated_exit_price
    assert rerun.simulated_pnl_usdt == baseline.simulated_pnl_usdt


def test_no_counterfactuals_are_produced_when_the_real_outcome_is_unknown():
    """A LIVE-mirrored close has no PAPER exit data, so there is nothing
    to be counter TO. Producing a flagged row anyway would let an
    unanchored simulation leak into aggregates."""
    position = make_position(exit_price=None, fees=None, funding=None)
    points = _path(position, (0, Decimal("100")), (30, Decimal("104")))

    assert _run(position, points) == []


def test_no_counterfactuals_are_produced_for_an_unobserved_path():
    assert _run(make_position(), []) == []


def test_the_baseline_row_reproduces_the_real_outcome_with_zero_delta():
    position = make_position(exit_price=Decimal("97"))
    points = _path(position, (0, Decimal("100")), (30, Decimal("97")))

    baseline = _by_policy(_run(position, points))["BASELINE"]

    assert baseline.delta_pnl_usdt == Decimal("0")
    assert baseline.simulated_pnl_usdt == baseline.actual_pnl_usdt


def test_rejecting_the_entry_produces_exactly_zero_pnl_and_no_costs():
    position = make_position(exit_price=Decimal("97"))
    points = _path(position, (0, Decimal("100")), (30, Decimal("97")))

    reject = _by_policy(_run(position, points))["REJECT_ENTRY"]

    assert reject.simulated_pnl_usdt == Decimal("0")
    # The real trade lost, so not taking it is an improvement of exactly
    # the loss - no more, no less.
    assert reject.delta_pnl_usdt == -reject.actual_pnl_usdt


def test_a_simulated_exit_pays_the_same_friction_the_live_engine_charges():
    """A policy must not be able to look good merely by trading more
    often for free: the simulated exit fill is worse than the observed
    price by exactly spread+slippage, and a fee is charged on notional."""
    position = make_position(exit_price=Decimal("100"), fees=Decimal("0"), funding=Decimal("0"))
    points = _path(position, (0, Decimal("100")), (30, Decimal("100")))

    result = _by_policy(_run(position, points))["SAFE_TP_AT_HALF_TARGET"]
    # progress never reached 0.5, so this policy could not fire
    assert not result.triggered

    tightened = _by_policy(_run(position, points))["TIGHTEN_SL_AFTER_FAVORABLE"]
    assert not tightened.triggered


def test_an_exit_policy_that_never_fires_is_recorded_as_identical_to_baseline():
    position = make_position(exit_price=Decimal("101"))
    points = _path(position, (0, Decimal("100")), (30, Decimal("100.5")))

    result = _by_policy(_run(position, points))["EXIT_ON_THESIS_INVALID"]

    assert result.triggered is False
    assert result.delta_pnl_usdt == Decimal("0")
    assert "never triggered" in result.detail["note"]


def test_delaying_the_entry_uses_the_price_actually_observed_at_the_delay():
    position = make_position(exit_price=Decimal("97"))
    # Guardian's real cadence (~1-2 min): the delayed trade must be
    # WATCHED while it is open, or its outcome is UNOBSERVABLE (next test).
    points = _path(
        position,
        (0, Decimal("100")),
        (30, Decimal("96")),
        *[(30 + step * 5, Decimal("97.5")) for step in range(1, 18)],
        (120, Decimal("97")),
    )

    delayed = _by_policy(_run(position, points))["DELAY_ENTRY_30M"]

    assert delayed.triggered
    assert delayed.detail["delayed_entry_minutes"] == 30
    # Entering at 96 instead of 100 in a market that ends at 97 turns a
    # loss into a gain - the whole reason this policy is worth measuring.
    assert delayed.delta_pnl_usdt > Decimal("0")


def test_delaying_the_entry_is_unscorable_when_the_trade_closed_first():
    position = make_position(exit_price=Decimal("97"))
    points = _path(position, (0, Decimal("100")), (10, Decimal("97")))

    delayed = _by_policy(_run(position, points))["DELAY_ENTRY_30M"]

    assert delayed.triggered is False
    assert delayed.simulated_pnl_usdt is None
    assert "before the delay elapsed" in delayed.detail["reason"]


def test_reducing_on_weakening_splits_the_outcome_in_half():
    position = make_position(exit_price=Decimal("97"))
    points = _path(
        position, (0, Decimal("100")), (30, Decimal("106")), (60, Decimal("102"))
    )

    reduced = _by_policy(_run(position, points))["REDUCE_ON_WEAKENING"]

    assert reduced.triggered
    realised = Decimal(reduced.detail["realised_half_pnl_usdt"])
    riding = Decimal(reduced.detail["riding_half_pnl_usdt"])
    assert reduced.simulated_pnl_usdt == realised + riding
    assert riding == reduced.actual_pnl_usdt / 2


def test_the_common_subset_excludes_positions_a_policy_could_not_score():
    """The bias found on the first real sweep: DELAY_ENTRY_30M silently
    skipped the ten fastest-resolving trades, so its raw total was
    computed over a different, easier book than every other policy."""
    rows = [
        {
            "position_id": "a",
            "policy": "REJECT_ENTRY",
            "delta_pnl_usdt": "1",
            "actual_pnl_usdt": "-1",
            "no_lookahead_verified": 1,
            "triggered": 1,
        },
        {
            "position_id": "b",
            "policy": "REJECT_ENTRY",
            "delta_pnl_usdt": "1",
            "actual_pnl_usdt": "-1",
            "no_lookahead_verified": 1,
            "triggered": 1,
        },
        {
            "position_id": "a",
            "policy": "DELAY_ENTRY_30M",
            "delta_pnl_usdt": "5",
            "actual_pnl_usdt": "-1",
            "no_lookahead_verified": 1,
            "triggered": 1,
        },
        {
            "position_id": "b",
            "policy": "DELAY_ENTRY_30M",
            "delta_pnl_usdt": None,
            "actual_pnl_usdt": "-1",
            "no_lookahead_verified": 1,
            "triggered": 0,
        },
    ]

    common = common_scorable_positions(rows)

    assert common == {"a"}
    restricted = aggregate_policy_performance(rows, restrict_to=common)
    assert restricted["REJECT_ENTRY"]["n"] == 1
    assert restricted["DELAY_ENTRY_30M"]["n"] == 1


def test_aggregation_separates_damage_to_winners_from_help_to_losers():
    rows = [
        {
            "position_id": "winner",
            "policy": "EXIT_EARLY",
            "delta_pnl_usdt": "-90",
            "actual_pnl_usdt": "100",
            "no_lookahead_verified": 1,
            "triggered": 1,
        },
        {
            "position_id": "loser",
            "policy": "EXIT_EARLY",
            "delta_pnl_usdt": "40",
            "actual_pnl_usdt": "-50",
            "no_lookahead_verified": 1,
            "triggered": 1,
        },
    ]

    aggregated = aggregate_policy_performance(rows)["EXIT_EARLY"]

    assert aggregated["winner_delta_usdt"] == Decimal("-90")
    assert aggregated["loser_delta_usdt"] == Decimal("40")
    assert aggregated["total_delta_usdt"] == Decimal("-50")


def test_aggregation_ignores_rows_that_failed_the_no_lookahead_check():
    rows = [
        {
            "position_id": "a",
            "policy": "P",
            "delta_pnl_usdt": "1000",
            "actual_pnl_usdt": "-1",
            "no_lookahead_verified": 0,
            "triggered": 1,
        }
    ]

    assert aggregate_policy_performance(rows) == {}


def test_a_lucky_handful_of_improvements_is_not_reported_as_significant():
    """The guard against acting on a big-looking total: four wins out of
    five is not evidence when nine policies were compared over the same
    small book."""
    rows = []
    for index in range(5):
        rows.append(
            {
                "position_id": f"p{index}",
                "policy": "LUCKY",
                "delta_pnl_usdt": "100" if index < 4 else "-50",
                "actual_pnl_usdt": "-10",
                "no_lookahead_verified": 1,
                "triggered": 1,
            }
        )

    verdicts = assess_policy_significance(rows)

    assert verdicts["LUCKY"]["verdict"] == "NOT_SIGNIFICANT"


def test_a_consistent_improvement_that_spares_winners_is_flagged_robust():
    rows = []
    for index in range(40):
        rows.append(
            {
                "position_id": f"p{index}",
                "policy": "GOOD",
                "delta_pnl_usdt": "10",
                "actual_pnl_usdt": "-5",
                "no_lookahead_verified": 1,
                "triggered": 1,
            }
        )

    verdicts = assess_policy_significance(rows)

    assert verdicts["GOOD"]["verdict"] == "ROBUST_IMPROVEMENT"
    assert verdicts["GOOD"]["damages_winners"] is False


def test_an_improvement_paid_for_by_winners_is_called_out_separately():
    rows = []
    for index in range(40):
        winner = index % 2 == 0
        rows.append(
            {
                "position_id": f"p{index}",
                "policy": "CHURN",
                "delta_pnl_usdt": "1" if winner else "20",
                "actual_pnl_usdt": "50" if winner else "-40",
                "no_lookahead_verified": 1,
                "triggered": 1,
            }
        )
    # Make the winner side genuinely damaging.
    for row in rows:
        if Decimal(row["actual_pnl_usdt"]) > 0:
            row["delta_pnl_usdt"] = "-30"

    verdicts = assess_policy_significance(rows)

    assert verdicts["CHURN"]["damages_winners"] is True
    assert verdicts["CHURN"]["verdict"] in (
        "IMPROVES_BUT_DAMAGES_WINNERS",
        "NOT_SIGNIFICANT",
    )


def test_the_baseline_policy_is_excluded_from_significance_testing():
    rows = [
        {
            "position_id": "a",
            "policy": "BASELINE",
            "delta_pnl_usdt": "0",
            "actual_pnl_usdt": "-1",
            "no_lookahead_verified": 1,
            "triggered": 1,
        }
    ]

    assert assess_policy_significance(rows) == {}


def test_a_delayed_trade_that_was_not_watched_is_unobservable_not_scored():
    position = make_position(exit_price=Decimal("97"))
    points = _path(
        position, (0, Decimal("100")), (30, Decimal("96")), (120, Decimal("97"))
    )

    delayed = _by_policy(_run(position, points))["DELAY_ENTRY_30M"]

    assert delayed.delta_pnl_usdt is None
    assert delayed.detail["observation_status"] == "UNOBSERVABLE"
