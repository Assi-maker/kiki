"""Tests for the GODFATHER supervisor's building blocks: shared stop
simulation, the MFE model, the position decision (A + B), counterfactual
engine v2, entry selection, portfolio and the policy registry.

The properties pinned here are the ones a wrong implementation would
silently violate while still producing plausible-looking numbers: no
lookahead, never widening a stop, UNOBSERVABLE / UNAVAILABLE never
counted as neutral, and no status beyond the evidence.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trading.godfather.counterfactual import run_counterfactuals, simulate_position_policy
from crypto_trading.godfather.entry_selection import (
    EntrySignal,
    assign_selection,
    evaluate_selection,
    within_cohort_effects,
)
from crypto_trading.godfather.mfe_model import MIN_SAMPLES, MfeModel, observations_for_trade
from crypto_trading.godfather.policy_registry import (
    PolicyEvidence,
    decide_status,
    evaluate_gates,
    evaluate_registry,
    next_status,
    rollback_check,
    transitions,
)
from crypto_trading.godfather.portfolio import (
    MAX_PER_THEME,
    assign_portfolio_verdicts,
    cohort_outcome_dependence,
    exposure_profile,
    theme_of,
)
from crypto_trading.godfather.position_decision import decide_position
from crypto_trading.godfather.stop_simulation import (
    MAX_UNOBSERVED_MINUTES,
    StopPolicy,
    ThresholdRule,
    simulate_stop_rule,
)
from crypto_trading.godfather.thesis import ThesisFeatures, ThesisThresholds
from tests.crypto_trading.godfather.intelligence_fixtures import (
    NOW,
    OPENED,
    make_position,
    path_point,
)
from tests.crypto_trading.test_market_snapshot import _settings

_RISK = _settings().risk_limits
_THRESHOLDS = ThesisThresholds(
    watch=Decimal("0.35"), protect=Decimal("0.55"), exit=Decimal("0.75"), max_hold_hours=24
)
_BE = StopPolicy("BE", Decimal("0.01"), Decimal("0"), "test", "test")


def _dense(position, *prices, start=0.0):
    return [
        path_point(start + i, Decimal(str(p)), position=position) for i, p in enumerate(prices)
    ]


def _closing_at(minutes, **kwargs):
    return make_position(closed_at=OPENED + timedelta(minutes=minutes), **kwargs)


# ---------------------------------------------------------------------
# Stop simulation
# ---------------------------------------------------------------------


def test_a_stop_rule_is_only_ever_shown_the_observed_prefix():
    position = _closing_at(5, exit_price=Decimal("110"), exit_reason="target")
    seen: list[int] = []

    def rule(prefix):
        seen.append(len(prefix))
        return None

    points = _dense(position, 100, 101, 102, 103, 104, 105)
    simulate_stop_rule(position, points, rule, Decimal("100"), _RISK, Decimal("0"))

    assert seen == [1, 2, 3, 4, 5, 6]


def test_a_proposal_at_or_below_the_original_stop_is_ignored_never_widening():
    position = _closing_at(3, exit_price=Decimal("110"), exit_reason="target")
    points = _dense(position, 100, 99, 94.9, 100)
    sim = simulate_stop_rule(
        position, points, lambda prefix: Decimal("90"), Decimal("100"), _RISK, Decimal("0")
    )

    assert not sim.activated
    assert not sim.stopped


def test_the_threshold_rule_refuses_out_of_order_prefixes():
    position = make_position()
    rule = ThresholdRule(position, _BE)
    points = _dense(position, 100, 101)
    rule(points[:1])
    with pytest.raises(ValueError):
        rule(points[:1])


def test_a_stop_armed_across_a_hole_is_unobservable():
    position = _closing_at(200, exit_price=Decimal("95"), exit_reason="stop_loss")
    points = [
        path_point(0, Decimal("100"), position=position),
        path_point(1, Decimal("101.5"), position=position),
        path_point(1 + MAX_UNOBSERVED_MINUTES + 5, Decimal("98"), position=position),
    ]
    sim = simulate_stop_rule(
        position, points, ThresholdRule(position, _BE), Decimal("-50"), _RISK, Decimal("0")
    )

    assert sim.stopped and not sim.observable


# ---------------------------------------------------------------------
# MFE model
# ---------------------------------------------------------------------


def _winner_trade(pid, closed_offset_minutes=10, revert=False):
    position = _closing_at(
        closed_offset_minutes, exit_price=Decimal("104"), exit_reason="target"
    ).model_copy(update={"position_id": pid})
    prices = [100, 100.6, 101.2, 99.8 if revert else 101.5, 103, 104]
    return position, _dense(position, *prices)


def test_an_observation_is_recorded_per_level_reached_with_what_happened_after():
    position, points = _winner_trade("a", revert=True)
    observations = {o.level_pct: o for o in observations_for_trade(position, points)}

    assert set(observations) == {Decimal("0.5"), Decimal("1.0"), Decimal("2.0"), Decimal("3.0")}
    assert observations[Decimal("1.0")].reverted_to_entry
    assert observations[Decimal("1.0")].hit_target
    assert observations[Decimal("3.0")].further_mfe_pct >= 0


def test_an_observation_with_a_hole_after_the_crossing_is_dropped_not_guessed():
    position = _closing_at(300, exit_price=Decimal("104"), exit_reason="target")
    points = [
        path_point(0, Decimal("100"), position=position),
        path_point(1, Decimal("101.2"), position=position),
        path_point(200, Decimal("104"), position=position),
    ]
    assert observations_for_trade(position, points) == []


def test_zero_size_trades_contribute_nothing_to_the_mfe_model():
    position, points = _winner_trade("z")
    assert observations_for_trade(position.model_copy(update={"size": Decimal("0")}), points) == []


def test_the_model_only_uses_trades_closed_before_the_moment_and_needs_a_floor():
    observations = []
    for index in range(MIN_SAMPLES):
        # Closes stay within a few minutes of the last tick (index/10 min
        # apart), so every trade is fully observed.
        position, points = _winner_trade(f"t{index}", closed_offset_minutes=6 + index / 10)
        observations.extend(observations_for_trade(position, points))
    model = MfeModel(observations)

    assert model.estimate(Decimal("1.1")).status == "ESTIMATE"
    early = model.as_of(OPENED + timedelta(minutes=7))
    assert early.estimate(Decimal("1.1")).status == "INSUFFICIENT_DATA"
    assert model.estimate(Decimal("0.2")).status == "NO_FAVOURABLE_MOVE_YET"


# ---------------------------------------------------------------------
# Position decision (A profit protection + B thesis)
# ---------------------------------------------------------------------


def _features(**overrides):
    base = dict(
        minutes_in_trade=60.0, time_fraction=0.05, decay_score=Decimal("0.1"),
        progress_ratio=Decimal("0.2"), unrealized_pnl=Decimal("10"),
        mfe_pct_so_far=Decimal("1.2"), mae_pct_so_far=Decimal("-0.2"),
        giveback_ratio_so_far=Decimal("0.1"), minutes_since_mfe=5.0,
        distance_to_sl_pct=Decimal("5"), distance_to_target_pct=Decimal("8"), factors={},
    )
    base.update(overrides)
    return ThesisFeatures(**base)


class _Estimate:
    def __init__(self, status="ESTIMATE", p_revert=0.7):
        self.status = status
        self.p_revert_to_entry = p_revert

    def as_dict(self):
        return {"status": self.status, "p_revert_to_entry": self.p_revert_to_entry}


def test_a_strong_thesis_in_profit_holds_even_when_history_says_trades_revert():
    decision = decide_position(
        make_position(),
        _features(progress_ratio=Decimal("0.6")),
        _THRESHOLDS,
        _Estimate(p_revert=0.9),
    )
    assert decision.thesis_state == "STRONG"
    assert decision.action == "HOLD"


def test_a_valid_thesis_is_protected_only_when_history_says_this_level_reverts():
    reverting = decide_position(make_position(), _features(), _THRESHOLDS, _Estimate(p_revert=0.7))
    holding = decide_position(make_position(), _features(), _THRESHOLDS, _Estimate(p_revert=0.3))
    unknown = decide_position(
        make_position(), _features(), _THRESHOLDS, _Estimate("INSUFFICIENT_DATA")
    )

    assert reverting.action == "TIGHTEN_SL"
    assert reverting.proposed_stop_loss == Decimal("100")
    assert holding.action == "HOLD"
    assert unknown.action == "HOLD"
    assert unknown.profit_protection == "INSUFFICIENT_DATA"


def test_an_invalid_thesis_exits_and_a_weakening_one_in_profit_moves_to_break_even():
    invalid = decide_position(
        make_position(),
        _features(unrealized_pnl=Decimal("-5"), factors={
            "momentum_decay": 0.9, "volume_decay": 0.9,
        }),
        _THRESHOLDS,
        None,
    )
    weakening = decide_position(
        make_position(), _features(decay_score=Decimal("0.4")), _THRESHOLDS, None
    )

    assert invalid.action == "EXIT"
    assert weakening.thesis_state == "WEAKENING"
    assert weakening.action == "TIGHTEN_SL"
    assert weakening.proposed_stop_loss > make_position().stop_loss


# ---------------------------------------------------------------------
# Counterfactual engine v2
# ---------------------------------------------------------------------


def _by(results):
    return {r.policy: r for r in results}


def test_zero_size_positions_produce_no_counterfactual_rows():
    position = _closing_at(5, size=Decimal("0"), exit_price=Decimal("104"), exit_reason="target")
    assert run_counterfactuals(
        position, _dense(position, 100, 101, 104), _RISK, _THRESHOLDS, NOW, "r"
    ) == []


def test_no_intervention_is_unavailable_when_guardian_closed_the_trade():
    position = _closing_at(5, exit_price=Decimal("99"), exit_reason="guardian_exit")
    row = _by(run_counterfactuals(
        position, _dense(position, 100, 99.5, 99), _RISK, _THRESHOLDS, NOW, "r"
    ))["NO_INTERVENTION"]

    assert row.simulated_pnl_usdt is None
    assert row.delta_pnl_usdt is None
    assert row.detail["observation_status"] == "UNAVAILABLE"


def test_stop_policies_are_unobservable_across_holes_and_carry_no_delta():
    position = _closing_at(300, exit_price=Decimal("95"), exit_reason="stop_loss")
    points = [
        path_point(0, Decimal("100"), position=position),
        path_point(1, Decimal("101.5"), position=position),
        path_point(200, Decimal("99"), position=position),
    ]
    rows = _by(run_counterfactuals(position, points, _RISK, _THRESHOLDS, NOW, "r"))

    for policy in ("TIGHTEN_SL_AFTER_FAVORABLE", "PROFIT_LOCK_HALF_MFE"):
        assert rows[policy].detail["observation_status"] == "UNOBSERVABLE"
        assert rows[policy].delta_pnl_usdt is None
    assert rows["BASELINE"].detail["engine_version"] == 2


def test_the_position_policy_cannot_be_changed_by_prices_after_its_exit():
    position = _closing_at(20, exit_price=Decimal("96"), exit_reason="stop_loss")
    points = _dense(position, 100, 99.8, 99.5, 99.0)
    decaying = [
        path_point(p.minutes_in_trade, p.price, position=position, decay=Decimal("0.8"))
        for p in points
    ]
    pnl, detail = simulate_position_policy(
        position, decaying, _THRESHOLDS, None, Decimal("-40"), _RISK, Decimal("0")
    )
    mutated = decaying[:1] + [
        path_point(p.minutes_in_trade, Decimal("150"), position=position, decay=Decimal("0.8"))
        for p in decaying[1:]
    ]
    pnl_mutated, _ = simulate_position_policy(
        position, mutated, _THRESHOLDS, None, Decimal("-40"), _RISK, Decimal("0")
    )

    assert detail["actions"].get("EXIT") == 1
    assert pnl == pnl_mutated


# ---------------------------------------------------------------------
# Entry selection
# ---------------------------------------------------------------------


def _signal(cid, run, score, pnl=None, verdict="WAIT", minutes=0, theme="crypto_alt"):
    return EntrySignal(
        candidate_id=cid, instrument=f"{cid}-USDT", discovery_run_id=run,
        decided_at=OPENED + timedelta(minutes=minutes), quality_score=score,
        absolute_verdict=verdict, candidate_score=0.5, theme=theme,
        realized_pnl=None if pnl is None else Decimal(str(pnl)),
    )


def test_selection_takes_the_better_half_of_a_cohort_and_keeps_rejects():
    signals = [
        _signal("a", "r1", 0.6), _signal("b", "r1", 0.5), _signal("c", "r1", 0.4),
        _signal("d", "r1", 0.9, verdict="REJECT"), _signal("solo", "r2", 0.1),
    ]
    assign_selection(signals)
    by_id = {s.candidate_id: s for s in signals}

    assert by_id["d"].selection_verdict == "REJECT"
    assert by_id["d"].cohort_rank == 1
    assert by_id["a"].selection_verdict == "TRADE"
    assert by_id["c"].selection_verdict == "WAIT"
    assert by_id["solo"].selection_verdict == "TRADE"


def test_within_cohort_effects_need_a_scored_signal_on_each_side():
    signals = [
        _signal("a", "r1", 0.9, pnl=10), _signal("b", "r1", 0.1, pnl=-10),
        _signal("c", "r2", 0.9, pnl=5), _signal("d", "r2", 0.1),
    ]
    assign_selection(signals)
    effects = within_cohort_effects(signals)

    assert [value for _m, value in effects] == [20.0]


def test_signals_never_opened_are_unavailable_not_flat():
    signals = [_signal("a", "r1", 0.9, pnl=10), _signal("b", "r1", 0.1)]
    assign_selection(signals)
    result = evaluate_selection(signals, OPENED)

    assert result["scored_signals"] == 1
    assert result["unavailable_outcomes"] == 1


# ---------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------


def test_theme_mapping_is_deterministic():
    assert theme_of("NCSKMSTR2USD-USDT") == "tokenised_equity"
    assert theme_of("NCCO1OILBRENT2USD-USDT") == "commodity"
    assert theme_of("ETH-USDC") == "crypto_major"
    assert theme_of("PUMP-USDT") == "crypto_alt"


def test_the_theme_cap_counts_positions_already_open_and_only_subtracts():
    signals = [_signal(c, "r1", s) for c, s in (("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6))]
    assign_selection(signals)
    assign_portfolio_verdicts(signals, {"a": ["crypto_alt"] * (MAX_PER_THEME - 1)})
    verdicts = {s.candidate_id: s.portfolio_verdict for s in signals}

    assert verdicts == {"a": "KEEP", "b": "SKIP_CONCENTRATION", "c": "NOT_TAKEN", "d": "NOT_TAKEN"}


def test_outcome_dependence_needs_enough_cohorts():
    few = [_signal(f"x{i}", f"r{i // 2}", 0.5, pnl=i) for i in range(6)]
    assert cohort_outcome_dependence(few)["status"] == "INSUFFICIENT_DATA"


def test_outcome_dependence_detects_cohorts_that_win_and_lose_together():
    signals = []
    for run in range(8):
        value = 20 if run % 2 else -20
        signals += [_signal(f"{run}-{i}", f"r{run}", 0.5, pnl=value + i) for i in range(3)]
    result = cohort_outcome_dependence(signals)

    assert result["icc_pnl"] > 0.9
    assert result["effective_independent_bets_per_cohort"] < 1.2


def test_exposure_profile_tracks_peak_concurrent_notional():
    t0 = OPENED
    profile = exposure_profile([
        (t0, t0 + timedelta(hours=2), Decimal("1000"), "crypto_alt"),
        (t0 + timedelta(hours=1), t0 + timedelta(hours=3), Decimal("500"), "commodity"),
    ])
    assert Decimal(profile["peak_concurrent_notional_usdt"]) == Decimal("1500")


# ---------------------------------------------------------------------
# Policy registry
# ---------------------------------------------------------------------


def _evidence(**overrides):
    base = dict(
        policy_id="P", kind="POSITION", description="d", live_today=False, n=40,
        mean_effect=5.0, ci_low=1.0, ci_high=9.0, p_value=0.001, train_mean=4.0,
        test_mean=6.0, walk_forward_block_means=[3.0, 4.0, 5.0, 6.0], costs_included=True,
        pessimistic_mean=2.0, expectancy_change=5.0,
    )
    base.update(overrides)
    return PolicyEvidence(**base)


def test_every_gate_passing_validates_but_never_promotes_while_promotion_is_disabled():
    rows = evaluate_registry([_evidence()], {}, promotion_enabled=False)
    assert rows[0]["status"] == "VALIDATED"
    assert all(v == "PASS" for v in rows[0]["gates"].values())

    rows = evaluate_registry([_evidence()], {}, promotion_enabled=True)
    assert rows[0]["status"] == "CANARY"


def test_below_thirty_samples_nothing_but_insufficient_data_or_suspect():
    gates = evaluate_gates(_evidence(n=29), True)
    assert decide_status(_evidence(n=29), gates, True)[0] == "INSUFFICIENT_DATA"
    live = _evidence(n=20, live_today=True, mean_effect=-3.0)
    assert decide_status(live, evaluate_gates(live, False), False)[0] == "SUSPECT"


def test_robust_harm_fails_and_unproven_live_harm_is_suspect():
    harm = _evidence(mean_effect=-5.0, ci_low=-9.0, ci_high=-1.0, train_mean=-4.0, test_mean=-6.0)
    assert decide_status(harm, evaluate_gates(harm, True), True)[0] == "FAILED"
    unproven = _evidence(live_today=True, mean_effect=-2.0, ci_low=-9.0, ci_high=4.0)
    assert decide_status(unproven, evaluate_gates(unproven, False), False)[0] == "SUSPECT"


def test_a_win_rate_gain_that_lowers_expectancy_is_flagged_and_not_validated():
    trap = _evidence(win_rate_change=0.3, expectancy_change=-1.0, mean_effect=-1.0,
                     ci_low=-3.0, ci_high=1.0)
    status, flags = decide_status(trap, evaluate_gates(trap, False), False)
    assert "WIN_RATE_UP_EXPECTANCY_DOWN" in flags
    assert status != "VALIDATED"


def test_a_failing_walk_forward_or_train_test_blocks_validation():
    for broken in (_evidence(test_mean=-1.0),
                   _evidence(walk_forward_block_means=[3.0, -1.0, -2.0, 4.0])):
        rows = evaluate_registry([broken], {}, promotion_enabled=True)
        assert rows[0]["status"] == "NOISE"


def test_live_policies_move_only_through_rollback():
    assert next_status("CANARY", "NOISE", promotion_enabled=True) == "CANARY"
    assert rollback_check("CANARY", [-1.0] * 12) == "ROLLED_BACK"
    assert rollback_check("CANARY", [1.0] * 12) == "PROMOTED"
    assert rollback_check("PROMOTED", [1.0] * 3) is None
    assert rollback_check("NOISE", [-1.0] * 12) is None


def test_transitions_record_only_changes():
    rows = evaluate_registry([_evidence(), _evidence(policy_id="Q", n=5)], {"P": "VALIDATED"},
                             promotion_enabled=False)
    changes = transitions(rows, NOW)
    assert [(c["policy_id"], c["from_status"], c["to_status"]) for c in changes] == [
        ("Q", None, "INSUFFICIENT_DATA")
    ]
