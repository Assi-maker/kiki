from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.backtest.report import (
    _bootstrap_ci,
    _median,
    _split_report_with_extras,
    build_tier1_report,
)
from crypto_trading.paper_trading.profit_protection_experiment import _shadow_id
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def _seed_shadow_row(
    repo, position_id: str, threshold_pct: Decimal,
    shadow_pnl: str | None, baseline_pnl: str | None, instrument: str = "BTCUSDT",
) -> str:
    """Seeds one shadow row through the repository's own public API only
    (no raw SQL), so these tests stay honest about what the real write
    path can actually produce.

    `shadow_pnl=None` leaves the row OPEN (right-censored shadow);
    `baseline_pnl=None` leaves the baseline outcome unfilled (the real
    position has not closed in the replay window yet). The backfill runs
    BEFORE the close so `pnl_difference` is populated by
    close_profit_protection_shadow exactly as production orders it."""
    shadow_id = _shadow_id(position_id, threshold_pct)
    repo.seed_profit_protection_shadow(
        shadow_id=shadow_id, position_id=position_id, instrument=instrument,
        threshold_pct=str(threshold_pct), entry_price=Decimal("50000"),
        original_stop_loss=Decimal("49000"), target=Decimal("60000"),
        threshold_price=Decimal("50500"), opened_at=_NOW, created_at=_NOW,
    )
    if baseline_pnl is not None:
        repo.backfill_profit_protection_baseline_outcome(
            position_id, "stop_loss", Decimal(baseline_pnl), _NOW
        )
    if shadow_pnl is not None:
        repo.close_profit_protection_shadow(
            shadow_id=shadow_id, exit_reason="target", theoretical_exit=Decimal("51000"),
            simulated_fill_exit=Decimal("50975"), fees=Decimal("0"), funding=Decimal("0"),
            closed_at=_NOW, shadow_realized_pnl=Decimal(shadow_pnl), updated_at=_NOW,
        )
    return shadow_id


def test_median_odd_count():
    assert _median([Decimal("1"), Decimal("5"), Decimal("3")]) == Decimal("3")


def test_median_even_count():
    assert _median([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]) == Decimal("2.5")


def test_median_empty_returns_none():
    assert _median([]) is None


def test_bootstrap_ci_returns_none_for_empty_input():
    assert _bootstrap_ci([]) is None


def test_bootstrap_ci_returns_a_tuple_bracketing_the_sample_mean():
    values = [Decimal(str(v)) for v in [10, 12, 9, 11, 50, -5, 8, 10, 11, 9]]
    low, high = _bootstrap_ci(values, resamples=2000, seed=42)
    sample_mean = sum(values) / len(values)
    assert low <= sample_mean <= high


def test_bootstrap_ci_is_deterministic_given_a_seed():
    values = [Decimal(str(v)) for v in [10, 12, 9, 11, 50, -5, 8, 10, 11, 9]]
    first = _bootstrap_ci(values, resamples=500, seed=7)
    second = _bootstrap_ci(values, resamples=500, seed=7)
    assert first == second


def test_build_tier1_report_flags_baseline_parity_mismatch(tmp_path):
    """If the replay's own baseline exit_reason disagrees with what
    production actually recorded for the same position, that must be a
    visible, named finding - never silently averaged away."""
    source = SQLiteRepository(tmp_path / "source.db")
    train = SQLiteRepository(tmp_path / "train.db")
    test_repo = SQLiteRepository(tmp_path / "test.db")
    from crypto_trading.schemas.event import Event
    from crypto_trading.schemas.trade import Position

    def _seed(repo, exit_reason):
        # create_position_with_event()'s INSERT only covers the "open"
        # columns (position_id..opened_at) - exit_reason/theoretical_exit/
        # simulated_fill_exit/fees/funding/closed_at are never in that
        # INSERT (see repository.py's SQLiteRepository.
        # create_position_with_event), so a position must be opened first
        # and then closed via close_position_with_event(), exactly like
        # production (position_opening.py -> position_closing.py) and
        # like the existing _seed_closed_position() helper in
        # tests/crypto_trading/test_notify_loop.py - otherwise exit_reason
        # is silently never persisted and this test's own mismatch check
        # would compare against None instead of "stop_loss".
        repo.create_position_with_event(
            Position(
                position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT",
                direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
                simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
                target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
                opened_at=_NOW,
            ),
            Event(
                event_id="e1", event_type="POSITION_OPENED", aggregate_type="position",
                aggregate_id="pos-1", occurred_at=_NOW, run_id="seed", schema_version=1, payload={},
            ),
        )
        repo.close_position_with_event(
            "pos-1", Decimal("49000"), Decimal("48975"), exit_reason,
            Decimal("0"), Decimal("0"), _NOW,
            Event(
                event_id="e2", event_type="POSITION_CLOSED", aggregate_type="position",
                aggregate_id="pos-1", occurred_at=_NOW, run_id="seed", schema_version=1, payload={},
            ),
        )

    _seed(train, "stop_loss")  # replay agrees with production
    target = BacktestTarget(
        position_id="pos-1", instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"), target=Decimal("52000"),
        opened_at=_NOW, original_size=Decimal("500"), original_status="CLOSED",
        original_exit_reason="target",  # production said target - DISAGREES with the replay above
        original_closed_at=_NOW, original_theoretical_exit=Decimal("52000"),
        original_simulated_fill_exit=Decimal("51974"),
    )

    report = build_tier1_report(train, test_repo, source, [target])

    assert len(report["baseline_parity_mismatches"]) == 1
    assert report["baseline_parity_mismatches"][0]["position_id"] == "pos-1"
    assert report["baseline_parity_mismatches"][0]["replayed_exit_reason"] == "stop_loss"
    assert report["baseline_parity_mismatches"][0]["production_exit_reason"] == "target"


