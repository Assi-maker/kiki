"""Tests for crypto_trading/godfather/investigator.py.

The classification taxonomy is the vocabulary every later statistic is
grouped by, so each label is driven directly. The ordering matters as
much as the labels: the first real sweep over the live book produced zero
TARGET_TOO_FAR and zero SL_TOO_WIDE because a more generic branch was
checked first, which is exactly the kind of silent gap these tests
exist to catch.
"""

from decimal import Decimal

from crypto_trading.godfather.investigator import (
    build_avoidable_loss_finding,
    classify_trade,
    judge_entry,
    judge_management,
    summarise_sustained_factors,
)
from crypto_trading.godfather.path import compute_path_metrics, reconstruct_price_path
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.godfather import CounterfactualResult
from tests.crypto_trading.godfather.intelligence_fixtures import (
    NOW,
    evidence_record,
    make_position,
    observation_row,
    path_point,
)

_WATCH = Decimal("0.35")
_EXIT = Decimal("0.75")


def _metrics(position, prices_at_minutes, decays=None):
    rows = []
    for index, (minutes, price) in enumerate(prices_at_minutes):
        decay = decays[index] if decays else "0.0"
        rows.append(observation_row(position, minutes, price, decay=decay))
    points = reconstruct_price_path(position, rows)
    return points, compute_path_metrics(position, points, _WATCH, _EXIT)


def test_a_win_that_kept_its_gains_is_good_entry_good_management():
    position = make_position(exit_price=Decimal("110"), exit_reason="target")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("108")), (60, Decimal("110"))]
    )

    classification, _reasons = classify_trade(position, metrics, Decimal("100"), None)

    assert classification == "GOOD_ENTRY_GOOD_MANAGEMENT"


def test_a_real_move_handed_back_far_from_target_is_exit_too_late():
    """A 4% move on a trade whose target was 10% away: the target was not
    the binding constraint, the exit was."""
    position = make_position(exit_price=Decimal("100"), exit_reason="time_limit")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("104")), (60, Decimal("100"))]
    )

    classification, _reasons = classify_trade(position, metrics, Decimal("-5"), None)

    assert classification == "EXIT_TOO_LATE"


def test_coming_within_reach_of_the_target_without_touching_it_is_target_too_far():
    """Reached 109 with the target at 110: had the target been set a
    fraction nearer, this trade would have won. That is a parameter
    finding, not a management one."""
    position = make_position(exit_price=Decimal("99"), exit_reason="time_limit")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("109")), (60, Decimal("99"))]
    )

    classification, reasons = classify_trade(position, metrics, Decimal("-10"), None)

    assert classification == "TARGET_TOO_FAR"
    assert "came within reach of the target and never touched it" in reasons


def test_a_stop_paid_long_after_the_thesis_died_is_sl_too_wide():
    position = make_position(exit_price=Decimal("95"), exit_reason="stop_loss")
    _points, metrics = _metrics(
        position,
        [(0, Decimal("100")), (30, Decimal("99")), (180, Decimal("95"))],
        decays=["0.1", "0.9", "0.9"],
    )

    classification, _reasons = classify_trade(position, metrics, Decimal("-50"), None)

    assert classification == "SL_TOO_WIDE"


def test_a_breakout_that_never_went_favourable_is_a_false_breakout():
    position = make_position(exit_price=Decimal("97"), exit_reason="stop_loss")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("100.1")), (60, Decimal("97"))]
    )
    candidate = Candidate(
        candidate_id="c",
        idempotency_key="k",
        instrument="BTC-USDT",
        discovery_run_id="r",
        evidence_hash="h",
        status="CONFIRMED",
        evidence_record=evidence_record(trigger_reasons=("momentum_breakout",)),
        created_at=NOW,
        updated_at=NOW,
    )

    classification, _reasons = classify_trade(position, metrics, Decimal("-30"), candidate)

    assert classification == "FALSE_BREAKOUT"


def test_a_loss_with_sustained_momentum_collapse_is_momentum_decay():
    position = make_position(exit_price=Decimal("99.9"), exit_reason="time_limit")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("100.2")), (60, Decimal("99.9"))]
    )

    classification, _reasons = classify_trade(
        position, metrics, Decimal("-1"), None, {"momentum_decay": True}
    )

    assert classification == "MOMENTUM_DECAY"


def test_a_loss_in_a_sustained_bad_btc_regime_is_a_regime_failure():
    position = make_position(exit_price=Decimal("99.9"), exit_reason="time_limit")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("100.2")), (60, Decimal("99.9"))]
    )

    classification, _reasons = classify_trade(
        position, metrics, Decimal("-1"), None, {"market_regime": True}
    )

    assert classification == "REGIME_FAILURE"


def test_a_trade_that_never_moved_either_way_is_noise():
    position = make_position(exit_price=Decimal("100.05"), exit_reason="time_limit")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("100.1")), (60, Decimal("100.05"))]
    )

    classification, _reasons = classify_trade(position, metrics, Decimal("0.4"), None)

    assert classification == "NOISE"


def test_a_loss_with_no_favourable_excursion_at_all_is_a_bad_entry():
    position = make_position(exit_price=Decimal("97"), exit_reason="stop_loss")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("100.1")), (60, Decimal("97"))]
    )

    classification, _reasons = classify_trade(position, metrics, Decimal("-30"), None)

    assert classification == "BAD_ENTRY"


