from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.performance.profit_protection_report import (
    _BREAKEVEN_BAND_PCT,
    _classify_reach,
    _conversion_ratio,
    _is_blocked_by_exposure,
    _sample_sizes,
    build_report,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _row(**overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", status="CLOSED", threshold_reached=1,
        entry_price="50000", mfe="1000", mae="-200",
        shadow_realized_pnl="300", hypothetical_baseline_exit_reason="target",
        hypothetical_baseline_pnl="1000", exit_reason="stop_loss",
    )
    defaults.update(overrides)
    return defaults


def test_classify_reach_never_reached(tmp_path):
    row = _row(threshold_reached=0, hypothetical_baseline_pnl=None,
               hypothetical_baseline_exit_reason=None)
    assert _classify_reach(row, position_size=Decimal("5000")) == "never_reached_threshold"


def test_classify_reach_loss(tmp_path):
    row = _row(hypothetical_baseline_pnl="-500")
    assert _classify_reach(row, position_size=Decimal("5000")) == "reached_threshold_baseline_loss"


def test_classify_reach_approx_breakeven(tmp_path):
    row = _row(hypothetical_baseline_pnl="10")  # 10/5000 = 0.002 <= 0.003 band
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_approx_breakeven"
    )


def test_classify_reach_big_winner(tmp_path):
    row = _row(hypothetical_baseline_pnl="1000", hypothetical_baseline_exit_reason="target")
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_big_winner"
    )


def test_classify_reach_moderate_gain(tmp_path):
    row = _row(hypothetical_baseline_pnl="200", hypothetical_baseline_exit_reason="time_limit")
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_moderate_gain"
    )


def test_classify_reach_pending_when_baseline_not_yet_known(tmp_path):
    row = _row(hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None)
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_pending"
    )


def test_classification_buckets_are_exhaustive_and_mutually_exclusive():
    known_buckets = {
        "never_reached_threshold", "reached_threshold_baseline_loss",
        "reached_threshold_baseline_approx_breakeven", "reached_threshold_baseline_big_winner",
        "reached_threshold_baseline_moderate_gain", "reached_threshold_baseline_pending",
    }
    scenarios = [
        _row(threshold_reached=0, hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None),
        _row(hypothetical_baseline_pnl="-1"),
        _row(hypothetical_baseline_pnl="0"),
        _row(hypothetical_baseline_pnl="5000", hypothetical_baseline_exit_reason="target"),
        _row(hypothetical_baseline_pnl="200", hypothetical_baseline_exit_reason="time_limit"),
        _row(hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None, threshold_reached=1),
    ]
    labels = [_classify_reach(row, position_size=Decimal("5000")) for row in scenarios]
    # Review finding 4 (final whole-branch review): == instead of <= - the
    # six fixture scenarios above are deliberately designed to cover all
    # six buckets, so this proves every bucket is actually REACHABLE, not
    # merely that no unknown bucket ever appears (which `<=` alone would
    # leave unproven - a buggy _classify_reach that always returned the
    # same single label would have passed the old assertion).
    assert set(labels) == known_buckets
    assert len(labels) == len(scenarios)  # one label per row, none skipped/duplicated


def test_breakeven_band_pct_is_pinned_at_three_tenths_of_one_percent():
    """Review finding 5 (final whole-branch review): _BREAKEVEN_BAND_PCT is
    already imported above but was never pinned by an assertion anywhere in
    this file - a silent, unnoticed change to this constant's value would
    have shifted the approx_breakeven/moderate_gain boundary (spec S7.2)
    without a single test failing."""
    assert _BREAKEVEN_BAND_PCT == Decimal("0.003")


def test_conversion_ratio_is_none_when_mfe_not_positive():
    row = _row(mfe="0")
    assert _conversion_ratio(row, position_size=Decimal("5000"), entry_price=Decimal("50000")) is None


def test_conversion_ratio_dimensionless_formula():
    row = _row(mfe="500", shadow_realized_pnl="250")  # mfe_pct=0.01, pnl_pct=0.05
    ratio = _conversion_ratio(row, position_size=Decimal("5000"), entry_price=Decimal("50000"))
    assert ratio == Decimal("5")  # 0.05 / 0.01


