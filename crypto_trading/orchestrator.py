from __future__ import annotations

from datetime import UTC, datetime

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.roles import ROLE_MAP
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.gate.qa_gate import run_qa_gate
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from crypto_trading.logging import log_event
from crypto_trading.schemas.assessments import ForecastAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.state_machine import can_transition, sweep_interrupted_analyses
from crypto_trading.storage.repository import Repository

_ROLE_ORDER = (
    "news_sentiment",
    "technical",
    "bull_thesis",
    "forecast",
    "risk",
    "bear_adversarial",
    "qa",
)


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        runner: AgentRunner,
        settings: Settings,
        news_connector: object | None = None,
        external_data_connector: object | None = None,
    ):
        self._repo = repo
        self._runner = runner
        self._settings = settings
        self._news_connector = news_connector
        self._external_data_connector = external_data_connector

    def process_candidate(self, candidate: Candidate, run_id: str) -> Candidate:
        ai_calls = 0
        for role in _ROLE_ORDER:
            if ai_calls >= self._settings.budget_limits.max_ai_calls_per_discovery_run:
                log_event(
                    run_id,
                    event="max_ai_calls_reached",
                    role=role,
                    candidate_id=candidate.candidate_id,
                )
                break
            spec = ROLE_MAP[role]
            agent_def = load_agent_definition(spec.agent_file)
            if role == "qa":
                # QA:s jobb (SPEC §4/§6, .claude/agents/crypto-qa-gate.md) är
                # intern konsistens MELLAN de sex föregående rollernas
                # bedömningar - strukturellt omöjligt via den generiska
                # _build_context() (bara evidence_record). run_qa_gate() är
                # redan testad (tests/crypto_trading/gate/test_qa_gate.py) -
                # återanvänds här istället för att duplicera dess
                # kontext-uppbyggnad.
                assessment = run_qa_gate(candidate, self._runner, run_id)
            else:
                context = self._build_context(candidate, role, run_id)
                assessment = self._runner.run(agent_def, context, spec.assessment_type)
            ai_calls += 1
            self._repo.record_ai_call_event(
                Event(
                    event_id=f"AI_CALL_MADE:{candidate.candidate_id}:{role}:{run_id}",
                    event_type="AI_CALL_MADE",
                    aggregate_type="candidate",
                    aggregate_id=candidate.candidate_id,
                    occurred_at=datetime.now(UTC),
                    run_id=run_id,
                    schema_version=1,
                    payload={"role": role, "status": assessment.status},
                )
            )
            setattr(candidate, role, assessment)
            self._repo.save_assessment(candidate.candidate_id, role, assessment)
            if role == "forecast" and assessment.status == "ok":
                self._repo.save_forecast_record(
                    _build_forecast_record(candidate, assessment, datetime.now(UTC))
                )
            log_event(
                run_id,
                event="assessment_completed",
                agent_name=agent_def.name,
                role=role,
                status=assessment.status,
                candidate_id=candidate.candidate_id,
            )

        open_positions = self._repo.count_open_positions()
        decision = evaluate_risk_signal_gate(
            candidate, open_positions, self._settings.risk_limits.max_concurrent_positions
        )

        now = datetime.now(UTC)
        allowed, reason = can_transition(candidate.status, decision.outcome)
        if not allowed:
            raise AssertionError(f"illegal transition attempted: {reason}")

        event = Event(
            event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:{decision.outcome}",
            event_type="CANDIDATE_TRANSITIONED",
            aggregate_type="candidate",
            aggregate_id=candidate.candidate_id,
            occurred_at=now,
            run_id=run_id,
            schema_version=1,
            payload={"from": candidate.status, "to": decision.outcome, "reasons": decision.reasons},
        )
        self._repo.transition_candidate_with_event(
            candidate.candidate_id, decision.outcome, now, event
        )
        self._repo.save_gate_decision(
            candidate.candidate_id, decision.outcome, decision.reasons, now
        )

        candidate.status = decision.outcome
        candidate.updated_at = now
        return candidate

    def _build_context(self, candidate: Candidate, role: str, run_id: str) -> dict:
        """Fas 5.5 Task 2: nyhets-/Fear&Greed-underlag läggs bara till för
        news_sentiment-rollen (SPEC §6 - roller delar read-only kontext, men
        delar inte nödvändigtvis IDENTISKT innehåll; de andra sex rollerna
        har ingen användning för rådata de inte tolkar). Icke-kritisk källa
        (SPEC §8.2): om connectorn saknas (None) eller kastar
        ConnectorUnavailableError utelämnas nyckeln helt - aldrig en tom
        gissning - matchande crypto-news-sentiment.md:s egna instruktion att
        skriva ut explicit att data saknas snarare än att hitta på."""
        context = {
            "candidate_id": candidate.candidate_id,
            "instrument": candidate.instrument,
            "evidence_record": candidate.evidence_record.model_dump(mode="json"),
            "run_id": run_id,
        }
        if role == "news_sentiment":
            if self._news_connector is not None:
                try:
                    context["news_headlines"] = self._news_connector.get_latest_items(limit=10)
                except ConnectorUnavailableError:
                    pass
            if self._external_data_connector is not None:
                try:
                    context["fear_greed_index"] = (
                        self._external_data_connector.get_fear_greed_index()
                    )
                except ConnectorUnavailableError:
                    pass
        # Root-cause-fix (2026-09-02): utan ett faktiskt referenspris kan
        # Risk Agent aldrig svara med ett absolut, Decimal-parsbart
        # suggested_stop_loss/suggested_target (paper_trading/
        # position_opening.py) - bara en kvalitativ beskrivning, som alltid
        # misslyckade parsningen (0/10 CONFIRMED öppnade någonsin en
        # position). Skopat strikt till risk-rollen - de andra sex
        # rollernas kontext/prompt/beteende är helt oförändrat. `None`
        # (t.ex. äldre candidates persisterade före denna fix) utelämnar
        # nyckeln helt, samma icke-gissnings-princip som news/fear_greed
        # ovan.
        if role == "risk" and candidate.reference_price is not None:
            context["reference_price"] = str(candidate.reference_price)
        return context