def test_a_trade_with_no_observed_path_is_unknown_rather_than_guessed():
    position = make_position()
    _points, metrics = _metrics(position, [])

    classification, reasons = classify_trade(position, metrics, Decimal("-5"), None)

    assert classification == "UNKNOWN"
    assert "no scorable outcome or no observed price path" in reasons


def test_a_trade_with_no_scorable_pnl_is_unknown():
    position = make_position(exit_price=None, fees=None, funding=None)
    _points, metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("104"))])

    classification, _reasons = classify_trade(position, metrics, None, None)

    assert classification == "UNKNOWN"


def test_entry_and_management_are_judged_independently_of_each_other():
    """The split that makes fault_domain meaningful: an entry that found
    a 6% move was a good entry even though the trade lost everything."""
    position = make_position(exit_price=Decimal("99"), exit_reason="time_limit")
    _points, metrics = _metrics(
        position, [(0, Decimal("100")), (30, Decimal("106")), (60, Decimal("99"))]
    )

    assert judge_entry(metrics, Decimal("-10")) == "GOOD"
    assert judge_management(metrics, Decimal("-10")) == "BAD"


def test_management_is_unknown_when_there_was_never_a_gain_to_manage():
    position = make_position(exit_price=Decimal("97"))
    _points, metrics = _metrics(position, [(0, Decimal("100")), (30, Decimal("98"))])

    assert judge_management(metrics, Decimal("-30")) == "UNKNOWN"


def test_sustained_factors_require_a_majority_of_the_trade_not_a_single_spike():
    position = make_position()
    points = [
        path_point(0, Decimal("100"), position=position, factors={"momentum_decay": 0.9}),
        path_point(30, Decimal("100"), position=position, factors={"momentum_decay": 0.0}),
        path_point(60, Decimal("100"), position=position, factors={"momentum_decay": 0.0}),
    ]

    assert summarise_sustained_factors(points) == {"momentum_decay": False}


def test_sustained_factors_fire_when_the_condition_held_for_half_the_trade():
    position = make_position()
    points = [
        path_point(m, Decimal("100"), position=position, factors={"volume_decay": 0.95})
        for m in (0, 30)
    ] + [path_point(60, Decimal("100"), position=position, factors={"volume_decay": 0.0})]

    assert summarise_sustained_factors(points) == {"volume_decay": True}


# ---------------------------------------------------------------------
# The avoidable-loss finding
# ---------------------------------------------------------------------


def _counterfactual(policy: str, delta: str, minutes: float = 30.0) -> CounterfactualResult:
    return CounterfactualResult(
        counterfactual_id=f"cf-{policy}",
        position_id="pos-1",
        policy=policy,
        created_at=NOW,
        triggered=True,
        trigger_minutes=minutes,
        simulated_pnl_usdt=Decimal("0"),
        actual_pnl_usdt=Decimal("-10"),
        delta_pnl_usdt=Decimal(delta),
        no_lookahead_verified=True,
        run_id="run",
    )


def test_no_helpful_alternative_is_reported_honestly_as_none():
    finding = build_avoidable_loss_finding([_counterfactual("REJECT_ENTRY", "-5")], {})

    assert finding.policy is None
    assert "would have improved its outcome" in finding.explanation


def test_the_best_alternative_is_reported_when_it_also_helps_the_whole_book():
    portfolio = {
        "EXIT_ON_THESIS_WEAKENING": {
            "total_delta_usdt": Decimal("500"),
            "winner_delta_usdt": Decimal("10"),
        }
    }

    finding = build_avoidable_loss_finding(
        [_counterfactual("EXIT_ON_THESIS_WEAKENING", "40")], portfolio
    )

    assert finding.policy == "EXIT_ON_THESIS_WEAKENING"
    assert finding.estimated_pnl_improvement_usdt == Decimal("40")
    assert finding.winner_damage_checked is True


def test_an_alternative_that_takes_money_out_of_winners_is_not_recommended():
    """The requirement's own clause. `REJECT_ENTRY` is net positive over
    a losing book by construction - it must still be refused, because it
    pays for that by destroying every winning trade and offers no rule
    that could have told the two apart beforehand."""
    portfolio = {
        "REJECT_ENTRY": {
            "total_delta_usdt": Decimal("711"),
            "winner_delta_usdt": Decimal("-930"),
        }
    }

    finding = build_avoidable_loss_finding(
        [_counterfactual("REJECT_ENTRY", "40")], portfolio
    )

    assert finding.policy is None
    assert finding.winner_damage_checked is True


def test_an_alternative_that_loses_money_across_the_book_is_not_recommended():
    """The requirement's second clause, enforced: helping THIS trade is
    not enough if adopting the rule costs more elsewhere."""
    portfolio = {
        "SAFE_TP_AT_HALF_TARGET": {
            "total_delta_usdt": Decimal("-300"),
            "winner_delta_usdt": Decimal("-400"),
        }
    }

    finding = build_avoidable_loss_finding(
        [_counterfactual("SAFE_TP_AT_HALF_TARGET", "40")], portfolio
    )

    assert finding.policy is None
    assert finding.winner_damage_checked is True
    assert "damage elsewhere" in finding.explanation


def test_without_portfolio_evidence_the_finding_refuses_to_claim_it_was_checked():
    finding = build_avoidable_loss_finding([_counterfactual("REJECT_ENTRY", "40")], None)

    assert finding.policy == "REJECT_ENTRY"
    assert finding.winner_damage_checked is False
    assert "NOT claimed" in finding.explanation


def test_the_baseline_row_is_never_offered_as_an_alternative():
    finding = build_avoidable_loss_finding([_counterfactual("BASELINE", "40")], None)

    assert finding.policy is None
