from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _guardian_state_for,
    seed_shadows_for_position,
)
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

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
