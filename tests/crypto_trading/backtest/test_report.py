from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.backtest.report import _bootstrap_ci, _median, build_tier1_report
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


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
