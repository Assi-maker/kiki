from datetime import UTC, datetime

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.orchestrator import run_discovery_cycle
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
from tests.crypto_trading.test_orchestrator import _happy_fixtures, _settings

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _evidence() -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.8,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _persisted_candidate_in_status(repo, status: str, candidate_id: str = "cand-1") -> Candidate:
    candidate = Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CANDIDATE",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    creation_event = Event(
        event_id=f"CANDIDATE_CREATED:{candidate_id}", event_type="CANDIDATE_CREATED",
        aggregate_type="candidate", aggregate_id=candidate_id, occurred_at=_NOW,
        run_id="run-1", schema_version=1, payload={},
    )
    repo.create_candidate_with_event(candidate, creation_event)
    if status != "CANDIDATE":
        transition_event = Event(
            event_id=f"CANDIDATE_TRANSITIONED:{candidate_id}:{status}",
            event_type="CANDIDATE_TRANSITIONED", aggregate_type="candidate",
            aggregate_id=candidate_id, occurred_at=_NOW, run_id="run-1", schema_version=1,
            payload={"from": "CANDIDATE", "to": status},
        )
        repo.transition_candidate_with_event(candidate_id, status, _NOW, transition_event)
    return candidate.model_copy(update={"status": status})


def test_run_discovery_cycle_sweeps_interrupted_analyses_first(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    stuck = _persisted_candidate_in_status(repo, "UNDER_AI_ANALYSIS")  # föräldralös, simulerar krasch

    run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(fixtures={}), settings=_settings(), run_id="run-2"
    )

    reloaded = repo.get_candidate(stuck.candidate_id)
    assert reloaded.status == "ANALYSIS_INTERRUPTED"


def test_run_discovery_cycle_transitions_candidate_status_before_analysis(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    results = run_discovery_cycle(repo=repo, runner=runner, settings=_settings(), run_id="run-1")

    assert results[0].status == "CONFIRMED"


def test_run_discovery_cycle_processes_multiple_candidates(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="cand-1")
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="cand-2")
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    results = run_discovery_cycle(repo=repo, runner=runner, settings=_settings(), run_id="run-1")

    assert {r.candidate_id for r in results} == {"cand-1", "cand-2"}
    assert all(r.status == "CONFIRMED" for r in results)
