from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.backtest.guardian_replay import copy_guardian_history
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def _observation(obs_id: str, position_id: str, state: str, observed_at: datetime) -> GuardianObservation:
    return GuardianObservation(
        observation_id=obs_id, position_id=position_id, observed_at=observed_at,
        state=state, decay_score=Decimal("0.4"), progress_ratio=Decimal("0.1"),
        unrealized_pnl=Decimal("10"), factors={"time_decay": 0.1}, run_id="seed",
    )


def test_copy_guardian_history_copies_all_observations_for_the_position(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    source.save_guardian_observation(_observation("obs-1", "pos-1", "WATCH", _NOW))
    source.save_guardian_observation(_observation("obs-2", "pos-1", "PROTECT", _NOW))
    source.save_guardian_observation(_observation("obs-3", "pos-OTHER", "WATCH", _NOW))

    count = copy_guardian_history(source, backtest, "pos-1")

    assert count == 2
    copied = backtest.find_guardian_observations_for_position("pos-1")
    assert {o["observation_id"] for o in copied} == {"obs-1", "obs-2"}
    assert backtest.find_guardian_observations_for_position("pos-OTHER") == []


def test_copy_guardian_history_returns_zero_for_a_position_with_no_history(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")

    count = copy_guardian_history(source, backtest, "pos-never-watched")

    assert count == 0
    assert backtest.find_guardian_observations_for_position("pos-never-watched") == []


def test_copy_guardian_history_never_writes_to_the_source_repo(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    source.save_guardian_observation(_observation("obs-1", "pos-1", "WATCH", _NOW))

    copy_guardian_history(source, backtest, "pos-1")

    assert len(source.find_guardian_observations_for_position("pos-1")) == 1  # unchanged, not duplicated


def test_copy_guardian_history_up_to_excludes_future_observations(tmp_path):
    """`up_to` must exclude any observation whose observed_at is AFTER the
    given cutoff - this is the no-look-ahead guarantee a tick-by-tick
    replay caller relies on. Without this filter, a Guardian observation
    dated after the candle currently being replayed would leak backwards
    (find_latest_guardian_observation's staleness guard has no upper
    bound on observed_at)."""
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    source.save_guardian_observation(_observation("obs-past", "pos-1", "WATCH", _NOW))
    source.save_guardian_observation(_observation("obs-future", "pos-1", "EXIT", _NOW + timedelta(hours=10)))

    count = copy_guardian_history(source, backtest, "pos-1", up_to=_NOW)

    assert count == 1
    copied = backtest.find_guardian_observations_for_position("pos-1")
    assert {o["observation_id"] for o in copied} == {"obs-past"}


def test_copy_guardian_history_up_to_none_copies_everything(tmp_path):
    """The default (up_to=None) preserves the original copy-everything
    behavior, unchanged."""
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    source.save_guardian_observation(_observation("obs-past", "pos-1", "WATCH", _NOW))
    source.save_guardian_observation(_observation("obs-future", "pos-1", "EXIT", _NOW + timedelta(hours=10)))

    count = copy_guardian_history(source, backtest, "pos-1")

    assert count == 2


def test_copy_guardian_history_factors_field_round_trip(tmp_path):
    """Verify that the factors field (stored as JSON string in DB) round-trips correctly
    through the full cycle: save_guardian_observation -> find_guardian_observations_for_position
    -> copy_guardian_history -> save to backtest -> find in backtest.

    The factors field is stored as a JSON string in the database, and the copy_guardian_history
    function must handle parsing it when reading from source, then reconstruct the observation
    correctly before saving to backtest."""
    import json

    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")

    # Save an observation with complex factors
    original_factors = {"time_decay": 0.1, "volume_zscore": 0.5, "rsi_threshold": 70.0}
    original_obs = GuardianObservation(
        observation_id="obs-1", position_id="pos-1", observed_at=_NOW,
        state="WATCH", decay_score=Decimal("0.4"), progress_ratio=Decimal("0.1"),
        unrealized_pnl=Decimal("10"), factors=original_factors, run_id="seed",
    )
    source.save_guardian_observation(original_obs)

    # Copy it to backtest - this is where json.loads() is needed internally
    count = copy_guardian_history(source, backtest, "pos-1")
    assert count == 1

    # Read it back from backtest - factors will be a JSON string (that's how DB stores it)
    copied = backtest.find_guardian_observations_for_position("pos-1")
    assert len(copied) == 1

    row = copied[0]
    # Factors comes back as JSON string from DB
    assert isinstance(row["factors"], str), "factors in DB should be JSON string"

    # But we can parse it and verify the data is correct
    parsed_factors = json.loads(row["factors"])
    assert parsed_factors == original_factors

    # And importantly, when we reconstruct with json.loads in place, it works
    row["factors"] = json.loads(row["factors"])
    reconstructed = GuardianObservation(**row)
    assert reconstructed.observation_id == "obs-1"
    assert reconstructed.factors == original_factors
