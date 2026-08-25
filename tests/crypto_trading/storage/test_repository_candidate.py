import json
import sqlite3
from datetime import UTC, datetime

import pytest

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import SQLiteRepository


def _make_evidence() -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=datetime.now(UTC),
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="pct_change_1h", value=3.2, baseline=0.5, threshold=2.0
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=False, metric="rsi", value=55.0, baseline=50.0, threshold=70.0
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="volume_zscore", value=3.1, baseline=1.0, threshold=2.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=False, metric="funding_rate", value=0.01, baseline=0.01, threshold=0.05
        ),
        candidate_score=0.71,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _make_candidate(candidate_id="cand-1", idempotency_key="key-1", status="CANDIDATE") -> Candidate:
    now = datetime.now(UTC)
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=idempotency_key,
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status=status,
        evidence_record=_make_evidence(),
        created_at=now,
        updated_at=now,
    )


def _make_event(candidate: Candidate, event_type: str) -> Event:
    return Event(
        event_id=f"{event_type}:{candidate.candidate_id}",
        event_type=event_type,
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=datetime.now(UTC),
        run_id=candidate.discovery_run_id,
        schema_version=1,
        payload={"instrument": candidate.instrument},
    )


def test_create_candidate_with_event_persists_both(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    event = _make_event(candidate, "CANDIDATE_CREATED")

    created = repo.create_candidate_with_event(candidate, event)

    assert created is True
    reloaded = repo.get_candidate("cand-1")
    assert reloaded is not None
    assert reloaded.status == "CANDIDATE"
    row = repo._conn.execute("SELECT event_type FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
    assert row is not None
    assert row["event_type"] == "CANDIDATE_CREATED"


def test_create_candidate_with_event_is_idempotent_on_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    event = _make_event(candidate, "CANDIDATE_CREATED")

    first = repo.create_candidate_with_event(candidate, event)
    second = repo.create_candidate_with_event(candidate, event)

    assert first is True
    assert second is False  # idempotent no-op, ingen dubblett
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1
    event_count = repo._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert event_count == 1


def test_get_candidate_returns_none_when_missing(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.get_candidate("does-not-exist") is None


def test_get_candidate_raises_corrupt_state_error_on_unrecognized_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, event)

    # simulera datakorruption: skriv ett ogiltigt status-värde direkt
    repo._conn.execute(
        "UPDATE candidates SET status = 'GARBAGE' WHERE candidate_id = 'cand-1'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate("cand-1")

    assert exc_info.value.candidate_id == "cand-1"
    assert exc_info.value.raw_status == "GARBAGE"
    assert exc_info.value.corrupted_field == "status"

    corrupt_event = repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-1'"
    ).fetchone()
    assert corrupt_event is not None
    assert json.loads(corrupt_event["payload"])["corrupted_field"] == "status"


def test_get_candidate_raises_corrupt_state_error_on_corrupt_evidence_record(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(candidate_id="cand-2", idempotency_key="key-2")
    event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, event)

    # simulera datakorruption i evidence_record-kolumnen, INTE status
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' WHERE candidate_id = 'cand-2'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate("cand-2")

    assert exc_info.value.candidate_id == "cand-2"
    assert exc_info.value.corrupted_field == "evidence_record"

    corrupt_event = repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-2'"
    ).fetchone()
    assert corrupt_event is not None
    assert json.loads(corrupt_event["payload"])["corrupted_field"] == "evidence_record"


def test_get_candidate_raises_corrupt_state_error_on_corrupt_timestamp(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(candidate_id="cand-3", idempotency_key="key-3")
    event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, event)

    # simulera datakorruption i created_at-kolumnen
    repo._conn.execute(
        "UPDATE candidates SET created_at = 'not-a-timestamp' WHERE candidate_id = 'cand-3'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate("cand-3")

    assert exc_info.value.candidate_id == "cand-3"
    assert exc_info.value.corrupted_field == "timestamp"

    corrupt_event = repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-3'"
    ).fetchone()
    assert corrupt_event is not None
    assert json.loads(corrupt_event["payload"])["corrupted_field"] == "timestamp"


def test_find_candidates_by_status_skips_corrupt_rows_and_keeps_valid_ones(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")

    valid = _make_candidate(candidate_id="cand-valid", idempotency_key="key-valid")
    repo.create_candidate_with_event(valid, _make_event(valid, "CANDIDATE_CREATED"))

    corrupt = _make_candidate(candidate_id="cand-corrupt", idempotency_key="key-corrupt")
    repo.create_candidate_with_event(corrupt, _make_event(corrupt, "CANDIDATE_CREATED"))
    # korrumpera evidence_record, INTE status - annars matchar WHERE status=... inte längre raden
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' WHERE candidate_id = 'cand-corrupt'"
    )
    repo._conn.commit()

    result = repo.find_candidates_by_status("CANDIDATE")

    result_ids = {c.candidate_id for c in result}
    assert result_ids == {"cand-valid"}  # korrupt rad hoppades över, avbröt inte resten

    corrupt_event = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-corrupt'"
    ).fetchone()
    assert corrupt_event is not None  # ändå auditerad, trots att den uteslöts ur resultatet


def test_repository_protocol_exposes_no_update_or_delete_event_method():
    assert not hasattr(SQLiteRepository, "update_event")
    assert not hasattr(SQLiteRepository, "delete_event")
