"""Tests for crypto_trading/godfather/policy_evaluation.py.

The two properties everything else rests on are tested first and by
mutation, not by inspection: a stop decision can never see a price that
had not happened yet, and a trade whose moved stop was not actually
watched is excluded rather than scored.
"""

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trading.godfather import stats
from crypto_trading.godfather.policy_evaluation import (
    MAX_UNOBSERVED_MINUTES,
    MIN_ACTIVATED_FOR_VERDICT,
    POLICY_UNDER_TEST,
    PRE_REGISTERED_POLICIES,
    classify_effect,
    evaluate_policy,
    evaluate_trade,
    render_markdown,
    run_policy_evaluation,
    scored,
    shadow_replication,
    simulate_stop_policy,
    verdict_for,
)
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.godfather.intelligence_fixtures import (
    NOW,
    OPENED,
    make_position,
    path_point,
)
from tests.crypto_trading.godfather.test_intelligence_pipeline import _seed_trade
from tests.crypto_trading.test_market_snapshot import _settings

_RISK_LIMITS = _settings().risk_limits
_BE = PRE_REGISTERED_POLICIES[0]
_BE_15 = PRE_REGISTERED_POLICIES[1]
_LOCK = PRE_REGISTERED_POLICIES[2]


def _path(position, *prices, step=1.0):
    return [
        path_point(i * step, Decimal(str(price)), position=position)
        for i, price in enumerate(prices)
    ]


def _winner(position_id="w", opened_offset_hours=0):
    """Hits +1%, dips back to entry, then goes on to the real target."""
    position = make_position(
        position_id,
        exit_price=Decimal("110"),
        exit_reason="target",
        opened_at=OPENED + timedelta(hours=opened_offset_hours),
    )
    return position, _path(position, 100, 101.2, 99.9, 104, 109.9)


def _loser(position_id="l", opened_offset_hours=0):
    """Hits +1%, then falls all the way to the original stop."""
    position = make_position(
        position_id,
        exit_price=Decimal("95"),
        exit_reason="stop_loss",
        opened_at=OPENED + timedelta(hours=opened_offset_hours),
    )
    return position, _path(position, 100, 101.2, 99.9, 97, 95.1)


def _sim(position, points, policy=_BE):
    actual = (position.simulated_fill_exit - position.simulated_fill_entry) / (
        position.simulated_fill_entry
    ) * position.size
    return simulate_stop_policy(position, points, policy, actual, _RISK_LIMITS, Decimal("0"))


# ---------------------------------------------------------------------
# No lookahead
# ---------------------------------------------------------------------


def test_the_stop_decision_cannot_be_changed_by_anything_after_it():
    position, points = _winner()
    before = _sim(position, points)
    assert before.stopped

    stop_index = next(i for i, p in enumerate(points) if p.minutes_in_trade == before.stop_minutes)
    mutated = list(points[: stop_index + 1]) + [
        path_point(p.minutes_in_trade, Decimal("500"), position=position)
        for p in points[stop_index + 1 :]
    ]
    after = _sim(position, mutated)

    assert after.stop_minutes == before.stop_minutes
    assert after.pnl_usdt == before.pnl_usdt
    assert after.activation_minutes == before.activation_minutes
    assert after.mfe_before_pct == before.mfe_before_pct


def test_the_activation_snapshot_uses_only_the_prefix():
    """MFE/MAE 'before the intervention' must not move when the future
    after the activation tick is rewritten."""
    position, points = _winner()
    base = _sim(position, points)
    mutated = points[:2] + [
        path_point(p.minutes_in_trade, Decimal("50"), position=position) for p in points[2:]
    ]
    changed = _sim(position, mutated)

    assert changed.activation_minutes == base.activation_minutes
    assert changed.mfe_before_pct == base.mfe_before_pct
    assert changed.mae_before_pct == base.mae_before_pct


def test_a_threshold_touch_only_arms_the_stop_from_the_next_tick():
    """The tick that reaches +1% cannot also be the tick that stops out -
    the same ordering the live mechanism and the PAPER shadow have."""
    position = make_position(exit_price=Decimal("110"), exit_reason="target")
    points = _path(position, 100, 101.0, 101.5, 109.9)
    sim = _sim(position, points)

    assert sim.activated
    assert sim.activation_minutes == 1.0
    assert not sim.stopped