def test_baseline_parity_flags_production_closed_but_replay_still_open(tmp_path):
    """Regression test for the final whole-branch review's Important Fix
    3. `_baseline_parity_mismatches` used to `continue` past any replayed
    position that was not CLOSED, silently dropping the exact
    disagreement class - production closed it, the replay left it open -
    that would have surfaced Critical Fix 1's unreachable-`time_limit`
    bug immediately. 7 such positions existed in the real run, invisible;
    only 4 unrelated mismatches were reported."""
    source = SQLiteRepository(tmp_path / "source.db")
    train = SQLiteRepository(tmp_path / "train.db")
    test_repo = SQLiteRepository(tmp_path / "test.db")

    # Seeded OPEN and deliberately never closed: the replay window ran out.
    train.create_position_with_event(
        Position(
            position_id="pos-open", candidate_id="pos-open", instrument="BTCUSDT",
            direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
            simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
            target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
            opened_at=_NOW,
        ),
        Event(
            event_id="e1", event_type="POSITION_OPENED", aggregate_type="position",
            aggregate_id="pos-open", occurred_at=_NOW, run_id="seed", schema_version=1, payload={},
        ),
    )
    target = BacktestTarget(
        position_id="pos-open", instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"), target=Decimal("52000"),
        opened_at=_NOW, original_size=Decimal("500"),
        original_status="CLOSED",  # production DID close this one
        original_exit_reason="time_limit",
        original_closed_at=_NOW + timedelta(hours=24),
        original_theoretical_exit=Decimal("50100"), original_simulated_fill_exit=Decimal("50075"),
    )

    report = build_tier1_report(train, test_repo, source, [target])

    assert len(report["baseline_parity_mismatches"]) == 1
    mismatch = report["baseline_parity_mismatches"][0]
    assert mismatch["position_id"] == "pos-open"
    assert mismatch["replayed_exit_reason"] is None
    assert mismatch["production_exit_reason"] == "time_limit"
    assert mismatch["note"] == "replay window ended with position still open"


def test_baseline_parity_stays_silent_for_a_target_routed_to_the_other_split(tmp_path):
    """A target that simply is not in THIS repo (it was routed to the
    other train/test split) is routing, not a disagreement - it must
    still be a silent skip, not a new false mismatch."""
    source = SQLiteRepository(tmp_path / "source.db")
    train = SQLiteRepository(tmp_path / "train.db")
    test_repo = SQLiteRepository(tmp_path / "test.db")
    target = BacktestTarget(
        position_id="pos-elsewhere", instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"), target=Decimal("52000"),
        opened_at=_NOW, original_size=Decimal("500"), original_status="CLOSED",
        original_exit_reason="target", original_closed_at=_NOW,
        original_theoretical_exit=Decimal("52000"), original_simulated_fill_exit=Decimal("51974"),
    )

    report = build_tier1_report(train, test_repo, source, [target])

    assert report["baseline_parity_mismatches"] == []


