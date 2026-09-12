from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.backtest.dataset import select_backtest_targets
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def _seed_position(repo, position_id, instrument, size, status, **overrides) -> Position:
    defaults = dict(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=size, fill_model_version="v1", opened_at=_NOW,
    )
    # Extract exit fields if provided
    theoretical_exit = overrides.pop("theoretical_exit", None)
    simulated_fill_exit = overrides.pop("simulated_fill_exit", None)
    exit_reason = overrides.pop("exit_reason", None)
    fees = overrides.pop("fees", None)
    funding = overrides.pop("funding", None)
    closed_at = overrides.pop("closed_at", None)

    defaults.update(overrides)
    position = Position(**defaults)
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
            run_id="seed", schema_version=1, payload={},
        ),
    )

    # If this is a closed position, close it properly
    if status == "CLOSED":
        repo.close_position_with_event(
            position_id=position_id,
            theoretical_exit=theoretical_exit or Decimal("0"),
            simulated_fill_exit=simulated_fill_exit or Decimal("0"),
            exit_reason=exit_reason or "",
            fees=fees or Decimal("0"),
            funding=funding or Decimal("0"),
            closed_at=closed_at or _NOW,
            event=Event(
                event_id=f"POSITION_CLOSED:{position_id}", event_type="POSITION_CLOSED",
                aggregate_type="position", aggregate_id=position_id, occurred_at=closed_at or _NOW,
                run_id="seed", schema_version=1, payload={},
            ),
        )

    return position


def test_select_backtest_targets_includes_size_zero_positions(tmp_path):
    """The whole point of Tier 1: a real historical position that got
    size=0 from the live exposure pool is still a perfectly valid trade
    setup (real entry/stop/target) - it must not be silently excluded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-blocked", "BTCUSDT", Decimal("0"), "CLOSED",
                    theoretical_exit=Decimal("49000"), simulated_fill_exit=Decimal("48975"),
                    exit_reason="stop_loss", fees=Decimal("0"), funding=Decimal("0"),
                    closed_at=_NOW)

    targets = select_backtest_targets(repo)

    assert len(targets) == 1
    assert targets[0].position_id == "pos-blocked"
    assert targets[0].original_size == Decimal("0")


def test_select_backtest_targets_includes_open_positions_with_no_original_exit(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-open", "ETHUSDT", Decimal("1000"), "OPEN_POSITION")

    targets = select_backtest_targets(repo)

    assert len(targets) == 1
    assert targets[0].original_status == "OPEN_POSITION"
    assert targets[0].original_exit_reason is None
    assert targets[0].original_closed_at is None


def test_select_backtest_targets_preserves_real_entry_stop_target(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-1", "BTCUSDT", Decimal("500"), "CLOSED",
                    theoretical_entry=Decimal("60000"), simulated_fill_entry=Decimal("60030"),
                    stop_loss=Decimal("58000"), target=Decimal("64000"),
                    theoretical_exit=Decimal("64000"), simulated_fill_exit=Decimal("63968"),
                    exit_reason="target", fees=Decimal("0.2"), funding=Decimal("0"),
                    closed_at=_NOW)

    targets = select_backtest_targets(repo)

    t = targets[0]
    assert t.entry_price == Decimal("60000")
    assert t.simulated_fill_entry == Decimal("60030")
    assert t.stop_loss == Decimal("58000")
    assert t.target == Decimal("64000")
    assert t.original_exit_reason == "target"
    assert t.original_theoretical_exit == Decimal("64000")
    assert t.original_simulated_fill_exit == Decimal("63968")


def test_select_backtest_targets_returns_empty_for_empty_repo(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert select_backtest_targets(repo) == []