# ---------------------------------------------------------------------
# Per-trade outcomes and classification
# ---------------------------------------------------------------------


def test_a_break_even_that_never_binds_reproduces_the_real_outcome_exactly():
    position = make_position(exit_price=Decimal("110"), exit_reason="target")
    points = _path(position, 100, 101.5, 104, 109.9)
    record = evaluate_trade(position, points, _RISK_LIMITS)

    assert record is not None
    sim = record.sims[POLICY_UNDER_TEST]
    assert sim.activated and not sim.stopped
    assert record.delta(POLICY_UNDER_TEST) == Decimal("0")
    assert classify_effect(sim, record.actual_pnl_usdt) == "NEUTRAL"


def test_a_winner_that_dips_to_entry_is_stopped_early_at_a_small_loss():
    position, points = _winner()
    record = evaluate_trade(position, points, _RISK_LIMITS)
    sim = record.sims[POLICY_UNDER_TEST]

    assert sim.stopped
    # Stopped at entry: costs (spread, slippage, fees) make it a small loss.
    assert Decimal("-2") < sim.pnl_usdt < Decimal("0")
    assert record.delta(POLICY_UNDER_TEST) < 0
    assert classify_effect(sim, record.actual_pnl_usdt) == "WINNER_STOPPED_EARLY"
    assert record.to_dict()["effect"] == "WINNER_STOPPED_EARLY"
    assert record.mfe_after_pct > record.sims[POLICY_UNDER_TEST].mfe_before_pct


def test_a_loser_that_first_went_green_has_its_loss_limited():
    position, points = _loser()
    record = evaluate_trade(position, points, _RISK_LIMITS)
    sim = record.sims[POLICY_UNDER_TEST]

    assert sim.stopped
    assert record.delta(POLICY_UNDER_TEST) > Decimal("40")
    assert classify_effect(sim, record.actual_pnl_usdt) == "LOSS_LIMITED"


def test_a_trade_that_never_reaches_the_threshold_is_not_activated():
    position = make_position(exit_price=Decimal("97"), exit_reason="stop_loss")
    points = _path(position, 100, 100.5, 99, 97.5)
    record = evaluate_trade(position, points, _RISK_LIMITS)

    assert not record.sims[POLICY_UNDER_TEST].activated
    assert classify_effect(record.sims[POLICY_UNDER_TEST], record.actual_pnl_usdt) == (
        "NOT_ACTIVATED"
    )


def test_the_profit_lock_ratchets_up_and_never_down():
    position = make_position(exit_price=Decimal("110"), exit_reason="target")
    points = _path(position, 100, 102, 104, 103.5, 101.9)
    sim = _sim(position, points, _LOCK)

    # Best excursion 4% -> lock at +2%; the later fall to 101.9 hits it.
    assert sim.stopped
    assert sim.stop_level == Decimal("102.00")
    assert sim.pnl_usdt > Decimal("0")


def test_the_higher_threshold_activates_later_or_not_at_all():
    position = make_position(exit_price=Decimal("110"), exit_reason="target")
    points = _path(position, 100, 101.2, 99.9, 109.9)

    live, later = _sim(position, points, _BE), _sim(position, points, _BE_15)

    assert live.activated and live.stopped
    assert later.activation_minutes > live.activation_minutes
    assert not later.stopped


def test_a_crossing_between_the_last_tick_and_the_close_has_two_readings():
    """Last tick above break-even, real exit at the original stop: the
    price crossed break-even in the final interval. A stop order fills at
    the stop; the pessimistic reading grants the policy nothing."""
    position = make_position(exit_price=Decimal("95"), exit_reason="stop_loss")
    close_minutes = (position.closed_at - position.opened_at).total_seconds() / 60
    points = [
        path_point(close_minutes - 3, Decimal("100"), position=position),
        path_point(close_minutes - 2, Decimal("101.2"), position=position),
        path_point(close_minutes - 1, Decimal("100.4"), position=position),
    ]
    record = evaluate_trade(position, points, _RISK_LIMITS)
    sim = record.sims[POLICY_UNDER_TEST]

    assert sim.stopped
    assert sim.pnl_usdt > record.actual_pnl_usdt
    assert sim.pnl_pessimistic_usdt == record.actual_pnl_usdt