def test_sample_sizes_counts_match_definitions():
    rows = [
        _row(shadow_id="a", threshold_reached=0, hypothetical_baseline_pnl=None,
             hypothetical_baseline_exit_reason=None, shadow_realized_pnl="0"),
        _row(shadow_id="b", hypothetical_baseline_pnl="-500", shadow_realized_pnl="0"),
        _row(shadow_id="c", hypothetical_baseline_exit_reason="target", shadow_realized_pnl="300"),
        _row(shadow_id="d", shadow_realized_pnl="-50", hypothetical_baseline_pnl="-50"),
    ]
    sizes = _sample_sizes(rows)
    assert sizes["n_closed"] == 4
    assert sizes["n_reached_threshold"] == 3
    assert sizes["n_not_reached"] == 1
    assert sizes["n_baseline_losses_after_threshold"] == 2  # b and d
    assert sizes["n_baseline_target_winners_after_threshold"] == 1  # c
    assert sizes["n_shadow_winners"] == 1  # c
    assert sizes["n_shadow_losses"] == 1  # d


def test_report_note_is_always_present_regardless_of_data(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    report = build_report(repo)
    assert "Pre-registered hypotheses under test" in report["note"]
    assert "never selects a winner" in report["note"]


def _make_position(position_id: str, instrument: str, size: Decimal) -> Position:
    return Position(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
        direction="LONG", status="CLOSED", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=size, fill_model_version="v1", opened_at=_NOW,
        theoretical_exit=Decimal("49000"), simulated_fill_exit=Decimal("48975"),
        exit_reason="stop_loss", fees=Decimal("0"), funding=Decimal("0"), closed_at=_NOW,
    )


def _seed_position(repo, position_id: str, instrument: str, size: Decimal) -> None:
    position = _make_position(position_id, instrument, size)
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
            run_id="seed", schema_version=1, payload={},
        ),
    )


def test_is_blocked_by_exposure_true_for_zero_size():
    """Same definition/semantics as performance/paper_track_report.py::
    _is_blocked_by_exposure() and detective/stats.py::_is_blocked_by_exposure()
    - a position whose size was pushed to 0 by max_total_exposure_pct
    represents zero real market exposure."""
    assert _is_blocked_by_exposure(_make_position("p", "BTCUSDT", Decimal("0"))) is True


def test_is_blocked_by_exposure_false_for_positive_size():
    assert _is_blocked_by_exposure(_make_position("p", "BTCUSDT", Decimal("1000"))) is False


def _seed_shadow_kwargs(**overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", entry_price=Decimal("50000"),
        original_stop_loss=Decimal("49000"), target=Decimal("52000"),
        threshold_price=Decimal("50500"), opened_at=_NOW, created_at=_NOW,
    )
    defaults.update(overrides)
    return defaults