def test_baseline_parity_reports_closed_at_delta_hours_when_both_timestamps_exist(tmp_path):
    """Plan Task 6 item 3, never implemented until this fix wave: when
    the replay and production BOTH closed the position, a large
    disagreement in WHEN they closed is itself a finding, even though
    this fixture's exit_reason also disagrees."""
    source = SQLiteRepository(tmp_path / "source.db")
    train = SQLiteRepository(tmp_path / "train.db")
    test_repo = SQLiteRepository(tmp_path / "test.db")
    replay_closed_at = _NOW + timedelta(hours=2)
    train.create_position_with_event(
        Position(
            position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT",
            direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
            simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
            target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
            opened_at=_NOW,
        ),
        Event(
            event_id="e1", event_type="POSITION_OPENED", aggregate_type="position",
            aggregate_id="pos-1", occurred_at=_NOW, run_id="seed", schema_version=1, payload={},
        ),
    )
    train.close_position_with_event(
        "pos-1", Decimal("49000"), Decimal("48975"), "stop_loss",
        Decimal("0"), Decimal("0"), replay_closed_at,
        Event(
            event_id="e2", event_type="POSITION_CLOSED", aggregate_type="position",
            aggregate_id="pos-1", occurred_at=replay_closed_at, run_id="seed",
            schema_version=1, payload={},
        ),
    )
    target = BacktestTarget(
        position_id="pos-1", instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"), target=Decimal("52000"),
        opened_at=_NOW, original_size=Decimal("500"), original_status="CLOSED",
        original_exit_reason="target",
        original_closed_at=_NOW + timedelta(hours=9),  # 7h later than the replay
        original_theoretical_exit=Decimal("52000"), original_simulated_fill_exit=Decimal("51974"),
    )

    report = build_tier1_report(train, test_repo, source, [target])

    mismatch = report["baseline_parity_mismatches"][0]
    assert mismatch["closed_at_delta_hours"] == 7.0


def test_split_report_paired_totals_exclude_right_censored_rows(tmp_path):
    """Regression test for the final whole-branch review's Critical Fix 2.

    `build_report()` (reused, unmodified) computes
    `shadow_total_pnl_usdt` from every row with a non-null shadow P/L and
    `baseline_total_pnl_usdt` from every row with a non-null baseline P/L
    - two INDEPENDENTLY filtered lists, never paired by row. Any
    right-censoring (a shadow that closed while its baseline is still
    open) therefore adds shadow P/L with no matching baseline P/L, and
    the headline "shadow vs baseline" comparison silently compares two
    different samples.

    Real-run evidence: train@1.0% unpaired read "shadow 201.54 vs
    baseline 219.50" (PP looks 17.96 USDT WORSE); the same 24 PAIRED
    trades read "shadow 236.57 vs baseline 219.50" (PP is 17.07 USDT
    BETTER). The sign of the headline number was a sampling artifact.

    This fixture reproduces that sign inversion in miniature: two fully
    paired rows where the baseline beats the shadow (10 vs 30, 20 vs 40 =
    shadow 30, baseline 70), plus ONE right-censored row contributing a
    large shadow-only P/L (100) and no baseline. Unpaired: shadow 130 >
    baseline 70 (shadow "wins"). Paired: shadow 30 < baseline 70 (shadow
    loses). The paired fields must report the paired truth."""
    train = SQLiteRepository(tmp_path / "train.db")
    _seed_shadow_row(train, "pos-paired-1", Decimal("0.010"), shadow_pnl="10", baseline_pnl="30")
    _seed_shadow_row(train, "pos-paired-2", Decimal("0.010"), shadow_pnl="20", baseline_pnl="40")
    # Right-censored: shadow closed, baseline still open -> no baseline P/L.
    _seed_shadow_row(train, "pos-censored", Decimal("0.010"), shadow_pnl="100", baseline_pnl=None)

    block = _split_report_with_extras(train, [])["per_threshold"]["0.010"]

    # The pre-existing, unpaired fields must be untouched (additive fix).
    assert Decimal(block["shadow_total_pnl_usdt"]) == Decimal("130")
    assert Decimal(block["baseline_total_pnl_usdt"]) == Decimal("70")

    # The new paired fields drop the right-censored row from BOTH sides,
    # which flips which side is larger - the whole point of the fix.
    assert block["n_paired"] == 2
    assert block["n_baseline_pending"] == 1
    assert Decimal(block["paired_shadow_total_pnl_usdt"]) == Decimal("30")
    assert Decimal(block["paired_baseline_total_pnl_usdt"]) == Decimal("70")
    assert Decimal(block["paired_shadow_total_pnl_usdt"]) < Decimal(block["paired_baseline_total_pnl_usdt"])
    assert Decimal(block["shadow_total_pnl_usdt"]) > Decimal(block["baseline_total_pnl_usdt"])

    # Medians over the paired subset only: [10, 20] -> 15, [30, 40] -> 35.
    assert Decimal(block["paired_shadow_median_pnl_usdt"]) == Decimal("15")
    assert Decimal(block["paired_baseline_median_pnl_usdt"]) == Decimal("35")


