from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import (
    GuardianConfig,
    ProfitProtectionExperimentConfig,
    RiskLimitsConfig,
    Settings,
)
from crypto_trading.paper_trading.execution import compute_fees, compute_fill_price, compute_pnl
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _guardian_state_for,
    advance_shadow,
    run_profit_protection_experiment_tick,
    seed_shadows_for_position,
)
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_frozen_thresholds_are_exactly_one_and_one_half_percent():
    assert FROZEN_THRESHOLDS_PCT == (Decimal("0.010"), Decimal("0.015"))


def test_guardian_state_lookup_returns_none_when_assisted_exit_disabled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=False)
    assert _guardian_state_for(repo, "pos-1", _NOW, config) is None


def test_guardian_state_lookup_returns_none_with_no_observation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=True, check_interval_seconds=60)
    assert _guardian_state_for(repo, "pos-1", _NOW, config) is None


def test_guardian_state_lookup_returns_state_when_fresh(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=True, check_interval_seconds=60)
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id="obs-1", position_id="pos-1", observed_at=_NOW - timedelta(seconds=30),
            state="EXIT", decay_score=Decimal("0.9"), progress_ratio=Decimal("0"),
            unrealized_pnl=Decimal("0"), factors={}, run_id="run-1",
        )
    )
    assert _guardian_state_for(repo, "pos-1", _NOW, config) == "EXIT"


def test_guardian_state_lookup_returns_none_when_stale(tmp_path):
    """Same 2x check_interval_seconds staleness limit as
    position_closing.py::close_triggered_positions - proven identical by
    this and the previous test using the exact same boundary math."""
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=True, check_interval_seconds=60)
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id="obs-1", position_id="pos-1",
            observed_at=_NOW - timedelta(seconds=121),  # > 2 * 60s
            state="EXIT", decay_score=Decimal("0.9"), progress_ratio=Decimal("0"),
            unrealized_pnl=Decimal("0"), factors={}, run_id="run-1",
        )
    )
    assert _guardian_state_for(repo, "pos-1", _NOW, config) is None


def _position(position_id="pos-1", opened_at=_NOW, instrument="BTCUSDT") -> Position:
    return Position(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("5000"), fill_model_version="v1",
        opened_at=opened_at,
    )