# ---------------------------------------------------------------------
# Exclusions: nothing is scored that was not observed
# ---------------------------------------------------------------------


def test_a_stop_armed_across_an_unobserved_hole_is_unobservable_and_not_scored():
    position = make_position(exit_price=Decimal("95"), exit_reason="stop_loss")
    points = [
        path_point(0, Decimal("100"), position=position),
        path_point(1, Decimal("101.2"), position=position),
        path_point(1 + MAX_UNOBSERVED_MINUTES + 60, Decimal("99"), position=position),
    ]
    record = evaluate_trade(position, points, _RISK_LIMITS)
    sim = record.sims[POLICY_UNDER_TEST]

    assert sim.activated and sim.stopped
    assert not sim.observable
    assert scored([record], POLICY_UNDER_TEST) == []
    assert record.to_dict()["effect"] == "UNOBSERVABLE"


def test_a_zero_size_position_is_excluded_rather_than_scored_as_neutral():
    position = make_position(size=Decimal("0"), exit_price=Decimal("110"), exit_reason="target")
    points = _path(position, 100, 101.2, 99.9, 109.9)

    assert evaluate_trade(position, points, _RISK_LIMITS) is None


def test_a_position_without_real_pnl_is_excluded():
    position = make_position(exit_price=None, exit_reason="stop_loss", fees=None, funding=None)
    points = _path(position, 100, 101.2, 99.9)

    assert evaluate_trade(position, points, _RISK_LIMITS) is None


def test_the_shadow_replication_drops_excluded_positions_and_checks_fidelity():
    def row(pid, reached, diff, opened="2026-09-25T08:00:00+00:00"):
        return {
            "position_id": pid, "threshold_pct": "0.010", "status": "CLOSED",
            "threshold_reached": 1 if reached else 0, "pnl_difference": diff,
            "hypothetical_baseline_pnl": "10", "shadow_realized_pnl": str(10 + float(diff)),
            "opened_at": opened,
        }

    rows = [row("a", True, "-5"), row("b", True, "3"), row("z", True, "-50"),
            row("c", False, "0"), row("d", False, "0.5")]
    result = shadow_replication(rows, OPENED, frozenset({"z"}))[POLICY_UNDER_TEST]

    assert result["reached_and_scorable"] == 2
    assert result["paired"]["total_delta_usdt"] == "-2"
    assert result["not_reached_rows"] == 2
    assert result["not_reached_nonzero_delta"] == 1


# ---------------------------------------------------------------------
# Verdicts: the bar, and the refusal to overclaim
# ---------------------------------------------------------------------


def _test(n, ci_low, ci_high):
    return {"n": n, "ci_low_usdt": ci_low, "ci_high_usdt": ci_high}


def test_below_the_sample_floor_the_verdict_is_insufficient_data_whatever_the_ci():
    assert verdict_for(_test(MIN_ACTIVATED_FOR_VERDICT - 1, -20, -10), True, -5, -5) == (
        "INSUFFICIENT_DATA"
    )


def test_robust_harm_needs_fdr_significance_a_negative_ci_and_both_halves():
    enough = MIN_ACTIVATED_FOR_VERDICT
    assert verdict_for(_test(enough, -20, -10), True, -5, -5) == "ROBUST_HARM"
    assert verdict_for(_test(enough, -20, -10), False, -5, -5) == "NOISE"
    assert verdict_for(_test(enough, -20, 1), True, -5, -5) == "NOISE"
    assert verdict_for(_test(enough, -20, -10), True, 5, -5) == "NOISE"


def _book(make, count, start=0):
    trades = []
    for i in range(count):
        position, points = make(f"t{start + i}", opened_offset_hours=start + i)
        trades.append(evaluate_trade(position, points, _RISK_LIMITS))
    return trades