def test_split_report_paired_medians_are_none_when_nothing_is_paired(tmp_path):
    train = SQLiteRepository(tmp_path / "train.db")
    _seed_shadow_row(train, "pos-censored", Decimal("0.010"), shadow_pnl="100", baseline_pnl=None)

    block = _split_report_with_extras(train, [])["per_threshold"]["0.010"]

    assert block["n_paired"] == 0
    assert block["n_baseline_pending"] == 1
    assert Decimal(block["paired_shadow_total_pnl_usdt"]) == Decimal("0")
    assert Decimal(block["paired_baseline_total_pnl_usdt"]) == Decimal("0")
    assert block["paired_shadow_median_pnl_usdt"] is None
    assert block["paired_baseline_median_pnl_usdt"] is None


def _seed_open_position(repo, position_id: str) -> None:
    repo.create_position_with_event(
        Position(
            position_id=position_id, candidate_id=position_id, instrument="BTCUSDT",
            direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
            simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
            target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
            opened_at=_NOW,
        ),
        Event(
            event_id=f"e-{position_id}", event_type="POSITION_OPENED", aggregate_type="position",
            aggregate_id=position_id, occurred_at=_NOW, run_id="seed", schema_version=1, payload={},
        ),
    )


def _target_for(position_id: str) -> BacktestTarget:
    return BacktestTarget(
        position_id=position_id, instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"), target=Decimal("52000"),
        opened_at=_NOW, original_size=Decimal("500"), original_status="OPEN_POSITION",
        original_exit_reason=None, original_closed_at=None,
        original_theoretical_exit=None, original_simulated_fill_exit=None,
    )


def test_split_report_counts_right_censored_shadows_and_positions(tmp_path):
    """Final whole-branch review, Important Fix 6: right-censored rows -
    shadows and baseline positions that the replay window ran out on
    before they could close - are excluded from every statistic in the
    block (build_report filters to status == 'CLOSED'). That exclusion
    must be VISIBLE, never silent, or the reader cannot tell a clean
    sample from a heavily truncated one."""
    train = SQLiteRepository(tmp_path / "train.db")
    # Two OPEN shadows at 1.0%, one OPEN at 1.5%, one CLOSED at 1.0%.
    _seed_shadow_row(train, "pos-open-1", Decimal("0.010"), shadow_pnl=None, baseline_pnl=None)
    _seed_shadow_row(train, "pos-open-2", Decimal("0.010"), shadow_pnl=None, baseline_pnl=None)
    _seed_shadow_row(train, "pos-open-2", Decimal("0.015"), shadow_pnl=None, baseline_pnl=None)
    _seed_shadow_row(train, "pos-closed", Decimal("0.010"), shadow_pnl="10", baseline_pnl="5")

    # Two positions left OPEN in the replay, one absent from this repo
    # entirely (routed to the other split - must NOT be counted here).
    _seed_open_position(train, "pos-open-1")
    _seed_open_position(train, "pos-open-2")
    targets = [_target_for(p) for p in ("pos-open-1", "pos-open-2", "pos-in-other-split")]

    split = _split_report_with_extras(train, targets)

    assert split["per_threshold"]["0.010"]["n_open_right_censored"] == 2
    assert split["per_threshold"]["0.015"]["n_open_right_censored"] == 1
    # Position-level, so counted once regardless of how many thresholds
    # each position seeded, and scoped to THIS repo only.
    assert split["n_baseline_positions_open_in_replay"] == 2


def test_split_report_right_censored_counts_are_zero_for_a_clean_split(tmp_path):
    train = SQLiteRepository(tmp_path / "train.db")
    _seed_shadow_row(train, "pos-closed", Decimal("0.010"), shadow_pnl="10", baseline_pnl="5")

    split = _split_report_with_extras(train, [_target_for("pos-closed")])

    assert split["per_threshold"]["0.010"]["n_open_right_censored"] == 0
    assert split["n_baseline_positions_open_in_replay"] == 0


def test_build_tier1_report_per_position_table_has_required_columns(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    train = SQLiteRepository(tmp_path / "train.db")
    test_repo = SQLiteRepository(tmp_path / "test.db")

    report = build_tier1_report(train, test_repo, source, [])

    assert report["per_position_table"] == []  # empty dataset -> empty table, never crashes
    required_columns = {
        "position_id", "instrument", "entry", "threshold", "threshold_reached",
        "mfe", "mae", "baseline_exit", "baseline_pnl", "shadow_exit", "shadow_pnl",
        "pnl_difference",
    }
    assert report["per_position_table_columns"] == sorted(required_columns)
