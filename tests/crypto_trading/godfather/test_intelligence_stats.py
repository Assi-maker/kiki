"""Tests for crypto_trading/godfather/stats.py.

These are the primitives every "this is an edge" claim rests on, so they
are tested against values that can be checked by hand rather than against
whatever the implementation happens to return.
"""

from crypto_trading.godfather import stats


def test_wilson_interval_brackets_the_point_estimate_and_stays_inside_zero_one():
    interval = stats.wilson_interval(7, 10)
    assert interval is not None
    assert 0.0 <= interval.lower < 0.7 < interval.upper <= 1.0


def test_wilson_interval_never_leaves_the_unit_range_at_the_extremes():
    """The exact case the normal approximation gets wrong, and the reason
    Wilson was chosen: 10/10 successes must not produce an upper bound
    above 1 or a lower bound above the data."""
    interval = stats.wilson_interval(10, 10)
    assert interval is not None
    assert interval.upper == 1.0
    assert 0.0 < interval.lower < 1.0


def test_wilson_interval_returns_none_for_degenerate_input():
    assert stats.wilson_interval(0, 0) is None
    assert stats.wilson_interval(5, 3) is None


def test_binomial_test_is_exact_for_a_hand_checkable_case():
    # P(X in {0,1,2,8,9,10}) for n=10, p=0.5 is 2*(1+10+45)/1024 = 0.109375
    assert stats.binomial_test_two_sided(8, 10, 0.5) == 0.109375


def test_binomial_test_of_the_null_itself_is_one():
    assert stats.binomial_test_two_sided(50, 100, 0.5) == 1.0


def test_binomial_test_returns_one_for_an_empty_sample():
    """A pattern with no samples is not evidence of anything, and must
    never be able to produce a significant p-value."""
    assert stats.binomial_test_two_sided(0, 0, 0.5) == 1.0


def test_benjamini_hochberg_rejects_only_the_small_p_values():
    passed = stats.benjamini_hochberg([0.001, 0.2, 0.7, 0.9], q=0.10)
    assert passed == [True, False, False, False]


def test_benjamini_hochberg_is_stricter_than_an_uncorrected_alpha():
    """The whole point of the correction: a p-value that would clear a
    naive 0.05 test must NOT automatically clear FDR control when many
    hypotheses were tested alongside it."""
    p_values = [0.04] + [0.9] * 40
    passed = stats.benjamini_hochberg(p_values, q=0.10)
    assert passed[0] is False


def test_benjamini_hochberg_handles_an_empty_sweep():
    assert stats.benjamini_hochberg([], q=0.10) == []


def test_bootstrap_mean_ci_is_deterministic_for_the_same_seed():
    values = [1.0, -2.0, 3.0, -1.0, 5.0, 0.5, -0.2, 2.0]
    first = stats.bootstrap_mean_ci(values, seed=1)
    second = stats.bootstrap_mean_ci(values, seed=1)
    assert first == second


def test_bootstrap_mean_ci_brackets_the_sample_mean():
    values = [3.0] * 20 + [4.0] * 20
    interval = stats.bootstrap_mean_ci(values)
    assert interval is not None
    assert interval.lower <= 3.5 <= interval.upper


def test_bootstrap_mean_ci_excludes_zero_only_when_the_data_is_one_sided():
    positive = stats.bootstrap_mean_ci([5.0] * 40)
    mixed = stats.bootstrap_mean_ci([5.0, -5.0] * 20)
    assert positive is not None and positive.excludes_zero_above()
    assert mixed is not None and not mixed.excludes_zero_above()


def test_bootstrap_mean_ci_needs_at_least_two_samples():
    assert stats.bootstrap_mean_ci([1.0]) is None


def test_split_halves_puts_the_extra_sample_in_the_recent_half():
    first, second = stats.split_halves([1, 2, 3, 4, 5])
    assert first == [1, 2]
    assert second == [3, 4, 5]