def test_build_report_surfaces_n_abandoned_count(tmp_path):
    """Review finding 1 (final whole-branch review): a shadow marked
    ABANDONED (real position no longer open, for any reason) must be
    surfaced as a visible count in the report, not silently dropped from
    every stat with no trace it was ever excluded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(
        shadow_id="pos-1:0.010", position_id="pos-1", threshold_pct="0.010",
    ))
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(
        shadow_id="pos-1:0.015", position_id="pos-1", threshold_pct="0.015",
        threshold_price=Decimal("50750"),
    ))
    repo.abandon_profit_protection_shadow("pos-1:0.010", _NOW)

    report = build_report(repo)

    assert report["per_threshold"]["0.010"]["sample_sizes"]["n_abandoned"] == 1
    assert report["per_threshold"]["0.015"]["sample_sizes"]["n_abandoned"] == 0
    assert report["combined"]["sample_sizes"]["n_abandoned"] == 1


def test_build_report_integration_with_real_seeded_advanced_and_closed_positions(tmp_path):
    """Finding 2 (final whole-branch review): build_report had only ever
    run against an empty repo - repo.get_position() in _stats_block, the
    chronological split, _max_drawdown, and the compute_win_rate/
    compute_expectancy/compute_profit_factor wiring had never executed
    against a single real row. This seeds two real positions and drives
    their shadows through the ACTUAL seed -> advance -> close -> backfill
    pipeline (run_profit_protection_experiment_tick + the real
    close_triggered_positions - never a hand-inserted shadow row), then
    asserts exact numbers computed from the same already-tested
    primitives (compute_fill_price/compute_pnl/_conversion_ratio) applied
    to this fixture's own known candle inputs - not merely "is not
    None"."""
    from crypto_trading.config.loader import ProfitProtectionExperimentConfig
    from crypto_trading.paper_trading.execution import compute_fill_price
    from crypto_trading.paper_trading.position_closing import close_triggered_positions
    from crypto_trading.paper_trading.profit_protection_experiment import (
        run_profit_protection_experiment_tick,
    )
    from tests.crypto_trading.test_market_snapshot import _settings as _market_settings

    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _market_settings(top_n=1)
    settings.profit_protection_experiment = ProfitProtectionExperimentConfig(enabled=True)
    settings.guardian.assisted_exit_enabled = False
    risk_limits = settings.risk_limits

    def _open(position_id, instrument, opened_at, entry, stop, target, size):
        entry_fill = compute_fill_price(
            entry, "LONG", risk_limits.spread_pct, risk_limits.slippage_pct, "entry"
        )
        position = Position(
            position_id=position_id, candidate_id=position_id, instrument=instrument,
            direction="LONG", status="OPEN_POSITION", theoretical_entry=entry,
            simulated_fill_entry=entry_fill, stop_loss=stop, target=target,
            size=size, fill_model_version="v1", opened_at=opened_at,
        )
        repo.create_position_with_event(
            position,
            Event(
                event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
                aggregate_type="position", aggregate_id=position_id,
                occurred_at=opened_at, run_id="seed", schema_version=1, payload={},
            ),
        )
        return repo.get_position(position_id)

    # pos-1 (BTCUSDT): touches BOTH frozen thresholds, then the shadow
    # exits early at breakeven while the real (baseline) position goes on,
    # untouched, to hit its own original, unmodified stop-loss much later -
    # "protection meaningfully reduced the loss" (exact numbers below, no
    # forced rounding).
    pos1 = _open(
        "pos-1", "BTCUSDT", _NOW,
        Decimal("50000"), Decimal("49000"), Decimal("60000"), Decimal("10000"),
    )

    # Tick 1 (activation tick): seeds pos-1's shadows, no exit, no threshold touch.
    run_profit_protection_experiment_tick(
        repo, [pos1], [],
        {"BTCUSDT": (Decimal("49700"), Decimal("50100"), Decimal("50000"), Decimal("0"))},
        _NOW, settings, "t1",
    )

    # pos-2 (ETHUSDT) opens one minute later - never reaches threshold,
    # closes identically for baseline and shadow on its very first tick.
    t2_time = _NOW + timedelta(minutes=1)
    pos2 = _open(
        "pos-2", "ETHUSDT", t2_time,
        Decimal("3000"), Decimal("2900"), Decimal("3200"), Decimal("2000"),
    )

    # Tick 2: pos-1's candle touches both +1.0%/+1.5% thresholds (breakeven
    # activates for both, effective next tick only, per spec S5.2 step 4);
    # pos-2 seeds AND closes at its own original stop-loss in this same
    # tick (plan correction C2) - identically for baseline and shadow,
    # since neither ever activates breakeven.
    tick2_price_lookup = {
        "BTCUSDT": (Decimal("50600"), Decimal("50900"), Decimal("50800"), Decimal("0")),
        "ETHUSDT": (Decimal("2850"), Decimal("2950"), Decimal("2900"), Decimal("0")),
    }
    open_positions_t2 = [repo.get_position("pos-1"), pos2]
    closed_t2 = close_triggered_positions(repo, tick2_price_lookup, t2_time, risk_limits, "t2")
    assert {p.position_id for p in closed_t2} == {"pos-2"}
    run_profit_protection_experiment_tick(
        repo, open_positions_t2, closed_t2, tick2_price_lookup, t2_time, settings, "t2",
    )

    assert repo.get_profit_protection_shadow("pos-1:0.010")["threshold_reached"] == 1
    assert repo.get_profit_protection_shadow("pos-1:0.010")["breakeven_stop_loss"] == "50000"
    assert repo.get_profit_protection_shadow("pos-1:0.010")["status"] == "OPEN"
    assert repo.get_profit_protection_shadow("pos-2:0.010")["status"] == "CLOSED"
    assert repo.get_profit_protection_shadow("pos-2:0.010")["threshold_reached"] == 0

    # Tick 3: pos-1's shadow (now at breakeven=50000) closes just below
    # entry; the real (baseline) position is untouched (49950 > its own,
    # unmodified 49000 stop) - the shadow independently exits BEFORE
    # baseline, exactly as spec S5.3 requires.
    t3_time = _NOW + timedelta(minutes=2)
    tick3_price_lookup = {
        "BTCUSDT": (Decimal("49950"), Decimal("50100"), Decimal("50000"), Decimal("0")),
    }
    open_positions_t3 = [repo.get_position("pos-1")]
    closed_t3 = close_triggered_positions(repo, tick3_price_lookup, t3_time, risk_limits, "t3")
    assert closed_t3 == []
    run_profit_protection_experiment_tick(
        repo, open_positions_t3, closed_t3, tick3_price_lookup, t3_time, settings, "t3",
    )
    for threshold in ("0.010", "0.015"):
        row = repo.get_profit_protection_shadow(f"pos-1:{threshold}")
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == "stop_loss"
        assert row["theoretical_exit"] == "49950"

    # Tick 4: the real (baseline) position finally hits its own original
    # stop-loss - well after its shadow already closed.
    t4_time = _NOW + timedelta(minutes=3)
    tick4_price_lookup = {
        "BTCUSDT": (Decimal("48800"), Decimal("49200"), Decimal("48900"), Decimal("0")),
    }
    open_positions_t4 = [repo.get_position("pos-1")]
    closed_t4 = close_triggered_positions(repo, tick4_price_lookup, t4_time, risk_limits, "t4")
    assert {p.position_id for p in closed_t4} == {"pos-1"}
    run_profit_protection_experiment_tick(
        repo, open_positions_t4, closed_t4, tick4_price_lookup, t4_time, settings, "t4",
    )

    # --- ground truth, read back from what the real pipeline persisted ---
    pos1_row_010 = repo.get_profit_protection_shadow("pos-1:0.010")
    pos2_row_010 = repo.get_profit_protection_shadow("pos-2:0.010")
    assert pos1_row_010["hypothetical_baseline_pnl"] is not None
    assert pos2_row_010["hypothetical_baseline_pnl"] is not None

    pos1_shadow_pnl = Decimal(pos1_row_010["shadow_realized_pnl"])
    pos1_baseline_pnl = Decimal(pos1_row_010["hypothetical_baseline_pnl"])
    pos2_shadow_pnl = Decimal(pos2_row_010["shadow_realized_pnl"])
    pos2_baseline_pnl = Decimal(pos2_row_010["hypothetical_baseline_pnl"])

    assert pos1_baseline_pnl < 0        # baseline lost more (rode down to the real 49000 stop)
    assert pos1_shadow_pnl < 0          # breakeven exit still nets a small loss after fees/slippage
    assert pos1_shadow_pnl > pos1_baseline_pnl  # protection meaningfully reduced the loss
    assert pos2_shadow_pnl == pos2_baseline_pnl  # never reached threshold -> byte-identical trade
    assert Decimal(pos2_row_010["pnl_difference"]) == 0

    report = build_report(repo)
    for threshold in ("0.010", "0.015"):
        block = report["per_threshold"][threshold]
        sizes = block["sample_sizes"]
        assert sizes["n_closed"] == 2
        assert sizes["n_reached_threshold"] == 1        # pos-1 only
        assert sizes["n_not_reached"] == 1               # pos-2
        assert sizes["n_baseline_losses_after_threshold"] == 1  # pos-1
        assert sizes["n_baseline_target_winners_after_threshold"] == 0
        assert sizes["n_shadow_winners"] == 0
        assert sizes["n_shadow_losses"] == 2
        assert sizes["n_abandoned"] == 0

        assert block["reach_classification_counts"] == {
            "reached_threshold_baseline_loss": 1,
            "never_reached_threshold": 1,
        }
        assert block["outcome_label_counts"] == {
            "protection_improved_other": 1,
            "protection_no_change": 1,
        }
        assert block["loss_saved_count"] == 0
        assert block["large_winner_clipped_count"] == 0

        row = repo.get_profit_protection_shadow(f"pos-1:{threshold}")
        diff = Decimal(row["pnl_difference"])
        assert diff > 0
        assert block["profit_protection_improved_pl"]["count"] == 1
        assert Decimal(block["profit_protection_improved_pl"]["total_usdt"]) == diff
        assert block["profit_protection_worsened_pl"]["count"] == 0
        assert Decimal(block["profit_protection_worsened_pl"]["total_usdt"]) == Decimal("0")

        shadow_pnl_1 = Decimal(row["shadow_realized_pnl"])
        shadow_pnl_2 = Decimal(
            repo.get_profit_protection_shadow(f"pos-2:{threshold}")["shadow_realized_pnl"]
        )
        assert Decimal(block["shadow_total_pnl_usdt"]) == shadow_pnl_1 + shadow_pnl_2
        assert Decimal(block["shadow_win_rate"]) == Decimal("0")
        assert Decimal(block["shadow_expectancy_usdt"]) == (shadow_pnl_1 + shadow_pnl_2) / 2
        assert Decimal(block["shadow_profit_factor"]) == Decimal("0")  # losses only, no wins
        assert Decimal(block["shadow_max_drawdown_usdt"]) == -(shadow_pnl_1 + shadow_pnl_2)

        # Conversion ratio: pos-2's mfe never exceeds 0 (candles never rose
        # above entry before it stopped out) -> excluded; pos-1 (mfe=900,
        # entry_price=50000) is the sole contributor to the average, so the
        # average must equal its own ratio exactly.
        entry_price = Decimal(row["entry_price"])
        expected_ratio = _conversion_ratio(row, position_size=pos1.size, entry_price=entry_price)
        assert expected_ratio is not None
        assert block["conversion_ratio_excluded_count"] == 1
        assert Decimal(block["conversion_ratio_avg"]) == expected_ratio

        split = block["chronological_split"]
        assert split["first_half"]["sample_sizes"]["n_closed"] == 1
        assert split["first_half"]["trades"][0]["position_id"] == "pos-1"
        assert split["second_half"]["sample_sizes"]["n_closed"] == 1
        assert split["second_half"]["trades"][0]["position_id"] == "pos-2"

    combined_sizes = report["combined"]["sample_sizes"]
    assert combined_sizes["n_closed"] == 4
    assert combined_sizes["n_reached_threshold"] == 2
    assert combined_sizes["n_not_reached"] == 2
    assert combined_sizes["n_baseline_losses_after_threshold"] == 2
    assert combined_sizes["n_shadow_winners"] == 0
    assert combined_sizes["n_shadow_losses"] == 4
    assert combined_sizes["n_abandoned"] == 0


