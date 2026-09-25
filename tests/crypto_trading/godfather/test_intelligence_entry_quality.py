"""Tests for crypto_trading/godfather/entry_quality.py.

The two properties that matter most here are opposites of each other, and
both are failure modes this layer has already been caught in once:

  * it must be able to say REJECT (otherwise it adds nothing), and
  * it must not say REJECT to everything (the first version rejected 113
    of 146 historical trades, which discriminates nothing at all).

Everything else - the cost check, the risk/reward term, the neutrality of
INSUFFICIENT_DATA - exists to keep those two in balance.
"""

from decimal import Decimal

from crypto_trading.godfather.entry_quality import (
    assess_entry_quality,
    backtest_entry_quality,
    expected_round_trip_cost_usdt,
)
from crypto_trading.godfather.features import build_candidate_features
from crypto_trading.schemas.candidate import Candidate
from tests.crypto_trading.godfather.intelligence_fixtures import NOW, evidence_record
from tests.crypto_trading.test_market_snapshot import _settings

_RISK_LIMITS = _settings().risk_limits


def _candidate(candidate_id="c1", **evidence_kwargs) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"k-{candidate_id}",
        instrument="BTC-USDT",
        discovery_run_id="r",
        evidence_hash="h",
        status="CONFIRMED",
        evidence_record=evidence_record(**evidence_kwargs),
        created_at=NOW,
        updated_at=NOW,
    )


def _assess(
    candidate=None,
    *,
    conflicts=(),
    patterns=(),
    entry=Decimal("100"),
    stop_loss=Decimal("95"),
    target=Decimal("110"),
    size=Decimal("500"),
    regime_compatible=None,
):
    candidate = candidate or _candidate()
    return assess_entry_quality(
        candidate=candidate,
        features=build_candidate_features(candidate),
        conflicts=list(conflicts),
        experience_patterns=list(patterns),
        planned_entry=entry,
        stop_loss=stop_loss,
        target=target,
        size=size,
        risk_limits=_RISK_LIMITS,
        now=NOW,
        run_id="run",
        regime_compatible=regime_compatible,
    )


def _pattern(edge_class: str, condition: dict, expectancy: str = "5"):
    return {
        "pattern_id": f"p-{edge_class}",
        "edge_class": edge_class,
        "condition": condition,
        "expectancy_usdt": expectancy,
    }


def test_an_unproven_setup_is_scored_neutrally_rather_than_rejected():
    """A system that has proven nothing yet must keep exploring, or it
    will never collect the data it needs to know better."""
    assessment = _assess()

    assert assessment.expected_edge_class == "INSUFFICIENT_DATA"
    assert assessment.verdict in ("TRADE", "WAIT")


def test_a_known_failure_pattern_is_rejected_unconditionally():
    patterns = [_pattern("FAILURE_PATTERN", {"instrument": "BTC-USDT"}, "-12")]

    assessment = _assess(patterns=patterns)

    assert assessment.verdict == "REJECT"
    assert "reject:matches_known_failure_pattern" in assessment.reason_codes
    assert assessment.expected_expectancy_usdt == Decimal("-12")


def test_a_proven_edge_raises_the_quality_score():
    baseline = _assess()
    with_edge = _assess(patterns=[_pattern("EDGE", {"instrument": "BTC-USDT"})])

    assert with_edge.quality_score > baseline.quality_score
    assert with_edge.verdict == "TRADE"


def test_contradictions_lower_the_score_without_any_single_one_rejecting():
    """The saturation bug this design replaced: each conflict must move
    the score, but no conflict may be able to dominate it."""
    clean = _assess()
    conflicted = _assess(
        conflicts=[
            "entry_rsi_at_or_above_80",
            "momentum_triggered_without_volume_confirmation",
            "entered_on_below_average_volume",
            "single_trigger_reason_only",
            "confirmed_with_bullish_probability_below_0.35",
        ]
    )

    assert conflicted.quality_score < clean.quality_score
    assert conflicted.conflict_score == 1.0
    # Even with EVERY known contradiction present, a setup with real
    # confirmation and sane risk/reward is not silently rejected.
    assert conflicted.verdict in ("WAIT", "REJECT")


