"""Config surface for the GODFATHER Intelligence Layer (2026-09-25).

Two things are pinned here. The analysis tick ships ENABLED - which is a
deliberate exception to this codebase's "ships inert" rule, justified by
the isolation proof in
tests/crypto_trading/godfather/test_intelligence_isolation.py: this layer
cannot reach a trading decision, so the worst a bug in it can do is
produce a wrong report. The two flags that WOULD let its conclusions
reach a trade ship inert in the usual way, and must stay that way until a
separate, explicitly approved activation.
"""

from crypto_trading.config.loader import GodfatherConfig, get_settings


def test_the_analysis_tick_ships_enabled():
    assert get_settings().godfather.intelligence_enabled is True


def test_both_enforcement_surfaces_ship_inert():
    godfather = get_settings().godfather

    assert godfather.entry_quality_enforcement_enabled is False
    assert godfather.thesis_enforcement_enabled is False


def test_the_enforcement_defaults_are_false_in_the_model_too():
    """So a config file that simply omits them can never accidentally
    activate an enforcement surface."""
    defaults = GodfatherConfig()

    assert defaults.entry_quality_enforcement_enabled is False
    assert defaults.thesis_enforcement_enabled is False


def test_the_priority_boost_overlay_is_untouched_by_this_plan():
    assert get_settings().godfather.priority_boost_enabled is False


def test_the_experience_gates_default_to_the_conservative_values():
    """The direction of danger: LOWERING the sample floor or RAISING the
    false-discovery rate makes GODFATHER more willing to believe a
    pattern is real. 30 is the same floor guardian/authority.py already
    enforces for live heuristics."""
    godfather = get_settings().godfather

    assert godfather.experience_min_sample_size == 30
    assert godfather.experience_min_support == 8
    assert godfather.experience_fdr_q == 0.10


def test_the_tick_cadence_and_batch_sizes_are_configurable_and_positive():
    godfather = get_settings().godfather

    assert godfather.intelligence_check_interval_seconds > 0
    assert godfather.intelligence_batch_limit > 0
    assert godfather.entry_quality_backfill_limit >= 0