def _seed_closed_shadow(
    repo, shadow_id, position_id, instrument, threshold_pct, mfe, threshold_reached,
    shadow_realized_pnl, baseline_pnl, exit_reason="stop_loss", baseline_exit_reason="stop_loss",
):
    """Test-only helper: seeds a shadow row and drives it straight to CLOSED
    with backfilled baseline outcome via the real repository write methods
    (seed/close/backfill), bypassing the full tick orchestration - the
    values under test here (mfe/threshold_reached/pnl) are set directly
    since this test targets profit_protection_report.py's own aggregation
    logic, not the state machine that produces these values in production."""
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(
        shadow_id=shadow_id, position_id=position_id, instrument=instrument,
        threshold_pct=threshold_pct,
    ))
    if threshold_reached:
        repo.activate_profit_protection_breakeven(shadow_id, Decimal("50000"), _NOW, _NOW)
    repo.record_profit_protection_tick(shadow_id, mfe, Decimal("-100"), _NOW)
    repo.close_profit_protection_shadow(
        shadow_id=shadow_id, exit_reason=exit_reason, theoretical_exit=Decimal("49000"),
        simulated_fill_exit=Decimal("48975"), fees=Decimal("0"), funding=Decimal("0"),
        closed_at=_NOW, shadow_realized_pnl=shadow_realized_pnl, updated_at=_NOW,
    )
    repo.backfill_profit_protection_baseline_outcome(
        position_id, baseline_exit_reason, baseline_pnl, _NOW
    )