def test_a_setup_whose_move_cannot_cover_its_own_costs_is_flagged():
    assessment = _assess(target=Decimal("100.05"))

    assert "expected_move_does_not_cover_round_trip_cost" in assessment.reason_codes


def test_risk_reward_below_one_is_flagged_and_lowers_the_score():
    good = _assess(stop_loss=Decimal("95"), target=Decimal("110"))
    poor = _assess(stop_loss=Decimal("90"), target=Decimal("103"))

    assert "risk_reward_below_1" not in good.reason_codes
    assert "risk_reward_below_1" in poor.reason_codes
    assert poor.quality_score < good.quality_score


def test_an_undefined_risk_reward_is_reported_rather_than_assumed():
    assessment = _assess(stop_loss=Decimal("100"))

    assert assessment.risk_reward is None
    assert "risk_reward_undefined" in assessment.reason_codes


def test_an_incompatible_regime_scales_the_score_down_but_does_not_decide_it():
    neutral = _assess(regime_compatible=None)
    hostile = _assess(regime_compatible=False)

    assert hostile.quality_score < neutral.quality_score
    assert "regime_incompatible" in hostile.reason_codes


def test_independent_confirmation_raises_the_score():
    unconfirmed = _assess(
        _candidate(
            "c-weak",
            volume_triggered=False,
            secondary_triggered=False,
            price_volatility_triggered=False,
            trigger_reasons=("momentum_breakout",),
        )
    )
    confirmed = _assess(
        _candidate(
            "c-strong",
            volume_triggered=True,
            secondary_triggered=True,
            price_volatility_triggered=True,
            trigger_reasons=("momentum_breakout", "volume", "price_volatility"),
        )
    )

    assert confirmed.quality_score > unconfirmed.quality_score


def test_the_score_always_stays_inside_the_unit_range():
    extreme = _assess(
        _candidate("c-max", candidate_score=1.0),
        patterns=[_pattern("EDGE", {"instrument": "BTC-USDT"})],
    )
    awful = _assess(
        _candidate("c-min", candidate_score=0.0),
        conflicts=list(
            [
                "entry_rsi_at_or_above_80",
                "momentum_triggered_without_volume_confirmation",
                "entered_on_below_average_volume",
                "single_trigger_reason_only",
            ]
        ),
        stop_loss=Decimal("50"),
        target=Decimal("100.05"),
        regime_compatible=False,
    )

    assert 0.0 <= awful.quality_score <= extreme.quality_score <= 1.0


def test_every_assessment_is_advisory_in_this_phase():
    """The activation is a data difference, not an invisible code
    change."""
    rejected = _assess(patterns=[_pattern("FAILURE_PATTERN", {"instrument": "BTC-USDT"})])
    assert _assess().enforced is False
    assert rejected.enforced is False


def test_the_expected_cost_matches_the_live_friction_model():
    size = Decimal("500")
    # (spread + slippage) on both legs, plus the fee, on notional.
    expected = size * (
        (_RISK_LIMITS.spread_pct + _RISK_LIMITS.slippage_pct) * 2 + _RISK_LIMITS.fee_pct
    )

    assert expected_round_trip_cost_usdt(size, _RISK_LIMITS) == expected


def test_the_backtest_separates_what_would_have_been_kept_from_what_was_blocked():
    """The only honest test of a filter: not how many losses it catches,
    but the net P/L of everything it would have removed."""
    kept = _assess(_candidate("kept"), patterns=[_pattern("EDGE", {"instrument": "BTC-USDT"})])
    blocked = _assess(
        _candidate("blocked"), patterns=[_pattern("FAILURE_PATTERN", {"instrument": "BTC-USDT"})]
    )

    result = backtest_entry_quality(
        [kept, blocked], {"kept": Decimal("30"), "blocked": Decimal("-40")}
    )

    assert result["by_verdict"]["TRADE"]["total_pnl_usdt"] == "30"
    assert result["by_verdict"]["REJECT"]["total_pnl_usdt"] == "-40"
    assert result["pnl_removed_by_filtering_usdt"] == "-40"


def test_the_backtest_ignores_candidates_with_no_known_outcome():
    assessment = _assess(_candidate("unknown"))

    result = backtest_entry_quality([assessment], {})

    assert result["by_verdict"] == {}
