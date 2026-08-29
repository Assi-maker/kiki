from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    PipelineConfig,
    RiskLimitsConfig,
    Settings,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.orchestrator import Orchestrator
from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.state_machine import can_transition
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _settings(
    max_ai_calls_per_discovery_run: int = 70, max_ai_calls_per_day: int = 500
) -> Settings:
    return Settings(
        db_path="unused",
        pipeline=PipelineConfig(
            discovery_interval_minutes=15,
            monitoring_interval_seconds=30,
            top_n=30,
            cooldown_minutes=60,
            max_data_age_seconds={
                "ticker": 30,
                "kline": 120,
                "funding_rate": 3600,
                "open_interest": 300,
                "contracts": 86400,
            },
            min_sample_size_for_calibration=30,
            calibration_preliminary_sample_size=10,
            sqlite_busy_timeout_ms=5000,
            required_fields={
                "ticker": ["lastPrice"],
                "kline": ["open"],
                "funding_rate": ["fundingRate"],
                "open_interest": ["openInterest"],
                "contracts": ["symbol"],
            },
            screener_timeframes=["1h"],
            bingx_base_url="https://open-api.bingx.com",
            bingx_requests_per_second=10,
            bingx_cache_ttl_seconds=5,
            bingx_max_retries=3,
            kline_consistency_tolerance_pct=Decimal("0.5"),
            eligibility_min_quote_volume_24h_usdt=Decimal("5000000"),
            eligibility_max_spread_pct=Decimal("0.002"),
            screener_lookback_periods=20,
            screener_price_volatility_threshold_pct=Decimal("2.0"),
            screener_rsi_period=14,
            screener_rsi_overbought_threshold=Decimal("70"),
            screener_volume_zscore_threshold=Decimal("2.5"),
            screener_funding_rate_threshold_pct=Decimal("0.05"),
            screener_funding_history_limit=10,
            evidence_change_threshold_for_reanalysis=Decimal("0.15"),
        ),
        risk_limits=RiskLimitsConfig(
            starting_capital_usdt=Decimal("10000"),
            risk_per_trade_pct=Decimal("0.01"),
            max_concurrent_positions=5,
            max_total_exposure_pct=Decimal("0.25"),
            spread_pct=Decimal("0.0005"),
            slippage_pct=Decimal("0.0005"),
            fee_pct=Decimal("0.0004"),
            max_position_hold_hours=24,
        ),
        budget_limits=BudgetLimitsConfig(
            max_candidates_per_discovery_run=10,
            max_ai_calls_per_discovery_run=max_ai_calls_per_discovery_run,
            max_ai_calls_per_day=max_ai_calls_per_day,
            warning_threshold_pct=Decimal("0.8"),
        ),
    )


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


def _persisted_candidate_in_under_ai_analysis(repo) -> Candidate:
    candidate = Candidate(
        candidate_id="cand-1",
        idempotency_key="key-1",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CANDIDATE",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    creation_event = Event(
        event_id="CANDIDATE_CREATED:cand-1",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id="cand-1",
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )
    repo.create_candidate_with_event(candidate, creation_event)
    transition_event = Event(
        event_id="CANDIDATE_TRANSITIONED:cand-1:UNDER_AI_ANALYSIS",
        event_type="CANDIDATE_TRANSITIONED",
        aggregate_type="candidate",
        aggregate_id="cand-1",
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={"from": "CANDIDATE", "to": "UNDER_AI_ANALYSIS"},
    )
    repo.transition_candidate_with_event("cand-1", "UNDER_AI_ANALYSIS", _NOW, transition_event)
    return candidate.model_copy(update={"status": "UNDER_AI_ANALYSIS"})


def _happy_fixtures() -> dict:
    return {
        "crypto-news-sentiment": NewsSentimentAssessment(
            agent_name="crypto-news-sentiment",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            verified_facts=["f"],
            source_claims=["c"],
            interpretation="i",
        ),
        "crypto-technical-analyst": TechnicalAssessment(
            agent_name="crypto-technical-analyst",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            market_data={},
            interpretation="i",
        ),
        "crypto-bull-thesis": BullThesisAssessment(
            agent_name="crypto-bull-thesis",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            hypothesis="h",
            catalyst="c",
            setup="s",
        ),
        "crypto-forecast-agent": ForecastAssessment(
            agent_name="crypto-forecast-agent",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            scenario_probabilities={"bullish": 0.6, "neutral": 0.3, "bearish": 0.1},
            horizon="4h",
            forecast_version="v1",
        ),
        "crypto-risk-agent": RiskAssessment(
            agent_name="crypto-risk-agent",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            suggested_stop_loss="1",
            suggested_target="2",
            downside="d",
            liquidity_risk="l",
            model_risk="m",
            timing_risk="t",
        ),
        "crypto-bear-adversarial": BearAdversarialAssessment(
            agent_name="crypto-bear-adversarial",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            counterarguments=["c"],
            alternative_explanations=["a"],
            falsification_conditions="f",
        ),
        "crypto-qa-gate": QAAssessment(
            agent_name="crypto-qa-gate",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            passed=True,
            violations=[],
        ),
    }


