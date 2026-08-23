# intelligence/orchestrator.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from intelligence.agents.loader import load_agent_definition
from intelligence.agents.roles import ROLE_MAP
from intelligence.agents.runner import AgentRunner
from intelligence.config import Settings
from intelligence.logging import log_event
from intelligence.reporting.report import write_report
from intelligence.schemas.event import Event
from intelligence.schemas.opportunity import Opportunity
from intelligence.scoring.model import score_opportunity
from intelligence.state_machine import can_transition
from intelligence.storage.repository import Repository

_ROLE_ORDER = ["research", "opportunity", "market", "forecast", "risk", "bear", "qa"]


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        runner: AgentRunner,
        weights: dict[str, float],
        settings: Settings,
        report_dest_dir: Path,
    ):
        self._repo = repo
        self._runner = runner
        self._weights = weights
        self._settings = settings
        self._report_dest_dir = report_dest_dir

    def process_event(self, event: Event, run_id: str) -> Opportunity:
        opportunity = Opportunity(
            opportunity_id=str(uuid.uuid4()),
            event_id=event.event_id,
            created_at=datetime.now(UTC),
            category=event.category,
            title=f"Avvikelse i {event.metric} ({event.source_id})",
            summary=event.description,
            time_horizon="okänt — bedöms av Forecasting Agent",
            liquidity="okänd — bedöms av Risk Agent",
        )
        self._repo.save_opportunity(opportunity)

        agent_calls = 0
        for role in _ROLE_ORDER:
            if agent_calls >= self._settings.max_agent_calls_per_run:
                log_event(run_id, event="max_agent_calls_reached", role=role)
                break
            spec = ROLE_MAP[role]
            agent_def = load_agent_definition(spec.agent_file)
            context = {
                "event": event.model_dump(mode="json"),
                "opportunity": opportunity.model_dump(mode="json"),
                "run_id": run_id,
            }
            assessment = self._runner.run(agent_def, context, spec.assessment_type)
            agent_calls += 1
            setattr(opportunity, role, assessment)
            self._repo.save_assessment(opportunity.opportunity_id, role, assessment)
            log_event(
                run_id,
                event="assessment_completed",
                agent_name=agent_def.name,
                role=role,
                status=assessment.status,
            )

        ok, reason = can_transition(opportunity, "reported")
        if ok:
            total, breakdown = score_opportunity(opportunity, self._weights)
            opportunity.score = total
            opportunity.score_breakdown = breakdown
            opportunity.status = "reported"
            self._repo.save_opportunity(opportunity)
            self._repo.update_opportunity_status(opportunity.opportunity_id, "reported")
            write_report(opportunity, self._report_dest_dir)
            log_event(
                run_id,
                event="opportunity_reported",
                opportunity_id=opportunity.opportunity_id,
                score=total,
            )
        else:
            qa = opportunity.qa
            target_status = (
                "rejected"
                if qa is not None and qa.status == "ok" and qa.passed is False
                else "under_review"
            )
            opportunity.status = target_status
            self._repo.update_opportunity_status(opportunity.opportunity_id, target_status)
            log_event(
                run_id,
                event="opportunity_blocked",
                opportunity_id=opportunity.opportunity_id,
                reason=reason,
                status=target_status,
            )

        return opportunity
