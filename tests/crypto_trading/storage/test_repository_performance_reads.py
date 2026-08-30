from datetime import UTC, datetime

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _position(position_id: str, status: str = "OPEN_POSITION", opened_hour: int = 10) -> Position:
    data = dict(
        position_id=position_id,
        candidate_id=f"cand-{position_id}",
        instrument="BTCUSDT",
        direction="LONG",
        status=status,
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=datetime(2026, 8, 30, opened_hour, tzinfo=UTC),
    )
    if status == "CLOSED":
        data.update(
            theoretical_exit="52000",
            simulated_fill_exit="51980",
            exit_reason="target",
            fees="2",
            funding="1",
            closed_at=datetime(2026, 8, 30, opened_hour + 1, tzinfo=UTC),
        )
    return Position(**data)


def _position_event(position: Position, event_type: str = "POSITION_OPENED") -> Event:
    return Event(
        event_id=f"{event_type}:{position.position_id}",
        event_type=event_type,
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def _forecast(forecast_id: str, actual_outcome: str | None = None) -> ForecastRecord:
    return ForecastRecord(
        forecast_id=forecast_id,
        candidate_id=f"cand-{forecast_id}",
        instrument="BTCUSDT",
        forecast_timestamp=_NOW,
        horizon="4h",
        scenario_probabilities={"bullish": 0.6, "bearish": 0.4},
        forecast_version="v1",
        market_state_metadata={},
        actual_outcome=actual_outcome,
    )


# --- find_closed_positions ---------------------------------------------------


def test_find_closed_positions_returns_empty_on_empty_db(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_closed_positions() == []


def test_find_closed_positions_returns_only_closed_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    open_pos = _position("pos-open", status="OPEN_POSITION")
    closed_pos = _position("pos-closed", status="CLOSED")
    repo.create_position_with_event(open_pos, _position_event(open_pos))
    repo.create_position_with_event(closed_pos, _position_event(closed_pos))

    result = repo.find_closed_positions()

    assert [p.position_id for p in result] == ["pos-closed"]
    assert result[0].status == "CLOSED"


def test_find_closed_positions_returns_unbounded_beyond_dashboard_page_cap(tmp_path):
    """Fas 7:s dashboard-pagineringstak (_MAX_PAGE_LIMIT=500) gäller INTE
    denna metod - en aggregatberäkning över hela historiken behöver alla
    rader, inte en sida. Skapar 501 stängda positioner (ett mer än Fas 7:s
    tak) och bevisar samtliga returneras."""
    repo = SQLiteRepository(tmp_path / "test.db")
    for i in range(501):
        pos = _position(f"pos-{i}", status="CLOSED", opened_hour=1)
        repo.create_position_with_event(pos, _position_event(pos))

    result = repo.find_closed_positions()

    assert len(result) == 501


# --- find_forecasts_with_outcome ---------------------------------------------


def test_find_forecasts_with_outcome_returns_empty_on_empty_db(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_forecasts_with_outcome() == []


def test_find_forecasts_with_outcome_excludes_null_outcome(tmp_path):
    """save_forecast_record() skriver medvetet aldrig actual_outcome (Fas
    5.5-beslut, ingen outcome-writer finns - PLAN_CRYPTO_PHASE8.md §0). Ett
    utfall seedas därför direkt via rå SQL i testet, INTE via
    Repository:ns publika skriv-API - exakt det mönster planen redan
    beskrev för att testa denna metod utan att bygga en outcome-writer."""
    repo = SQLiteRepository(tmp_path / "test.db")
    repo.save_forecast_record(_forecast("fc-no-outcome"))
    repo.save_forecast_record(_forecast("fc-with-outcome"))
    repo._conn.execute(
        "UPDATE forecasts SET actual_outcome = 'bullish' WHERE forecast_id = 'fc-with-outcome'"
    )
    repo._conn.commit()

    result = repo.find_forecasts_with_outcome()

    assert [f.forecast_id for f in result] == ["fc-with-outcome"]
    assert result[0].actual_outcome == "bullish"
