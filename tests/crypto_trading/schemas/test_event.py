from datetime import UTC, datetime

from crypto_trading.schemas.event import Event


def test_event_roundtrips_all_fields():
    event = Event(
        event_id="CANDIDATE_CREATED:abc-123",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id="abc-123",
        occurred_at=datetime.now(UTC),
        run_id="run-1",
        schema_version=1,
        payload={"instrument": "BTCUSDT"},
    )
    assert event.event_type == "CANDIDATE_CREATED"
    assert event.payload["instrument"] == "BTCUSDT"


def test_event_run_id_is_optional():
    event = Event(
        event_id="CORRUPT_STATE_DETECTED:abc-123:X",
        event_type="CORRUPT_STATE_DETECTED",
        aggregate_type="candidate",
        aggregate_id="abc-123",
        occurred_at=datetime.now(UTC),
        run_id=None,
        schema_version=1,
        payload={"raw_status": "X"},
    )
    assert event.run_id is None
