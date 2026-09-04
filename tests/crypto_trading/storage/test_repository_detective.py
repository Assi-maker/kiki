from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.detective import DetectiveAnalysisRecord
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _closed_position(position_id: str, closed_at: datetime) -> Position:
    return Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTCUSDT",
        direction="LONG",
        status="CLOSED",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=Decimal("49000"),
        target=Decimal("52000"),
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=closed_at,
        theoretical_exit=Decimal("52000"),
        simulated_fill_exit=Decimal("51975"),
        exit_reason="target",
        fees=Decimal("0.4"),
        funding=Decimal("0"),
        closed_at=closed_at,
    )


def _seed_closed_position(repo: SQLiteRepository, position_id: str, closed_at: datetime) -> None:
    position = _closed_position(position_id, closed_at)
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=closed_at,
        run_id="seed",
        schema_version=1,
        payload={},
    )
    repo.create_position_with_event(position.model_copy(update={"status": "OPEN_POSITION"}), event)
    repo.close_position_with_event(
        position_id=position_id,
        theoretical_exit=position.theoretical_exit,
        simulated_fill_exit=position.simulated_fill_exit,
        exit_reason=position.exit_reason,
        fees=position.fees,
        funding=position.funding,
        closed_at=closed_at,
        event=Event(
            event_id=f"POSITION_CLOSED:{position_id}",
            event_type="POSITION_CLOSED",
            aggregate_type="position",
            aggregate_id=position_id,
            occurred_at=closed_at,
            run_id="seed",
            schema_version=1,
            payload={},
        ),
    )


def _record(position_ids: list[str], analysis_id: str = "detective-1") -> DetectiveAnalysisRecord:
    return DetectiveAnalysisRecord(
        analysis_id=analysis_id,
        created_at=_NOW,
        position_ids=position_ids,
        win_count=1,
        loss_count=0,
        breakeven_count=0,
        status="ok",
        observations=["obs"],
        winning_patterns=["win"],
        losing_patterns=[],
        stats_snapshot={"a": 1},
        ai_cost_usd=Decimal("0.05"),
    )


def test_find_closed_positions_pending_detective_analysis_returns_oldest_first(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "pos-2", datetime(2026, 9, 2, tzinfo=UTC))
    _seed_closed_position(repo, "pos-1", datetime(2026, 9, 1, tzinfo=UTC))

    pending = repo.find_closed_positions_pending_detective_analysis(limit=10)

    assert [p.position_id for p in pending] == ["pos-1", "pos-2"]


def test_find_closed_positions_pending_detective_analysis_respects_limit(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(5):
        _seed_closed_position(repo, f"pos-{i}", datetime(2026, 9, 1, tzinfo=UTC))

    pending = repo.find_closed_positions_pending_detective_analysis(limit=2)

    assert len(pending) == 2


def test_count_closed_positions_pending_detective_analysis(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_position(repo, f"pos-{i}", datetime(2026, 9, 1, tzinfo=UTC))

    assert repo.count_closed_positions_pending_detective_analysis() == 3


def test_save_detective_analysis_persists_the_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "pos-1", _NOW)
    record = _record(["pos-1"])

    repo.save_detective_analysis(record)

    found = repo.find_detective_analyses(limit=10)
    assert len(found) == 1
    assert found[0].analysis_id == "detective-1"
    assert found[0].position_ids == ["pos-1"]
    assert found[0].ai_cost_usd == Decimal("0.05")


def test_save_detective_analysis_marks_its_positions_as_no_longer_pending(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "pos-1", _NOW)
    _seed_closed_position(repo, "pos-2", _NOW)
    assert repo.count_closed_positions_pending_detective_analysis() == 2

    repo.save_detective_analysis(_record(["pos-1"]))

    pending = repo.find_closed_positions_pending_detective_analysis(limit=10)
    assert [p.position_id for p in pending] == ["pos-2"]


def test_find_detective_analyses_orders_most_recent_first(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "pos-1", _NOW)
    _seed_closed_position(repo, "pos-2", _NOW)
    repo.save_detective_analysis(
        _record(["pos-1"], analysis_id="detective-1").model_copy(
            update={"created_at": datetime(2026, 9, 1, tzinfo=UTC)}
        )
    )
    repo.save_detective_analysis(
        _record(["pos-2"], analysis_id="detective-2").model_copy(
            update={"created_at": datetime(2026, 9, 2, tzinfo=UTC)}
        )
    )

    found = repo.find_detective_analyses(limit=10)

    assert [r.analysis_id for r in found] == ["detective-2", "detective-1"]
