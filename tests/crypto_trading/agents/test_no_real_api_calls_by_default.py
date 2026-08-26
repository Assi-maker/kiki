"""AC7: default pytest-körning kräver noll Claude API-anrop."""

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


def test_importing_agent_runner_module_requires_no_api_key(monkeypatch):
    """RealClaudeRunner konstruerar en Anthropic-klient bara när den faktiskt
    instansieras av en anropare - aldrig vid modulimport. Detta test bevisar
    att bara importera crypto_trading.agents.runner (vilket varje test i
    default-sviten gör transitivt) inte i sig kräver en nyckel."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import importlib

    import crypto_trading.agents.runner as runner_module

    importlib.reload(runner_module)
    assert hasattr(runner_module, "RealClaudeRunner")


def test_full_discovery_cycle_works_end_to_end_without_anthropic_api_key(tmp_path, monkeypatch):
    """Funktionellt bevis: hela discovery-cykeln (sju roller, QA-gate,
    Risk/Signal Gate) körs klart via MockAgentRunner utan att
    ANTHROPIC_API_KEY är satt alls."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5
        ),
        candidate_score=0.8,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )
    candidate = Candidate(
        candidate_id="cand-1", idempotency_key="key-1", instrument="BTCUSDT",
        discovery_run_id="run-1", evidence_hash="hash-1", status="CANDIDATE",
        evidence_record=evidence, created_at=_NOW, updated_at=_NOW,
    )
    repo.create_candidate_with_event(
        candidate,
        Event(
            event_id="CANDIDATE_CREATED:cand-1", event_type="CANDIDATE_CREATED",
            aggregate_type="candidate", aggregate_id="cand-1", occurred_at=_NOW,
            run_id="run-1", schema_version=1, payload={},
        ),
    )

    runner = MockAgentRunner(fixtures=_happy_fixtures())
    results = run_discovery_cycle(repo=repo, runner=runner, settings=_settings(), run_id="run-1")

    assert results[0].status == "CONFIRMED"
