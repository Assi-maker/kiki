from datetime import UTC, datetime

from crypto_trading.schemas.event import Event
from crypto_trading.storage.repository import SQLiteRepository


def _ai_call_event(candidate_id: str, role: str, run_id: str, at: datetime) -> Event:
    return Event(
        event_id=f"AI_CALL_MADE:{candidate_id}:{role}:{run_id}",
        event_type="AI_CALL_MADE",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=at,
        run_id=run_id,
        schema_version=1,
        payload={"role": role},
    )


def test_record_ai_call_event_persists_a_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.record_ai_call_event(
        _ai_call_event("c-1", "risk", "run-1", datetime(2026, 8, 27, tzinfo=UTC))
    )

    row = repo._conn.execute("SELECT * FROM events WHERE event_type = 'AI_CALL_MADE'").fetchone()
    assert row is not None


def test_record_ai_call_event_is_idempotent_on_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    event = _ai_call_event("c-1", "risk", "run-1", datetime(2026, 8, 27, tzinfo=UTC))

    repo.record_ai_call_event(event)
    repo.record_ai_call_event(event)  # samma event_id - simulerar en retry

    count = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = 'AI_CALL_MADE'"
    ).fetchone()["n"]
    assert count == 1


def test_count_ai_calls_since_only_counts_events_at_or_after_cutoff(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.record_ai_call_event(
        _ai_call_event("c-1", "risk", "run-1", datetime(2026, 8, 27, 0, 30, tzinfo=UTC))
    )
    repo.record_ai_call_event(
        _ai_call_event("c-2", "risk", "run-1", datetime(2026, 8, 26, 23, 59, tzinfo=UTC))
    )

    count = repo.count_ai_calls_since(datetime(2026, 8, 27, 0, 0, tzinfo=UTC))
    assert count == 1
