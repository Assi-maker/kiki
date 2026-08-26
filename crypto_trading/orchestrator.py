from __future__ import annotations

from datetime import UTC, datetime

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.roles import ROLE_MAP
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from crypto_trading.logging import log_event
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.state_machine import can_transition
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
    def __init__(self, repo: Repository, runner: AgentRunner, settings: Settings):
        self._repo = repo
        self._runner = runner
        self._settings = settings

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
            context = self._build_context(candidate, run_id)
            assessment = self._runner.run(agent_def, context, spec.assessment_type)
            ai_calls += 1
            setattr(candidate, role, assessment)
            self._repo.save_assessment(candidate.candidate_id, role, assessment)
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
        self._repo.save_gate_decision(candidate.candidate_id, decision.outcome, decision.reasons, now)

        candidate.status = decision.outcome
        candidate.updated_at = now
        return candidate

    @staticmethod
    def _build_context(candidate: Candidate, run_id: str) -> dict:
        return {
            "candidate_id": candidate.candidate_id,
            "instrument": candidate.instrument,
            "evidence_record": candidate.evidence_record.model_dump(mode="json"),
            "run_id": run_id,
        }