def test_process_candidate_reaches_confirmed_on_full_happy_path(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    orch = Orchestrator(repo=repo, runner=runner, settings=_settings())
    result = orch.process_candidate(candidate, run_id="run-1")

    assert result.status == "CONFIRMED"
    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded.status == "CONFIRMED"
    assert reloaded.risk is not None  # assessments faktiskt persisterade


def test_process_candidate_never_lets_agent_timeout_crash_the_loop(tmp_path):
    """AC6."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures(), timeout_agents={"crypto-risk-agent"})

    orch = Orchestrator(repo=repo, runner=runner, settings=_settings())
    result = orch.process_candidate(candidate, run_id="run-1")  # kastar aldrig

    assert result.status == "NO_TRADE"


def test_process_candidate_never_lets_agent_failure_crash_the_loop(tmp_path):
    """AC6, andra hälften: status='failed', inte bara timeout."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures(), fail_agents={"crypto-bear-adversarial"})

    orch = Orchestrator(repo=repo, runner=runner, settings=_settings())
    result = orch.process_candidate(candidate, run_id="run-1")

    assert result.status == "NO_TRADE"


def test_rejected_to_confirmed_transition_is_always_false():
    """AC5 - strukturell verifiering av redan befintlig Fas 0-garanti."""
    allowed, _reason = can_transition("REJECTED", "CONFIRMED")
    assert allowed is False


class _SpyRunner(MockAgentRunner):
    """Fångar det faktiska context-argumentet per rollanrop, utan att ändra
    MockAgentRunners befintliga beteende (Fas 5.5 Task 2 - bevisar att
    news_headlines/fear_greed_index faktiskt når fram, inte bara att inget
    kraschar)."""

    def __init__(self, fixtures, fail_agents=None, timeout_agents=None):
        super().__init__(fixtures, fail_agents, timeout_agents)
        self.captured_contexts: dict[str, dict] = {}

    def run(self, agent_def, context, output_schema):
        self.captured_contexts[agent_def.name] = context
        return super().run(agent_def, context, output_schema)


class _StubNewsConnector:
    def get_latest_items(self, limit: int) -> list[dict]:
        return [{"title": "UNIK_TESTRUBRIK", "link": "l", "pub_date": "p", "description": "d"}]


class _StubExternalDataConnector:
    def get_fear_greed_index(self) -> dict:
        return {"value": "50", "value_classification": "Neutral"}


class _RaisingNewsConnector:
    def get_latest_items(self, limit: int) -> list[dict]:
        raise ConnectorUnavailableError("news_rss otillgänglig")


class _RaisingExternalDataConnector:
    def get_fear_greed_index(self) -> dict:
        raise ConnectorUnavailableError("fear_greed otillgänglig")


def test_build_context_includes_news_and_fear_greed_only_for_news_sentiment_role(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    spy = _SpyRunner(_happy_fixtures())

    orch = Orchestrator(
        repo=repo,
        runner=spy,
        settings=_settings(),
        news_connector=_StubNewsConnector(),
        external_data_connector=_StubExternalDataConnector(),
    )
    orch.process_candidate(candidate, run_id="run-1")

    news_context = spy.captured_contexts["crypto-news-sentiment"]
    assert news_context["news_headlines"] == [
        {"title": "UNIK_TESTRUBRIK", "link": "l", "pub_date": "p", "description": "d"}
    ]
    assert news_context["fear_greed_index"] == {"value": "50", "value_classification": "Neutral"}
    assert "evidence_record" in news_context  # befintligt fält kvar oförändrat

    other_context = spy.captured_contexts["crypto-technical-analyst"]
    assert "news_headlines" not in other_context
    assert "fear_greed_index" not in other_context
    assert "evidence_record" in other_context


def test_build_context_omits_news_keys_when_connectors_are_none(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    spy = _SpyRunner(_happy_fixtures())

    orch = Orchestrator(repo=repo, runner=spy, settings=_settings())
    orch.process_candidate(candidate, run_id="run-1")

    news_context = spy.captured_contexts["crypto-news-sentiment"]
    assert "news_headlines" not in news_context
    assert "fear_greed_index" not in news_context
    assert "evidence_record" in news_context


def test_qa_role_is_routed_through_run_qa_gate_with_six_prior_assessments(tmp_path):
    """QA-gate-kontext-luckan (flaggad 2026-08-29): produktions-QA-rollen
    gick tidigare genom den generiska _build_context() (bara evidence_record)
    istället för den redan testade run_qa_gate() (de sex föregående
    rollernas fulla assessments) - omöjligt för QA att göra sitt faktiska
    jobb (intern konsistens, se .claude/agents/crypto-qa-gate.md). Detta
    bevisar att Orchestrator.process_candidate() nu routar "qa" genom
    run_qa_gate() istället."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    spy = _SpyRunner(_happy_fixtures())

    orch = Orchestrator(repo=repo, runner=spy, settings=_settings())
    orch.process_candidate(candidate, run_id="run-1")

    qa_context = spy.captured_contexts["crypto-qa-gate"]
    assert "evidence_record" not in qa_context  # bevisar run_qa_gate(), inte generiska pathen
    for role_key in (
        "news_sentiment",
        "technical",
        "bull_thesis",
        "forecast",
        "risk",
        "bear_adversarial",
    ):
        assert role_key in qa_context
        assert qa_context[role_key] is not None
    assert qa_context["candidate_id"] == candidate.candidate_id
    assert qa_context["instrument"] == candidate.instrument
    assert qa_context["run_id"] == "run-1"


def test_build_context_degrades_gracefully_when_news_connector_raises(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(_happy_fixtures())

    orch = Orchestrator(
        repo=repo,
        runner=runner,
        settings=_settings(),
        news_connector=_RaisingNewsConnector(),
        external_data_connector=_StubExternalDataConnector(),
    )
    result = orch.process_candidate(candidate, run_id="run-1")  # kastar aldrig

    assert result.news_sentiment.status == "ok"
    assert result.status == "CONFIRMED"


def test_build_context_degrades_gracefully_when_external_data_connector_raises(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(_happy_fixtures())

    orch = Orchestrator(
        repo=repo,
        runner=runner,
        settings=_settings(),
        news_connector=_StubNewsConnector(),
        external_data_connector=_RaisingExternalDataConnector(),
    )
    result = orch.process_candidate(candidate, run_id="run-1")  # kastar aldrig

    assert result.news_sentiment.status == "ok"
    assert result.status == "CONFIRMED"


def test_process_candidate_records_one_ai_call_event_per_role_invocation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    orch = Orchestrator(repo=repo, runner=runner, settings=_settings())
    orch.process_candidate(candidate, run_id="run-1")

    assert repo.count_ai_calls_since(_NOW) == 7


def test_process_candidate_records_ai_call_event_even_when_role_times_out(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures(), timeout_agents={"crypto-risk-agent"})

    orch = Orchestrator(repo=repo, runner=runner, settings=_settings())
    orch.process_candidate(candidate, run_id="run-1")

    # anropet kostade även om utfallet blev timeout - samtliga sju roller körs
    # fortfarande (bara Risk-rollen timeoutar, resten status="ok").
    assert repo.count_ai_calls_since(_NOW) == 7


def test_process_candidate_persists_forecast_record_on_successful_forecast_role(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(_happy_fixtures())

    orch = Orchestrator(repo=repo, runner=runner, settings=_settings())
    orch.process_candidate(candidate, run_id="run-1")

    record = repo.get_forecast_record(candidate.candidate_id)
    assert record is not None
    assert record.candidate_id == candidate.candidate_id
    assert record.instrument == "BTCUSDT"
    assert record.scenario_probabilities == {"bullish": 0.6, "neutral": 0.3, "bearish": 0.1}
    assert record.horizon == "4h"
    assert record.forecast_version == "v1"
    assert record.market_state_metadata == candidate.evidence_record.model_dump(mode="json")
    assert record.actual_outcome is None
    assert record.outcome_timestamp is None


def test_process_candidate_does_not_persist_forecast_record_when_forecast_role_fails(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(_happy_fixtures(), timeout_agents={"crypto-forecast-agent"})

    orch = Orchestrator(repo=repo, runner=runner, settings=_settings())
    orch.process_candidate(candidate, run_id="run-1")

    assert repo.get_forecast_record(candidate.candidate_id) is None


def test_process_candidate_stops_role_loop_at_ai_call_budget(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    orch = Orchestrator(
        repo=repo, runner=runner, settings=_settings(max_ai_calls_per_discovery_run=3)
    )
    result = orch.process_candidate(candidate, run_id="run-1")

    assert result.risk is None or result.bear_adversarial is None  # loopen bröts tidigt
    assert result.status == "NO_TRADE"  # ofullständig -> aldrig CONFIRMED
