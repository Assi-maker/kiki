from datetime import UTC, datetime, timedelta

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.storage.repository import SQLiteRepository

_DAY_START = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
_BEFORE_DAY_START = _DAY_START - timedelta(hours=1)
_DURING_DAY = _DAY_START + timedelta(hours=5)


def _evidence() -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_DURING_DAY,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.8,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _candidate(
    candidate_id: str, status: str, created_at: datetime, updated_at: datetime
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status=status,
        evidence_record=_evidence(),
        created_at=created_at,
        updated_at=updated_at,
    )


def _event(candidate_id: str) -> Event:
    return Event(
        event_id=f"CANDIDATE_CREATED:{candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=_DURING_DAY,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def test_count_candidates_created_since_only_counts_on_or_after_cutoff(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    old = _candidate("cand-old", "CANDIDATE", _BEFORE_DAY_START, _BEFORE_DAY_START)
    new = _candidate("cand-new", "CANDIDATE", _DURING_DAY, _DURING_DAY)
    repo.create_candidate_with_event(old, _event("cand-old"))
    repo.create_candidate_with_event(new, _event("cand-new"))

    assert repo.count_candidates_created_since(_DAY_START) == 1


def test_count_candidates_by_status_since_filters_both_status_and_time(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    confirmed_today = _candidate("cand-1", "CONFIRMED", _BEFORE_DAY_START, _DURING_DAY)
    confirmed_yesterday = _candidate("cand-2", "CONFIRMED", _BEFORE_DAY_START, _BEFORE_DAY_START)
    rejected_today = _candidate("cand-3", "REJECTED", _BEFORE_DAY_START, _DURING_DAY)
    repo.create_candidate_with_event(confirmed_today, _event("cand-1"))
    repo.create_candidate_with_event(confirmed_yesterday, _event("cand-2"))
    repo.create_candidate_with_event(rejected_today, _event("cand-3"))

    assert repo.count_candidates_by_status_since("CONFIRMED", _DAY_START) == 1
    assert repo.count_candidates_by_status_since("REJECTED", _DAY_START) == 1
    assert repo.count_candidates_by_status_since("NO_TRADE", _DAY_START) == 0


def test_count_runs_by_status_since_filters_both_status_and_time(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-old", "discovery", _BEFORE_DAY_START)
    repo.complete_run("run-old", _BEFORE_DAY_START, "error", ["boom"])
    repo.start_run("run-new-ok", "discovery", _DURING_DAY)
    repo.complete_run("run-new-ok", _DURING_DAY, "ok", [])
    repo.start_run("run-new-error", "monitoring", _DURING_DAY)
    repo.complete_run("run-new-error", _DURING_DAY, "error", ["boom"])

    assert repo.count_runs_by_status_since("error", _DAY_START) == 1
    assert repo.count_runs_by_status_since("ok", _DAY_START) == 1


def test_sum_instruments_scanned_since_sums_only_discovery_runs_today(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-old", "discovery", _BEFORE_DAY_START)
    repo.complete_run("run-old", _BEFORE_DAY_START, "ok", [], instruments_scanned=500)
    repo.start_run("run-1", "discovery", _DURING_DAY)
    repo.complete_run("run-1", _DURING_DAY, "ok", [], instruments_scanned=1119)
    repo.start_run("run-2", "discovery", _DURING_DAY)
    repo.complete_run("run-2", _DURING_DAY, "ok", [], instruments_scanned=1120)
    repo.start_run("run-monitoring", "monitoring", _DURING_DAY)
    repo.complete_run("run-monitoring", _DURING_DAY, "ok", [])

    assert repo.sum_instruments_scanned_since(_DAY_START) == 1119 + 1120


def test_sum_instruments_scanned_since_returns_zero_when_no_discovery_runs(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.sum_instruments_scanned_since(_DAY_START) == 0