def test_seed_shadows_for_position_creates_one_row_per_frozen_threshold(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    shadows = repo.find_all_profit_protection_shadows()
    assert {s["threshold_pct"] for s in shadows} == {"0.010", "0.015"}
    assert all(s["position_id"] == "pos-1" for s in shadows)


def test_seed_shadows_for_position_computes_correct_threshold_price(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert Decimal(row["threshold_price"]) == Decimal("50500")  # 50000 * 1.010


def test_seed_shadows_for_position_never_seeds_before_activation_watermark(tmp_path):
    """Spec G6 / plan correction C1 - a position opened strictly before the
    watermark is permanently excluded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    activated_at = _NOW
    early_position = _position(opened_at=_NOW - timedelta(seconds=1))
    seed_shadows_for_position(repo, early_position, activated_at=activated_at, now=_NOW)
    assert repo.find_all_profit_protection_shadows() == []


def test_seed_shadows_for_position_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    assert len(repo.find_all_profit_protection_shadows()) == 2


def _shadow_row(repo, **overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", entry_price=Decimal("50000"),
        original_stop_loss=Decimal("49000"), target=Decimal("52000"),
        threshold_price=Decimal("50500"), opened_at=_NOW, created_at=_NOW,
    )
    defaults.update(overrides)
    repo.seed_profit_protection_shadow(**defaults)
    return repo.get_profit_protection_shadow(defaults["shadow_id"])


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"), risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5, max_total_exposure_pct=Decimal("1.0"),
        max_position_notional_usdt=Decimal("1000000"), spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"), fee_pct=Decimal("0.0004"), max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


def _seed_real_position(repo, **overrides) -> None:
    from crypto_trading.schemas.event import Event
    position = _position(**overrides)
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{position.position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position.position_id,
            occurred_at=position.opened_at, run_id="seed", schema_version=1, payload={},
        ),
    )


def test_advance_shadow_does_not_move_sl_when_threshold_not_reached(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("50000"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "OPEN"
    assert row["threshold_reached"] == 0
    assert row["breakeven_stop_loss"] is None


def test_advance_shadow_activates_breakeven_starting_next_tick_only(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    # Tick 1: threshold touched (candle_high 50600 >= 50500), no stop/target hit
    advance_shadow(
        shadow, candle_low=Decimal("50100"), candle_high=Decimal("50600"),
        current_price=Decimal("50400"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    after_tick_1 = repo.get_profit_protection_shadow("pos-1:0.010")
    assert after_tick_1["threshold_reached"] == 1
    assert after_tick_1["breakeven_stop_loss"] == "50000"
    assert after_tick_1["status"] == "OPEN"  # never closed same tick it activated

    # Tick 2: price drops to exactly breakeven - now the active SL, closes here
    tick_2_time = _NOW + timedelta(minutes=1)
    advance_shadow(
        after_tick_1, candle_low=Decimal("49900"), candle_high=Decimal("50200"),
        current_price=Decimal("50000"), funding_rate=Decimal("0"), now=tick_2_time,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    after_tick_2 = repo.get_profit_protection_shadow("pos-1:0.010")
    assert after_tick_2["status"] == "CLOSED"
    assert after_tick_2["exit_reason"] == "stop_loss"


def test_advance_shadow_same_candle_threshold_and_stop_resolves_stop_first(tmp_path):
    """Spec G8: a candle whose low <= original SL (49000) and whose high
    also >= threshold (50500) must be resolved as the stop firing first -
    threshold is never considered reached on a candle that also breached
    the pre-existing SL."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("48900"), candle_high=Decimal("50600"),
        current_price=Decimal("49500"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "stop_loss"
    assert row["threshold_reached"] == 0  # never got the chance - stop fired first


def test_advance_shadow_time_limit_fires_when_hold_hours_exceeded(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    later = _NOW + timedelta(hours=25)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("49800"), funding_rate=Decimal("0"), now=later,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "time_limit"
    assert row["theoretical_exit"] == "49800"  # current_price, not candle_high/low


def test_advance_shadow_guardian_exit_fires_only_when_enabled_and_state_is_exit(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("49900"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state="EXIT",
        guardian_assisted_exit_enabled=True, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "guardian_exit"


def test_advance_shadow_guardian_exit_state_never_closes_when_disabled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("49900"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state="EXIT",
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "OPEN"


def test_advance_shadow_updates_mfe_and_mae_before_any_exit_check(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49700"), candle_high=Decimal("50300"),
        current_price=Decimal("50000"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["mfe"] == "300"   # 50300 - 50000
    assert row["mae"] == "-300"  # 49700 - 50000


def test_advance_shadow_target_gap_closes_at_target_not_candle_high(tmp_path):
    """Review finding 1a: the target branch had zero coverage. A candle that
    gaps strictly past target (52000) to 52300 must still theoretically-exit
    at target itself, never at the gapped candle_high - the conservative
    min() formula, not "whatever price the candle happened to touch"."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("51000"), candle_high=Decimal("52300"),
        current_price=Decimal("52200"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "target"
    assert row["theoretical_exit"] == "52000"  # min(52300, 52000) - never the gapped price


def test_advance_shadow_stop_loss_gap_uses_actual_candle_low_not_sl_level(tmp_path):
    """Review finding 1b: every prior stop_loss test had candle_low within a
    tick of the SL, so min(candle_low, active_sl) always degenerated to the
    same value regardless of which operand "won". This pins the ACTUAL
    behavior of that formula for a genuine gap-through: the stop_loss branch
    only ever fires when candle_low <= active_sl, so min(candle_low,
    active_sl) is mathematically forced to equal candle_low (the worse,
    more pessimistic price actually touched this candle) whenever the gap is
    strict - never the SL level itself. This mirrors the target branch's own
    forced resolution to `target` (never the gapped candle_high) - in both
    cases the formula picks the value less favorable to the shadow's
    reported outcome: the worse price for a loss, the capped price for a
    win. Verified directly: min(Decimal('48500'), Decimal('49000')) ==
    Decimal('48500')."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("48500"), candle_high=Decimal("49800"),
        current_price=Decimal("48700"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "stop_loss"
    assert row["theoretical_exit"] == "48500"  # min(48500, 49000) == candle_low, not the SL level


class _SpyRepo:
    """Review finding 2: a minimal spy proving the MECHANISM (threshold-
    detection code is never even reached this tick), not just the outcome -
    the real repo's own `WHERE status = 'OPEN'` guard on
    activate_profit_protection_breakeven would silently reject that write
    anyway once the row is CLOSED, so an outcome-only assertion can't tell
    the difference between "never called" and "called but rejected"."""

    def __init__(self, real_position):
        self._real_position = real_position
        self.activate_calls = 0

    def record_profit_protection_tick(self, shadow_id, mfe, mae, updated_at):
        pass

    def activate_profit_protection_breakeven(
        self, shadow_id, breakeven_stop_loss, threshold_reached_at, updated_at
    ):
        self.activate_calls += 1

    def close_profit_protection_shadow(self, **kwargs):
        pass

    def get_position(self, position_id):
        return self._real_position


def test_advance_shadow_same_candle_never_calls_activate_breakeven(tmp_path):
    """Review finding 2: directly proves the mandated `return` after closing
    means threshold-detection code is never reached this tick - independent
    of whatever the real repository's own guard would have done."""
    real_repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(real_repo)
    shadow = _shadow_row(real_repo)
    real_position = real_repo.get_position("pos-1")
    spy = _SpyRepo(real_position)
    advance_shadow(
        shadow, candle_low=Decimal("48900"), candle_high=Decimal("50600"),
        current_price=Decimal("49500"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=spy,
    )
    assert spy.activate_calls == 0


def test_advance_shadow_close_is_a_noop_when_real_position_is_missing(tmp_path):
    """Review finding 3: _close_shadow must never let an unhandled
    AttributeError escape when repo.get_position() returns None (defensive -
    in practice positions are never deleted, but the experiment must not be
    able to abort other shadows' processing in the same tick)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    # Deliberately do NOT seed a real position for "pos-missing" - the
    # shadow references a position_id with no row in `positions`.
    shadow = _shadow_row(
        repo, shadow_id="pos-missing:0.010", position_id="pos-missing"
    )
    advance_shadow(
        shadow, candle_low=Decimal("48900"), candle_high=Decimal("50600"),
        current_price=Decimal("49500"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )  # must not raise
    row = repo.get_profit_protection_shadow("pos-missing:0.010")
    assert row["status"] == "OPEN"


def test_shadow_realized_pnl_matches_compute_pnl_formula_exactly(tmp_path):
    """Proves the ephemeral Position built in _close_shadow produces the
    exact same number compute_pnl() would produce for an equivalent real
    position - the single-source-of-truth guarantee from spec §5.4."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    shadow = _shadow_row(repo)
    risk_limits = _risk_limits()

    advance_shadow(
        shadow, candle_low=Decimal("52100"), candle_high=Decimal("52100"),
        current_price=Decimal("52050"), funding_rate=Decimal("0.0001"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=risk_limits, repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["exit_reason"] == "target"

    # Recompute independently, using the real position's own known fields
    # plus the same formulas, and assert equality with what got stored.
    real_position = repo.get_position("pos-1")
    expected_theoretical_exit = min(Decimal("52100"), Decimal("52000"))  # target=52000
    expected_fill_exit = compute_fill_price(
        expected_theoretical_exit, "LONG", risk_limits.spread_pct, risk_limits.slippage_pct, "exit"
    )
    expected_fees = compute_fees(real_position.size, risk_limits.fee_pct)
    assert Decimal(row["simulated_fill_exit"]) == expected_fill_exit
    assert Decimal(row["fees"]) == expected_fees

    expected_ephemeral = Position(
        position_id="x", candidate_id="x", instrument="BTCUSDT", direction="LONG",
        status="CLOSED", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=real_position.simulated_fill_entry,
        stop_loss=Decimal("49000"), target=Decimal("52000"), size=real_position.size,
        fill_model_version="v1", opened_at=_NOW,
        theoretical_exit=expected_theoretical_exit, simulated_fill_exit=expected_fill_exit,
        exit_reason="target", fees=expected_fees,
        funding=Decimal(row["funding"]), closed_at=_NOW,
    )
    assert Decimal(row["shadow_realized_pnl"]) == compute_pnl(expected_ephemeral)


def test_shadow_close_never_writes_to_positions_table(tmp_path):
    """G1: closing a shadow must never mutate the real position row."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    before = repo.get_position("pos-1")
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("48900"), candle_high=Decimal("49000"),
        current_price=Decimal("48950"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    after = repo.get_position("pos-1")
    assert before == after
    assert after.status == "OPEN_POSITION"  # still open - only the shadow closed


def _settings_with_pp(enabled: bool, guardian_assisted: bool = False) -> Settings:
    # _market_settings(top_n=1) is the exact same full-Settings builder
    # tests/crypto_trading/test_monitoring_loop.py already uses (imported
    # there the same way, from test_market_snapshot.py) - avoids
    # constructing a second, divergent fake Settings for this same purpose.
    settings = _market_settings(top_n=1)
    settings.profit_protection_experiment = ProfitProtectionExperimentConfig(enabled=enabled)
    settings.guardian.assisted_exit_enabled = guardian_assisted
    return settings


def test_tick_does_nothing_when_experiment_disabled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    price_lookup = {"BTCUSDT": (Decimal("49500"), Decimal("50100"), Decimal("49900"), Decimal("0"))}
    run_profit_protection_experiment_tick(
        repo, [position], [], price_lookup, _NOW, _settings_with_pp(enabled=False), "run-1"
    )
    assert repo.find_all_profit_protection_shadows() == []
    assert repo.get_profit_protection_activated_at() is None


def test_tick_seeds_and_advances_a_newly_opened_position_in_the_same_tick(tmp_path):
    """Plan correction C2 - a position that opens and immediately stops
    out on the very first tick the experiment observes it must still be
    seeded AND closed within that same tick call, never left stranded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    price_lookup = {
        "BTCUSDT": (Decimal("48900"), Decimal("49000"), Decimal("48950"), Decimal("0"))
    }
    run_profit_protection_experiment_tick(
        repo, [position], [], price_lookup, _NOW, _settings_with_pp(enabled=True), "run-1"
    )
    shadows = repo.find_all_profit_protection_shadows()
    assert len(shadows) == 2
    assert all(s["status"] == "CLOSED" for s in shadows)
    assert all(s["exit_reason"] == "stop_loss" for s in shadows)


def test_tick_defers_seeding_when_instrument_missing_from_price_lookup(tmp_path):
    """Plan correction C2 - mirrors close_triggered_positions's own
    'instrument not in price_lookup -> skip' behavior; never seeds on
    absent data."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    run_profit_protection_experiment_tick(
        repo, [position], [], {}, _NOW, _settings_with_pp(enabled=True), "run-1"
    )
    assert repo.find_all_profit_protection_shadows() == []


def test_tick_backfills_baseline_outcome_for_positions_closed_this_tick(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    settings = _settings_with_pp(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50100"), Decimal("50000"), Decimal("0"))}
    run_profit_protection_experiment_tick(
        repo, [position], [], price_lookup, _NOW, settings, "run-1"
    )
    # Simulate the real position closing on a later tick (as
    # close_triggered_positions would report it):
    closed_position = position.model_copy(update={
        "status": "CLOSED", "exit_reason": "stop_loss",
        "theoretical_exit": Decimal("49000"), "simulated_fill_exit": Decimal("48975.5"),
        "fees": Decimal("2"), "funding": Decimal("0"), "closed_at": _NOW,
    })
    later = _NOW + timedelta(minutes=1)
    run_profit_protection_experiment_tick(
        repo, [], [closed_position], {}, later, settings, "run-2"
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["hypothetical_baseline_exit_reason"] == "stop_loss"
    assert row["hypothetical_baseline_pnl"] is not None