def test_build_report_all_positions_blocked_by_exposure_does_not_crash(tmp_path):
    """Regression (size=0 report crash): every closed shadow this tick is
    linked to a real position whose size was pushed to 0 by the exposure
    pool cap (paper_trading/position_sizing.py::compute_position_size()).
    Before the fix, _classify_reach's breakeven-band division and
    _conversion_ratio's realized_pnl_pct division both divided by this 0,
    raising decimal.DivisionUndefined. threshold_reached=1 and mfe>0 are
    chosen deliberately to exercise BOTH crash sites."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-1", "BTCUSDT", Decimal("0"))
    _seed_position(repo, "pos-2", "ETHUSDT", Decimal("0"))
    _seed_closed_shadow(
        repo, "pos-1:0.010", "pos-1", "BTCUSDT", "0.010",
        mfe=Decimal("900"), threshold_reached=1,
        shadow_realized_pnl=Decimal("0"), baseline_pnl=Decimal("0"),
    )
    _seed_closed_shadow(
        repo, "pos-2:0.010", "pos-2", "ETHUSDT", "0.010",
        mfe=Decimal("0"), threshold_reached=0,
        shadow_realized_pnl=Decimal("0"), baseline_pnl=Decimal("0"),
    )

    report = build_report(repo)  # must not raise decimal.DivisionUndefined

    block = report["per_threshold"]["0.010"]
    sizes = block["sample_sizes"]
    assert sizes["n_closed"] == 2
    assert sizes["n_reached_threshold"] == 1  # threshold_reached is still counted for blocked rows
    assert sizes["n_not_reached"] == 1
    assert sizes["n_blocked_by_exposure"] == 2
    assert block["reach_classification_counts"] == {"blocked_by_exposure": 2}
    assert block["shadow_total_pnl_usdt"] == "0"
    assert block["shadow_win_rate"] is None  # undefined - no economically-active trades at all
    assert block["shadow_expectancy_usdt"] is None
    assert block["shadow_profit_factor"] is None
    assert block["shadow_max_drawdown_usdt"] is None
    assert block["conversion_ratio_avg"] is None
    # never even considered, not "excluded due to mfe<=0"
    assert block["conversion_ratio_excluded_count"] == 0
    assert block["loss_saved_count"] == 0
    assert block["large_winner_clipped_count"] == 0


def test_build_report_mixed_blocked_and_active_positions_separates_dollar_stats(tmp_path):
    """Regression: one economically-active position (size>0) and one
    blocked-by-exposure position (size=0, given a deliberately large,
    obviously-wrong shadow_realized_pnl/baseline_pnl here to prove the
    exclusion is a real size==0 check, not an accident of the blocked
    row's real-world value always happening to be 0). Price-based counts
    (n_closed/n_reached_threshold) must include both; every dollar-based
    aggregate must reflect ONLY the active position."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-active", "BTCUSDT", Decimal("5000"))
    _seed_position(repo, "pos-blocked", "ETHUSDT", Decimal("0"))
    _seed_closed_shadow(
        repo, "pos-active:0.010", "pos-active", "BTCUSDT", "0.010",
        mfe=Decimal("500"), threshold_reached=1,
        shadow_realized_pnl=Decimal("250"), baseline_pnl=Decimal("-100"),
        exit_reason="target", baseline_exit_reason="stop_loss",
    )
    _seed_closed_shadow(
        repo, "pos-blocked:0.010", "pos-blocked", "ETHUSDT", "0.010",
        mfe=Decimal("900"), threshold_reached=1,
        shadow_realized_pnl=Decimal("99999"), baseline_pnl=Decimal("99999"),
        exit_reason="target", baseline_exit_reason="target",
    )

    report = build_report(repo)  # must not raise decimal.DivisionUndefined

    block = report["per_threshold"]["0.010"]
    sizes = block["sample_sizes"]
    assert sizes["n_closed"] == 2
    assert sizes["n_reached_threshold"] == 2
    assert sizes["n_blocked_by_exposure"] == 1

    assert block["reach_classification_counts"]["blocked_by_exposure"] == 1
    assert sum(
        v for k, v in block["reach_classification_counts"].items() if k != "blocked_by_exposure"
    ) == 1

    # Every dollar aggregate reflects pos-active ONLY - pos-blocked's huge
    # 99999 values must never leak in despite threshold_reached=1/mfe>0.
    assert block["shadow_total_pnl_usdt"] == "250"
    assert Decimal(block["shadow_win_rate"]) == Decimal("1")
    assert Decimal(block["shadow_expectancy_usdt"]) == Decimal("250")
    assert block["conversion_ratio_excluded_count"] == 0
    expected_ratio = _conversion_ratio(
        {"mfe": "500", "shadow_realized_pnl": "250"}, Decimal("5000"), Decimal("50000")
    )
    assert Decimal(block["conversion_ratio_avg"]) == expected_ratio
    assert block["profit_protection_improved_pl"]["count"] == 1
    assert Decimal(block["profit_protection_improved_pl"]["total_usdt"]) == Decimal("350")
    assert block["loss_saved_count"] == 1  # pos-active: baseline -100 -> shadow 250 >= 0
