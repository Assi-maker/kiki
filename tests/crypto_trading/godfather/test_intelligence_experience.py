"""Tests for crypto_trading/godfather/experience.py - the anti-noise contract.

This is the file that decides whether GODFATHER is allowed to believe
something. Every edge class is driven directly, with hand-built evidence,
rather than hoping production eventually produces one of each - an
unreachable branch in a classifier is a branch that silently never fires,
and here that would mean either never learning or, far worse, calling
noise an edge.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather import stats
from crypto_trading.godfather.experience import (
    ExperienceConfig,
    ExperienceSample,
    PatternDefinition,
    _PatternStats,
    build_experience_memory,
    classify_edge,
    enumerate_patterns,
    lookup_edge_class,
)

_NOW = datetime(2026, 9, 25, tzinfo=UTC)
_CONFIG = ExperienceConfig()


def _sample(index: int, pnl: str, features: dict, regime: str = "btc_ok") -> ExperienceSample:
    return ExperienceSample(
        position_id=f"p{index}",
        closed_at=_NOW + timedelta(hours=index),
        pnl=Decimal(pnl),
        mfe_pct=Decimal("1"),
        mae_pct=Decimal("-1"),
        minutes_to_mfe=30.0,
        regime=regime,
        features=features,
    )


def _pattern_stats(
    *,
    n: int,
    lift: str | None,
    first_half_lift: str | None,
    second_half_lift: str | None,
    ci: tuple[float, float] | None,
    wilson: tuple[float, float] | None,
    walk_forward_lift: str | None,
    walk_forward_n: int,
    regime_breakdown: dict | None = None,
    p_value: float = 0.001,
) -> _PatternStats:
    """A hand-built evidence file, so each classification branch can be
    driven on its own terms instead of through a fixture that happens to
    hit it."""
    definition = PatternDefinition(family="f", key="f=x", condition={"f": "x"})
    matched = [_sample(i, "1", {"f": "x"}) for i in range(n)]
    return _PatternStats(
        definition=definition,
        matched=matched,
        win_count=n,
        win_rate=1.0,
        wilson=stats.Interval(*wilson) if wilson else None,
        expectancy=Decimal("1"),
        expectancy_ci=stats.Interval(*ci) if ci else None,
        p_value=p_value,
        lift=Decimal(lift) if lift is not None else None,
        first_half_lift=Decimal(first_half_lift) if first_half_lift is not None else None,
        second_half_lift=Decimal(second_half_lift) if second_half_lift is not None else None,
        regime_breakdown=regime_breakdown or {},
        walk_forward_lift=(
            Decimal(walk_forward_lift) if walk_forward_lift is not None else None
        ),
        walk_forward_n=walk_forward_n,
    )


def _edge_candidate(**overrides) -> _PatternStats:
    base = dict(
        n=40,
        lift="5",
        first_half_lift="4",
        second_half_lift="6",
        ci=(1.0, 9.0),
        wilson=(0.7, 0.99),
        walk_forward_lift="5",
        walk_forward_n=12,
    )
    base.update(overrides)
    return _pattern_stats(**base)


# ---------------------------------------------------------------------
# classify_edge: every branch, driven directly
# ---------------------------------------------------------------------


def test_a_small_sample_is_insufficient_data_no_matter_how_good_it_looks():
    """The user's explicit requirement. A pattern that wins every one of
    its 12 trades is still not an edge, and must not be reported as a
    weak one either."""
    pattern = _edge_candidate(n=12)

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "INSUFFICIENT_DATA"


def test_a_pattern_that_fails_fdr_correction_is_noise():
    pattern = _edge_candidate()

    edge_class, _survived = classify_edge(pattern, False, 0.4, _CONFIG)

    assert edge_class == "NOISE"


def test_a_pattern_clearing_every_gate_is_an_edge():
    pattern = _edge_candidate()

    edge_class, survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "EDGE"
    assert survived is True


def test_an_edge_is_downgraded_when_its_expectancy_interval_includes_zero():
    """Win rate is not profit. A pattern that wins often but whose
    expectancy interval straddles zero has not been shown to make
    money."""
    pattern = _edge_candidate(ci=(-2.0, 9.0))

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "WEAK_EDGE"


def test_an_edge_is_downgraded_when_its_win_rate_does_not_beat_the_baseline():
    pattern = _edge_candidate(wilson=(0.35, 0.8))

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "WEAK_EDGE"


def test_an_edge_is_downgraded_when_it_did_not_survive_walk_forward():
    pattern = _edge_candidate(walk_forward_lift="-3")

    edge_class, survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert survived is False
    assert edge_class == "WEAK_EDGE"


def test_an_edge_is_downgraded_when_the_holdout_is_too_small_to_judge():
    pattern = _edge_candidate(walk_forward_n=3)

    edge_class, survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert survived is False
    assert edge_class == "WEAK_EDGE"


def test_a_pattern_that_worked_and_then_stopped_is_a_decaying_edge():
    pattern = _edge_candidate(first_half_lift="12", second_half_lift="-2")

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "DECAYING_EDGE"


def test_a_pattern_that_only_works_in_one_regime_is_regime_dependent():
    breakdown = {
        "btc_strong": {"n": 20, "lift": "9"},
        "btc_bad": {"n": 20, "lift": "-7"},
    }
    pattern = _edge_candidate(
        ci=(-2.0, 9.0), regime_breakdown=breakdown, first_half_lift="4", second_half_lift="6"
    )

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "REGIME_DEPENDENT"


def test_regime_splits_are_ignored_when_a_regime_has_too_few_samples():
    breakdown = {
        "btc_strong": {"n": 38, "lift": "9"},
        "btc_bad": {"n": 2, "lift": "-7"},
    }
    pattern = _edge_candidate(ci=(-2.0, 9.0), regime_breakdown=breakdown)

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "WEAK_EDGE"


def test_a_robustly_losing_pattern_is_a_failure_pattern():
    """The most immediately useful class: a reliable way to lose money is
    cheaper to avoid than a way to make it is to find."""
    pattern = _edge_candidate(
        lift="-6", first_half_lift="-5", second_half_lift="-7", walk_forward_lift="-6"
    )

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "FAILURE_PATTERN"


def test_an_inconsistent_negative_pattern_is_noise_not_a_failure_pattern():
    pattern = _edge_candidate(lift="-6", first_half_lift="3", second_half_lift="-15")

    edge_class, _survived = classify_edge(pattern, True, 0.4, _CONFIG)

    assert edge_class == "NOISE"


# ---------------------------------------------------------------------
# build_experience_memory: the whole sweep over real-shaped samples
# ---------------------------------------------------------------------


def _population() -> list[ExperienceSample]:
    """60 losing trades and 40 winning ones, separated by one feature,
    plus an uncorrelated coin-flip feature and a rare feature."""
    samples: list[ExperienceSample] = []
    for index in range(60):
        samples.append(
            _sample(index, "-10", {"g": "no", "coin": "a" if index % 2 else "b"})
        )
    for index in range(60, 100):
        samples.append(
            _sample(index, "20", {"g": "yes", "coin": "a" if index % 2 else "b"})
        )
    for index in range(100, 110):
        samples.append(_sample(index, "5", {"g": "rare", "coin": "a"}))
    return samples


def _by_id(patterns):
    return {pattern.pattern_id: pattern for pattern in patterns}


def test_a_strongly_separating_feature_is_found_as_an_edge():
    patterns = _by_id(build_experience_memory(_population(), _NOW, "run"))

    assert patterns["g:g=yes"].edge_class == "EDGE"
    assert patterns["g:g=yes"].sample_size == 40
    assert patterns["g:g=yes"].fdr_significant is True
    assert patterns["g:g=yes"].confidence > 0.0


def test_the_mirror_image_of_an_edge_is_recorded_as_a_failure_pattern():
    patterns = _by_id(build_experience_memory(_population(), _NOW, "run"))

    assert patterns["g:g=no"].edge_class == "FAILURE_PATTERN"
    assert Decimal(str(patterns["g:g=no"].lift_expectancy_usdt)) < 0


def test_an_uncorrelated_feature_is_reported_as_noise():
    patterns = _by_id(build_experience_memory(_population(), _NOW, "run"))

    assert patterns["coin:coin=a"].edge_class == "NOISE"
    assert patterns["coin:coin=a"].confidence == 0.0


def test_a_rare_feature_value_is_insufficient_data_not_a_weak_edge():
    patterns = _by_id(build_experience_memory(_population(), _NOW, "run"))

    assert patterns["g:g=rare"].edge_class == "INSUFFICIENT_DATA"
    assert patterns["g:g=rare"].confidence == 0.0


def test_the_sweep_records_how_many_hypotheses_were_tested_alongside_each_pattern():
    """Significance is a property of the pattern AND of the sweep it was
    found in, so the sweep size is stored with every verdict."""
    patterns = build_experience_memory(_population(), _NOW, "run")

    assert all(p.detail["hypotheses_tested_in_sweep"] >= 1 for p in patterns)
    assert all(p.detail["fdr_q"] == _CONFIG.fdr_q for p in patterns)


def test_values_below_minimum_support_are_not_even_enumerated():
    samples = [_sample(i, "1", {"x": "common"}) for i in range(20)]
    samples += [_sample(100 + i, "1", {"x": "unique"}) for i in range(2)]

    keys = {p.key for p in enumerate_patterns(samples, _CONFIG)}

    assert "x=common" in keys
    assert "x=unique" not in keys


def test_an_empty_history_produces_no_patterns_rather_than_empty_claims():
    assert build_experience_memory([], _NOW, "run") == []


def test_raising_the_sample_floor_turns_a_would_be_edge_into_insufficient_data():
    """Direction-of-safety check: the config knob that makes the system
    more credulous is the one that lowers this floor."""
    strict = ExperienceConfig(min_sample_size=200)

    patterns = _by_id(build_experience_memory(_population(), _NOW, "run", strict))

    assert patterns["g:g=yes"].edge_class == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------
# lookup_edge_class: what Experience Memory tells a new candidate
# ---------------------------------------------------------------------


def _row(pattern_id: str, edge_class: str, condition: dict, expectancy: str = "5"):
    return {
        "pattern_id": pattern_id,
        "edge_class": edge_class,
        "condition": condition,
        "expectancy_usdt": expectancy,
    }


def test_lookup_returns_insufficient_data_when_nothing_applies():
    edge_class, expectancy, matched = lookup_edge_class([], {"g": "yes"})

    assert edge_class == "INSUFFICIENT_DATA"
    assert expectancy is None
    assert matched == []


def test_lookup_prefers_a_known_failure_pattern_over_a_known_edge():
    """Asymmetric on purpose: a proven way to lose money outranks a
    proven way to make it, because avoiding the first is the cheaper and
    more reliable of the two."""
    rows = [
        _row("a", "EDGE", {"g": "yes"}),
        _row("b", "FAILURE_PATTERN", {"h": "bad"}, "-9"),
    ]

    edge_class, expectancy, matched = lookup_edge_class(rows, {"g": "yes", "h": "bad"})

    assert edge_class == "FAILURE_PATTERN"
    assert expectancy == Decimal("-9")
    assert set(matched) == {"a", "b"}


def test_lookup_ignores_noise_and_insufficient_data_rows():
    rows = [
        _row("a", "NOISE", {"g": "yes"}),
        _row("b", "INSUFFICIENT_DATA", {"g": "yes"}),
    ]

    edge_class, _expectancy, matched = lookup_edge_class(rows, {"g": "yes"})

    assert edge_class == "INSUFFICIENT_DATA"
    assert matched == []


def test_lookup_is_fail_closed_on_a_feature_the_candidate_does_not_have():
    rows = [_row("a", "EDGE", {"missing_feature": "x"})]

    edge_class, _expectancy, matched = lookup_edge_class(rows, {"g": "yes"})

    assert edge_class == "INSUFFICIENT_DATA"
    assert matched == []
