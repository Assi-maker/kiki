from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.storage.repository import SQLiteRepository


def _ai_call_event(
    candidate_id: str, role: str, run_id: str, at: datetime, cost_usd: str | None = None
) -> Event:
    payload = {"role": role}
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    return Event(
        event_id=f"AI_CALL_MADE:{candidate_id}:{role}:{run_id}",
        event_type="AI_CALL_MADE",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=at,
        run_id=run_id,
        schema_version=1,
        payload=payload,
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


def test_sum_ai_cost_since_sums_persisted_cost_across_matching_events(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.record_ai_call_event(
        _ai_call_event(
            "c-1", "risk", "run-1", datetime(2026, 8, 27, 0, 30, tzinfo=UTC), cost_usd="0.0186"
        )
    )
    repo.record_ai_call_event(
        _ai_call_event(
            "c-1", "qa", "run-1", datetime(2026, 8, 27, 0, 31, tzinfo=UTC), cost_usd="0.0511"
        )
    )

    total = repo.sum_ai_cost_since(datetime(2026, 8, 27, 0, 0, tzinfo=UTC))
    assert total == Decimal("0.0697")


def test_sum_ai_cost_since_only_counts_events_at_or_after_cutoff(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.record_ai_call_event(
        _ai_call_event(
            "c-1", "risk", "run-1", datetime(2026, 8, 27, 0, 30, tzinfo=UTC), cost_usd="0.02"
        )
    )
    repo.record_ai_call_event(
        _ai_call_event(
            "c-2", "risk", "run-1", datetime(2026, 8, 26, 23, 59, tzinfo=UTC), cost_usd="0.02"
        )
    )

    total = repo.sum_ai_cost_since(datetime(2026, 8, 27, 0, 0, tzinfo=UTC))
    assert total == Decimal("0.02")


def test_sum_ai_cost_since_returns_zero_for_no_matching_events(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    total = repo.sum_ai_cost_since(datetime(2026, 8, 27, 0, 0, tzinfo=UTC))
    assert total == Decimal("0")


def test_ai_cost_survives_repository_reopen_simulating_process_restart(tmp_path):
    """Krav 5 (restart safety): kostnaden är persisterad i DB-filen, inte i
    processminnet - en ny Repository-instans mot SAMMA fil (simulerar en
    omstart av run.py) måste se exakt samma ackumulerade kostnad."""
    db_path = tmp_path / "t.db"
    repo = SQLiteRepository(db_path)
    repo.record_ai_call_event(
        _ai_call_event(
            "c-1", "risk", "run-1", datetime(2026, 8, 27, 0, 30, tzinfo=UTC), cost_usd="1.2345"
        )
    )
    del repo

    reopened = SQLiteRepository(db_path)
    assert reopened.sum_ai_cost_since(datetime(2026, 8, 27, 0, 0, tzinfo=UTC)) == Decimal("1.2345")
    assert reopened.count_ai_calls_since(datetime(2026, 8, 27, 0, 0, tzinfo=UTC)) == 1
