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
from tests.crypto_trading.test_orchestrator import (
    _happy_fixtures,
    _settings,
    _SpyRunner,
    _StubExternalDataConnector,
    _StubNewsConnector,
)

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
        event_id=f"CANDIDATE_CREATED:{candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )
    repo.create_candidate_with_event(candidate, creation_event)
    if status != "CANDIDATE":
        transition_event = Event(
            event_id=f"CANDIDATE_TRANSITIONED:{candidate_id}:{status}",
            event_type="CANDIDATE_TRANSITIONED",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=_NOW,
            run_id="run-1",
            schema_version=1,
            payload={"from": "CANDIDATE", "to": status},
        )
        repo.transition_candidate_with_event(candidate_id, status, _NOW, transition_event)
    return candidate.model_copy(update={"status": status})


def test_run_discovery_cycle_sweeps_interrupted_analyses_first_and_resumes_it_same_cycle(
    tmp_path,
):
    """Sweepen (SPEC §8.5) körs fortfarande som steg 1 - verifierat via
    ANALYSIS_INTERRUPTED_DETECTED-audit-eventet - men med Fas 5:s
    återupptagningspolicy (PLAN_CRYPTO_PHASE5.md Beslut 2) plockas samma
    candidate nu upp och körs igenom hela rollkedjan i SAMMA
    run_discovery_cycle-anrop, istället för att bara lämnas i
    ANALYSIS_INTERRUPTED (det gamla, nu obsoleta beteendet innan
    återupptagningspolicyn fanns)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    stuck = _persisted_candidate_in_status(
        repo, "UNDER_AI_ANALYSIS"
    )  # föräldralös, simulerar krasch

    results = run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=_settings(), run_id="run-2"
    )

    swept_event = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_type = 'ANALYSIS_INTERRUPTED_DETECTED' "
        "AND aggregate_id = ?",
        (stuck.candidate_id,),
    ).fetchone()
    assert swept_event is not None  # sweepen körde fortfarande som steg 1

    assert len(results) == 1
    assert results[0].candidate_id == stuck.candidate_id
    reloaded = repo.get_candidate(stuck.candidate_id)
    assert reloaded.status == "CONFIRMED"  # återupptagen och fullt analyserad samma cykel


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


def test_analysis_interrupted_candidate_is_resumed_and_reaches_confirmed(tmp_path):
    """Simulerar en krasch: en candidate sitter i ANALYSIS_INTERRUPTED (redan
    svept av en tidigare sweep_interrupted_analyses-körning). Nästa
    run_discovery_cycle-anrop ska plocka upp den och köra hela rollkedjan."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "ANALYSIS_INTERRUPTED", candidate_id="interrupted-1")

    results = run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=_settings(), run_id="run-2"
    )

    assert len(results) == 1
    assert results[0].candidate_id == "interrupted-1"
    assert results[0].status == "CONFIRMED"


def test_daily_ai_call_cap_sends_candidate_to_budget_limited_not_rejected(tmp_path):
    """AC2: taket nås mitt i en flercykel-körning -> BUDGET_LIMITED, aldrig
    ett sakligt underkännande."""
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _settings(max_ai_calls_per_day=3)  # 3 räcker inte till en hel 7-rollsanalys
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="c-1")

    results = run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-1"
    )

    # candidate hann aldrig starta sin analys, men BUDGET_LIMITED-övergången
    # returneras ändå i results (samma synlighetsprincip som
    # candidate_engine.prioritize_and_apply_budget) - inga assessments satta.
    assert len(results) == 1
    assert results[0].candidate_id == "c-1"
    assert results[0].status == "BUDGET_LIMITED"
    assert results[0].risk is None
    assert repo.get_candidate("c-1").status == "BUDGET_LIMITED"


def test_daily_ai_call_cap_leaves_interrupted_candidate_untouched_not_budget_limited(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _settings(max_ai_calls_per_day=1)
    _persisted_candidate_in_status(repo, "ANALYSIS_INTERRUPTED", candidate_id="interrupted-1")
    # taket är redan uppnått innan cykeln ens börjar (simulerar att en
    # tidigare cykel samma dygn redan förbrukat hela det dagliga taket).
    repo.record_ai_call_event(
        Event(
            event_id="AI_CALL_MADE:other-candidate:risk:run-0",
            event_type="AI_CALL_MADE",
            aggregate_type="candidate",
            aggregate_id="other-candidate",
            occurred_at=_NOW,
            run_id="run-0",
            schema_version=1,
            payload={"role": "risk"},
        )
    )

    results = run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-1"
    )

    assert results == []
    assert repo.get_candidate("interrupted-1").status == "ANALYSIS_INTERRUPTED"


def test_daily_ai_call_cap_is_respected_across_two_separate_discovery_cycles(tmp_path):
    """Taket är persisterat - gäller även om en 'ny' run_discovery_cycle
    anropas (simulerar en ny cykel efter omstart), inte bara inom en."""
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _settings(max_ai_calls_per_day=7)  # exakt en candidates fulla analys
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="c-1")
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="c-2")

    run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-1"
    )
    run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-2"
    )

    statuses = {repo.get_candidate("c-1").status, repo.get_candidate("c-2").status}
    assert "BUDGET_LIMITED" in statuses
    assert "CONFIRMED" in statuses or "NO_TRADE" in statuses or "REJECTED" in statuses


def test_run_discovery_cycle_forwards_news_connector_to_orchestrator(tmp_path):
    """Fas 5.5 Task 3: bevisar den fulla vägen, inte bara att parametern
    accepteras - news_headlines/fear_greed_index ska faktiskt nå fram till
    news_sentiment-rollens context när run_discovery_cycle anropas MED
    connectors."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    spy = _SpyRunner(_happy_fixtures())

    run_discovery_cycle(
        repo=repo,
        runner=spy,
        settings=_settings(),
        run_id="run-1",
        news_connector=_StubNewsConnector(),
        external_data_connector=_StubExternalDataConnector(),
    )

    news_context = spy.captured_contexts["crypto-news-sentiment"]
    assert "news_headlines" in news_context
    assert "fear_greed_index" in news_context


def test_run_discovery_cycle_omits_news_keys_when_connectors_not_passed(tmp_path):
    """Connectors är valfria (Beslut 2/Global Constraints) - befintligt
    anropsmönster utan dem ska ge identiskt beteende som innan Task 3."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    spy = _SpyRunner(_happy_fixtures())

    run_discovery_cycle(repo=repo, runner=spy, settings=_settings(), run_id="run-1")

    news_context = spy.captured_contexts["crypto-news-sentiment"]
    assert "news_headlines" not in news_context
    assert "fear_greed_index" not in news_context
