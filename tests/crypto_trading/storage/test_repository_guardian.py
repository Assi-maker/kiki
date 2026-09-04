from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _observation(position_id="pos-1", observed_at=_NOW, state="HOLD", observation_id=None):
    return GuardianObservation(
        observation_id=observation_id or f"{position_id}:{observed_at.isoformat()}",
        position_id=position_id,
        observed_at=observed_at,
        state=state,
        decay_score=Decimal("0.2"),
        progress_ratio=Decimal("0.3"),
        unrealized_pnl=Decimal("10.5"),
        factors={"time_decay": 0.1, "momentum_decay": 0.3},
        ai_reasoning=None,
        ai_cost_usd=None,
        run_id="run-1",
    )


def test_save_guardian_observation_persists_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    created = repo.save_guardian_observation(_observation())

    assert created is True
    row = repo.find_latest_guardian_observation("pos-1")
    assert row["state"] == "HOLD"
    assert row["decay_score"] == "0.2"


def test_save_guardian_observation_is_idempotent_on_duplicate_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    obs = _observation()

    first = repo.save_guardian_observation(obs)
    second = repo.save_guardian_observation(obs)

    assert first is True
    assert second is False


def test_find_latest_guardian_observation_returns_none_when_absent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.find_latest_guardian_observation("pos-nope") is None


def test_find_latest_guardian_observation_returns_the_most_recent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_observation(_observation(observed_at=_NOW, state="HOLD"))
    repo.save_guardian_observation(_observation(observed_at=_NOW + timedelta(minutes=1), state="WATCH"))

    latest = repo.find_latest_guardian_observation("pos-1")

    assert latest["state"] == "WATCH"


def test_find_guardian_observations_for_position_returns_chronological_history(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_observation(_observation(observed_at=_NOW, state="HOLD"))
    repo.save_guardian_observation(_observation(observed_at=_NOW + timedelta(minutes=1), state="WATCH"))

    history = repo.find_guardian_observations_for_position("pos-1")

    assert [h["state"] for h in history] == ["HOLD", "WATCH"]


def test_guardian_observation_never_writes_to_positions_table(tmp_path):
    """Isolation guarantee (design doc §2/§10)."""
    from crypto_trading.schemas.event import Event
    from crypto_trading.schemas.trade import Position

    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    before = repo.get_position("pos-1")

    repo.save_guardian_observation(_observation())

    after = repo.get_position("pos-1")
    assert after == before