def run_discovery_cycle(
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    run_id: str,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
) -> list[Candidate]:
    """Discovery-loop-wiring: (1) sveper föräldralösa UNDER_AI_ANALYSIS-
    candidates till ANALYSIS_INTERRUPTED (Fas 0:s sweep_interrupted_analyses,
    SPEC §8.5), (2) hämtar alla CANDIDATE- och ANALYSIS_INTERRUPTED-status-
    candidates (Fas 2:s candidate_engine lämnar budget-godkända candidates i
    CANDIDATE; en tidigare krasch lämnar en candidate i ANALYSIS_INTERRUPTED -
    Fas 5:s återupptagningspolicy, se PLAN_CRYPTO_PHASE5.md Beslut 2: full
    återkörning av hela 7-rollskedjan, ingen delvis återupptagning), sorterade
    äldst-först för determinism, (3) transitionerar var och en till
    UNDER_AI_ANALYSIS, (4) kör Orchestrator.process_candidate på var och en -
    om inte det persisterade dagliga AI-anropstaket (SPEC §10, Beslut 3) skulle
    överskridas AV DENNA CANDIDATE (projected-cost-kontroll, se
    PLAN_CRYPTO_PHASE5.md Conflict A-beslutet 2026-08-27: daily_count_so_far +
    planned_calls_for_candidate > max_ai_calls_per_day, där
    planned_calls_for_candidate = min(len(_ROLE_ORDER), max_ai_calls_per_discovery_run)
    - exakt, inte en gissning, eftersom process_candidate() alltid kör exakt
    så många anrop innan den stannar, oavsett assessment-utfall). En
    CANDIDATE-statuscandidate som blockeras skickas till BUDGET_LIMITED
    (aldrig REJECTED/NO_TRADE - §8.3) och en ANALYSIS_INTERRUPTED-candidate
    lämnas orörd för nästa cykel (aldrig BUDGET_LIMITED - den fick redan en
    delvis analys innan kraschen, se Beslut 2). En candidate som väl påbörjar
    sin rollkedja avbryts aldrig i förtid av det dagliga taket (Beslut 3)."""
    sweep_interrupted_analyses(repo, swept_at=datetime.now(UTC), run_id=run_id)

    orchestrator = Orchestrator(
        repo=repo,
        runner=runner,
        settings=settings,
        news_connector=news_connector,
        external_data_connector=external_data_connector,
    )
    daily_cap = settings.budget_limits.max_ai_calls_per_day
    day_start = _utc_day_start(datetime.now(UTC))
    planned_calls_for_candidate = min(
        len(_ROLE_ORDER), settings.budget_limits.max_ai_calls_per_discovery_run
    )

    to_analyze = sorted(
        [
            *repo.find_candidates_by_status("CANDIDATE"),
            *repo.find_candidates_by_status("ANALYSIS_INTERRUPTED"),
        ],
        key=lambda c: c.created_at,
    )

    daily_count_at_start = repo.count_ai_calls_since(day_start)
    if daily_count_at_start >= float(settings.budget_limits.warning_threshold_pct) * daily_cap:
        log_event(
            run_id,
            event="daily_ai_call_budget_warning",
            count=daily_count_at_start,
            cap=daily_cap,
        )

    results: list[Candidate] = []
    for candidate in to_analyze:
        if repo.count_ai_calls_since(day_start) + planned_calls_for_candidate > daily_cap:
            if candidate.status == "CANDIDATE":
                results.append(_send_to_budget_limited(repo, candidate, run_id))
            else:
                log_event(
                    run_id,
                    event="daily_ai_call_budget_reached_interrupted_deferred",
                    candidate_id=candidate.candidate_id,
                )
            continue

        allowed, reason = can_transition(candidate.status, "UNDER_AI_ANALYSIS")
        if not allowed:
            raise AssertionError(f"illegal transition attempted: {reason}")
        now = datetime.now(UTC)
        event = Event(
            event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:UNDER_AI_ANALYSIS",
            event_type="CANDIDATE_TRANSITIONED",
            aggregate_type="candidate",
            aggregate_id=candidate.candidate_id,
            occurred_at=now,
            run_id=run_id,
            schema_version=1,
            payload={"from": candidate.status, "to": "UNDER_AI_ANALYSIS"},
        )
        repo.transition_candidate_with_event(
            candidate.candidate_id, "UNDER_AI_ANALYSIS", now, event
        )
        candidate.status = "UNDER_AI_ANALYSIS"
        results.append(orchestrator.process_candidate(candidate, run_id))
    return results


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _build_forecast_record(
    candidate: Candidate, assessment: ForecastAssessment, now: datetime
) -> ForecastRecord:
    """Fas 5.5 Task 7 (SPEC §9): persisterar den redan producerade
    ForecastAssessment:en som en fristående ForecastRecord, så Fas 8 har
    verklig historik att kalibrera mot. `forecast_id = candidate_id` (samma
    idempotensprincip som `position_id = candidate_id`, Fas 4) - en
    återupptagen candidates andra forecast skriver över, dubblerar aldrig
    (se Repository.save_forecast_record). `actual_outcome`/
    `outcome_timestamp` lämnas medvetet `None` - Fas 8:s jobb, inte denna
    fas."""
    return ForecastRecord(
        forecast_id=candidate.candidate_id,
        candidate_id=candidate.candidate_id,
        instrument=candidate.instrument,
        forecast_timestamp=now,
        horizon=assessment.horizon,
        scenario_probabilities=assessment.scenario_probabilities,
        forecast_version=assessment.forecast_version,
        market_state_metadata=candidate.evidence_record.model_dump(mode="json"),
    )


def _send_to_budget_limited(repo: Repository, candidate: Candidate, run_id: str) -> Candidate:
    now = datetime.now(UTC)
    allowed, reason = can_transition(candidate.status, "BUDGET_LIMITED")
    if not allowed:
        raise AssertionError(f"illegal transition attempted: {reason}")
    event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:BUDGET_LIMITED",
        event_type="CANDIDATE_TRANSITIONED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=now,
        run_id=run_id,
        schema_version=1,
        payload={"from": candidate.status, "to": "BUDGET_LIMITED", "reason": "daily_ai_call_cap"},
    )
    repo.transition_candidate_with_event(candidate.candidate_id, "BUDGET_LIMITED", now, event)
    return candidate.model_copy(update={"status": "BUDGET_LIMITED", "updated_at": now})
