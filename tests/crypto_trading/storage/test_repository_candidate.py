import json
import sqlite3
from datetime import UTC, datetime

import pytest

from crypto_trading.schemas.assessments import RiskAssessment
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


def _risk_assessment() -> RiskAssessment:
    return RiskAssessment(
        agent_name="crypto-risk-agent",
        run_id="run-1",
        created_at=datetime.now(UTC),
        status="ok",
        suggested_stop_loss="1",
        suggested_target="2",
        downside="d",
        liquidity_risk="l",
        model_risk="m",
        timing_risk="t",
    )


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


def _make_candidate(
    candidate_id="cand-1", idempotency_key="key-1", status="CANDIDATE"
) -> Candidate:
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
    row = repo._conn.execute(
        "SELECT event_type FROM events WHERE event_id = ?", (event.event_id,)
    ).fetchone()
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
    repo._conn.execute("UPDATE candidates SET status = 'GARBAGE' WHERE candidate_id = 'cand-1'")
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
    # korrumpera evidence_record, INTE status - annars matchar WHERE status=...
    # inte längre raden
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' "
        "WHERE candidate_id = 'cand-corrupt'"
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


def test_find_latest_candidate_by_instrument_and_status_returns_none_when_no_match(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED") is None


def test_find_latest_candidate_by_instrument_and_status_returns_most_recent(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    older = _make_candidate(candidate_id="cand-old", idempotency_key="key-old", status="REJECTED")
    newer = _make_candidate(candidate_id="cand-new", idempotency_key="key-new", status="REJECTED")
    older = older.model_copy(update={"created_at": datetime(2026, 8, 20, tzinfo=UTC)})
    newer = newer.model_copy(update={"created_at": datetime(2026, 8, 21, tzinfo=UTC)})
    repo.create_candidate_with_event(older, _make_event(older, "CANDIDATE_CREATED"))
    repo.create_candidate_with_event(newer, _make_event(newer, "CANDIDATE_CREATED"))

    result = repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED")

    assert result is not None
    assert result.candidate_id == "cand-new"


def test_find_latest_candidate_by_instrument_and_status_ignores_other_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(status="CANDIDATE")
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    assert repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED") is None


def test_find_latest_candidate_by_instrument_and_status_propagates_corrupt_state_error(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(status="REJECTED")
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' WHERE candidate_id = 'cand-1'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError):
        repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED")


def test_save_assessment_persists_and_get_candidate_reloads_it(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    repo.save_assessment(candidate.candidate_id, "risk", _risk_assessment())

    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded.risk is not None
    assert reloaded.risk.downside == "d"
    assert reloaded.news_sentiment is None  # oskrivna fält förblir None


def test_save_assessment_is_idempotent_overwrite_on_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    repo.save_assessment(candidate.candidate_id, "risk", _risk_assessment())
    updated = _risk_assessment().model_copy(update={"downside": "changed"})
    repo.save_assessment(candidate.candidate_id, "risk", updated)

    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded.risk.downside == "changed"
    count = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM assessments WHERE candidate_id = ?", (candidate.candidate_id,)
    ).fetchone()["n"]
    assert count == 1  # overwrite, inte dubblett


def test_get_candidate_raises_corrupt_state_error_on_corrupt_assessment(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))
    repo.save_assessment(candidate.candidate_id, "risk", _risk_assessment())
    repo._conn.execute(
        "UPDATE assessments SET payload = 'not valid json' "
        "WHERE candidate_id = ? AND field_name = 'risk'",
        (candidate.candidate_id,),
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate(candidate.candidate_id)

    assert exc_info.value.corrupted_field == "assessment:risk"


def test_save_gate_decision_persists_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    repo.save_gate_decision(
        candidate.candidate_id,
        decision="CONFIRMED",
        reasons=["all checks passed"],
        evaluated_at=datetime.now(UTC),
    )

    row = repo._conn.execute(
        "SELECT decision, reasons FROM gate_decisions WHERE candidate_id = ?",
        (candidate.candidate_id,),
    ).fetchone()
    assert row["decision"] == "CONFIRMED"
    assert "all checks passed" in row["reasons"]


def test_save_gate_decision_is_idempotent_overwrite_on_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    repo.save_gate_decision(
        candidate.candidate_id, decision="NO_TRADE", reasons=["r1"], evaluated_at=datetime.now(UTC)
    )
    repo.save_gate_decision(
        candidate.candidate_id, decision="CONFIRMED", reasons=["r2"], evaluated_at=datetime.now(UTC)
    )

    count = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM gate_decisions WHERE candidate_id = ?", (candidate.candidate_id,)
    ).fetchone()["n"]
    assert count == 1
    row = repo._conn.execute(
        "SELECT decision FROM gate_decisions WHERE candidate_id = ?", (candidate.candidate_id,)
    ).fetchone()
    assert row["decision"] == "CONFIRMED"


def test_count_open_positions_returns_zero_when_none(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.count_open_positions() == 0


def test_count_open_positions_counts_only_open_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    repo._conn.execute(
        "INSERT INTO positions (position_id, candidate_id, instrument, direction, status, "
        "theoretical_entry, simulated_fill_entry, stop_loss, target, size, "
        "fill_model_version, opened_at) VALUES "
        "('p1','c1','BTCUSDT','LONG','OPEN_POSITION','1','1','1','1','1','v1','2026-08-26')"
    )
    repo._conn.execute(
        "INSERT INTO positions (position_id, candidate_id, instrument, direction, status, "
        "theoretical_entry, simulated_fill_entry, stop_loss, target, size, "
        "fill_model_version, opened_at) VALUES "
        "('p2','c2','ETHUSDT','LONG','CLOSED','1','1','1','1','1','v1','2026-08-26')"
    )
    repo._conn.commit()

    assert repo.count_open_positions() == 1


def test_repository_protocol_exposes_no_update_or_delete_event_method():
    assert not hasattr(SQLiteRepository, "update_event")
    assert not hasattr(SQLiteRepository, "delete_event")


class _FailingConnection:
    """Wrapper runt en riktig sqlite3.Connection som injicerar ett fel på ett
    specifikt execute()-anrop. sqlite3.Connection är en C-typ vars execute-
    attribut är skrivskyddat per instans - kan inte monkeypatchas direkt,
    därför den här tunna wrappern istället (byts in på repo._conn, som är ett
    vanligt Python-attribut)."""

    def __init__(self, real_conn, fail_on_call_number: int):
        self._real_conn = real_conn
        self._fail_on_call_number = fail_on_call_number
        self._call_count = 0

    def execute(self, sql, *args, **kwargs):
        self._call_count += 1
        if self._call_count == self._fail_on_call_number:
            raise sqlite3.OperationalError(
                "simulated failure between state-update and event-insert"
            )
        return self._real_conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self._real_conn.commit()

    def rollback(self):
        return self._real_conn.rollback()


def test_transition_candidate_with_event_rolls_back_atomically_on_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    creation_event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, creation_event)

    real_conn = repo._conn
    # anrop 1 = UPDATE candidates, anrop 2 = event-INSERT - fel injiceras exakt där
    repo._conn = _FailingConnection(real_conn, fail_on_call_number=2)
    transition_event = _make_event(candidate, "CANDIDATE_TO_UNDER_ANALYSIS")

    with pytest.raises(sqlite3.OperationalError):
        repo.transition_candidate_with_event(
            "cand-1", "UNDER_AI_ANALYSIS", datetime.now(UTC), transition_event
        )

    repo._conn = real_conn
    reloaded = repo.get_candidate("cand-1")
    assert reloaded.status == "CANDIDATE"  # oförändrat - rollback fungerade
    event_row = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (transition_event.event_id,)
    ).fetchone()
    assert event_row is None  # eventet skrevs aldrig heller
