from datetime import UTC, datetime

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.state_machine import sweep_interrupted_analyses
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


def _seed_candidate(repo: SQLiteRepository, candidate_id: str, status: str) -> None:
    now = datetime.now(UTC)
    candidate = Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-old",
        evidence_hash="hash-1",
        status="CANDIDATE",
        evidence_record=_make_evidence(),
        created_at=now,
        updated_at=now,
    )
    creation_event = Event(
        event_id=f"CANDIDATE_CREATED:{candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=now,
        run_id="run-old",
        schema_version=1,
        payload={},
    )
    repo.create_candidate_with_event(candidate, creation_event)
    if status != "CANDIDATE":
        transition_event = Event(
            event_id=f"MOVE_TO_{status}:{candidate_id}",
            event_type=f"MOVE_TO_{status}",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=now,
            run_id="run-old",
            schema_version=1,
            payload={},
        )
        repo.transition_candidate_with_event(candidate_id, status, now, transition_event)


def test_sweep_moves_under_analysis_candidates_to_interrupted(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-1", "UNDER_AI_ANALYSIS")
    _seed_candidate(repo, "stuck-2", "UNDER_AI_ANALYSIS")
    _seed_candidate(repo, "not-stuck", "CANDIDATE")

    swept_at = datetime.now(UTC)
    interrupted_ids = sweep_interrupted_analyses(repo, swept_at, run_id="startup-run-1")

    assert set(interrupted_ids) == {"stuck-1", "stuck-2"}
    assert repo.get_candidate("stuck-1").status == "ANALYSIS_INTERRUPTED"
    assert repo.get_candidate("stuck-2").status == "ANALYSIS_INTERRUPTED"
    assert repo.get_candidate("not-stuck").status == "CANDIDATE"


def test_sweep_writes_one_event_per_interrupted_candidate(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-1", "UNDER_AI_ANALYSIS")

    sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="startup-run-1")

    row = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = 'ANALYSIS_INTERRUPTED_DETECTED'"
    ).fetchone()
    assert row["n"] == 1


def test_sweep_never_transitions_analysis_interrupted_back_to_under_analysis(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "already-interrupted", "ANALYSIS_INTERRUPTED")

    interrupted_ids = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="startup-run-2")

    assert interrupted_ids == []
    assert repo.get_candidate("already-interrupted").status == "ANALYSIS_INTERRUPTED"


def test_sweep_is_idempotent_on_repeated_calls(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-1", "UNDER_AI_ANALYSIS")

    first = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="run-a")
    second = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="run-b")

    assert first == ["stuck-1"]
    assert second == []  # redan ANALYSIS_INTERRUPTED efter första sweepen, inget kvar att svepa


def test_sweep_continues_past_corrupt_candidate_and_still_interrupts_valid_ones(tmp_path):
    """En korrupt candidate bland flera UNDER_AI_ANALYSIS-rader ska aldrig
    blockera sweepen från att behandla övriga, giltiga candidates."""
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-valid", "UNDER_AI_ANALYSIS")
    _seed_candidate(repo, "stuck-corrupt", "UNDER_AI_ANALYSIS")

    # korrumpera evidence_record, INTE status - så att raden fortfarande
    # matchar UNDER_AI_ANALYSIS-frågan men inte kan deserialiseras fullt ut.
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' "
        "WHERE candidate_id = 'stuck-corrupt'"
    )
    repo._conn.commit()

    interrupted_ids = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="startup-run-3")

    assert interrupted_ids == ["stuck-valid"]  # korrupt candidate blockerade inte den giltiga
    assert repo.get_candidate("stuck-valid").status == "ANALYSIS_INTERRUPTED"

    corrupt_event = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'stuck-corrupt'"
    ).fetchone()
    assert corrupt_event is not None  # den korrupta candidate:n auditerades ändå