def test_a_consistently_harmful_policy_is_reported_as_worse_than_baseline():
    trades = _book(_winner, 40)
    report = evaluate_policy(trades, [], [], NOW, "run-1")
    answers = report["answers"]

    assert report["policies"][POLICY_UNDER_TEST]["verdict"] == "ROBUST_HARM"
    assert answers["overall_verdict"] == "WORSE_THAN_BASELINE"
    assert answers["q5_good_but_small_sample"] == "NOT_SUPPORTED"
    assert answers["change_live_rules"] is False
    assert report["promotion_allowed"] is False


def test_a_small_book_is_insufficient_data_not_a_conclusion():
    trades = _book(_winner, 5)
    report = evaluate_policy(trades, [], [], NOW, "run-1")

    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert report["answers"]["q1_generally_bad"] == "INSUFFICIENT_DATA"


def test_an_unproven_harm_is_reported_as_noise_not_as_disproven():
    trades = _book(_winner, 18) + _book(_loser, 17, start=100)
    report = evaluate_policy(trades, [], [], NOW, "run-1")
    answers = report["answers"]

    assert report["policies"][POLICY_UNDER_TEST]["verdict"] == "NOISE"
    assert answers["overall_verdict"] == "NOISE"
    assert answers["q1_generally_bad"] == "NOISE"


def test_the_naive_comparison_is_kept_apart_from_the_paired_effect():
    trades = _book(_winner, 3)
    idle = make_position("idle", exit_price=Decimal("96"), exit_reason="stop_loss")
    trades.append(evaluate_trade(idle, _path(idle, 100, 99, 96.5), _RISK_LIMITS))
    report = evaluate_policy(trades, [], [], NOW, "run-1")
    naive = report["naive_vs_paired"]

    assert naive["selection_effect_usdt"] > 0
    assert naive["paired_policy_effect_mean_usdt"] < 0


def test_the_markdown_report_renders_every_section():
    trades = _book(_winner, 12) + _book(_loser, 12, start=100)
    text = render_markdown(evaluate_policy(trades, [], [], NOW, "run-1"))

    for heading in ("## Answers", "## Policy level", "## Independent replication",
                    "## Mechanism", "## Exploratory breakdowns", "## Out-of-sample",
                    "## Real LIVE activations", "## Per-trade record"):
        assert heading in text
    assert "promotion_allowed = false" in text


# ---------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------


def test_sign_flip_p_value_is_small_for_a_consistent_effect_and_one_for_none():
    assert stats.sign_flip_p_value([1.0] * 12) < 0.01
    assert stats.sign_flip_p_value([0.0] * 12) == 1.0
    assert stats.sign_flip_p_value([]) == 1.0
    assert stats.sign_flip_p_value([1.0, -1.0] * 6) > 0.5


def test_sign_flip_p_value_is_deterministic():
    values = [3.0, -1.0, 2.5, 0.5, -0.2, 4.0]
    assert stats.sign_flip_p_value(values) == stats.sign_flip_p_value(values)


# ---------------------------------------------------------------------
# Runner + storage: real repository, promotion structurally impossible
# ---------------------------------------------------------------------


def test_the_runner_persists_one_report_and_excludes_zero_size_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "policy.db")
    for index in range(4):
        _seed_trade(
            repo, index, prices=[Decimal("100"), Decimal("101.5"), Decimal("108")],
            exit_price=Decimal("108"), exit_reason="target",
        )

    report = run_policy_evaluation(repo, _settings(), NOW, "run-1")
    rows = repo.find_godfather_policy_evaluations(POLICY_UNDER_TEST)

    assert len(rows) == 1
    assert rows[0]["promotion_allowed"] == 0
    assert rows[0]["verdict"] == report["verdict"]
    assert report["data"]["scorable_trades_with_path"] == 4


def test_the_table_cannot_hold_a_row_that_allows_promotion(tmp_path):
    repo = SQLiteRepository(tmp_path / "policy.db")
    conn = sqlite3.connect(tmp_path / "policy.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO godfather_policy_evaluations (evaluation_id, policy, evaluated_at, "
            "verdict, confidence, activated_trades, promotion_allowed, report_json, run_id) "
            "VALUES ('x', 'P', '2026-09-25', 'NOISE', 'LOW', 0, 1, '{}', 'r')"
        )
    conn.close()
    assert repo.find_godfather_policy_evaluations("P") == []
