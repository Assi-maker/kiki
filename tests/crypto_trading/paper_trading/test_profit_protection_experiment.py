from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _guardian_state_for,
)
from crypto_trading.schemas.guardian import GuardianObservation
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
