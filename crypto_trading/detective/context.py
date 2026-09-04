from __future__ import annotations

from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.trade import Position

_ASSESSMENT_ROLES = (
    "news_sentiment",
    "technical",
    "bull_thesis",
    "forecast",
    "risk",
    "bear_adversarial",
    "qa",
)


def build_position_analysis_context(
    position: Position, candidate: Candidate | None, gate_decision: dict | None
) -> dict:
    """Ren funktion: bygger EN stängd positions fulla analysunderlag åt
    Detective, uteslutande genom att LÄSA redan persisterad Position/
    Candidate/gate_decision-data - duplicerar aldrig lagring (explicit
    användarkrav: "Återanvänd befintlig trade/evidence-data där det är
    möjligt"). `candidate`/`gate_decision` kan saknas (None, t.ex. mycket
    gamla rader eller en korrupt candidate-rad - se detective/batch.py) -
    utelämnas då tyst, samma icke-gissningsprincip som orchestrator.py::
    _build_context() redan använder för valfria källor."""
    hold_hours = None
    if position.closed_at is not None:
        hold_hours = round(
            (position.closed_at - position.opened_at).total_seconds() / 3600, 2
        )

    context: dict = {
        "position_id": position.position_id,
        "instrument": position.instrument,
        "direction": position.direction,
        "entry": str(position.simulated_fill_entry),
        "exit": (
            str(position.simulated_fill_exit)
            if position.simulated_fill_exit is not None
            else None
        ),
        "stop_loss": str(position.stop_loss),
        "target": str(position.target),
        "size": str(position.size),
        "realized_pnl_usdt": (
            str(compute_pnl(position)) if position.status == "CLOSED" else None
        ),
        "exit_reason": position.exit_reason,
        "hold_hours": hold_hours,
        "fees": str(position.fees) if position.fees is not None else None,
        "funding": str(position.funding) if position.funding is not None else None,
    }
    if candidate is not None:
        context["evidence_record"] = candidate.evidence_record.model_dump(mode="json")
        context["trigger_reasons"] = candidate.evidence_record.trigger_reasons
        for role in _ASSESSMENT_ROLES:
            assessment = getattr(candidate, role)
            if assessment is not None:
                context[f"{role}_assessment"] = assessment.model_dump(mode="json")
    if gate_decision is not None:
        context["gate_decision"] = gate_decision
    return context


def signal_type_for_candidate(candidate: Candidate | None) -> str:
    """Kommaseparerad, sorterad signaltyp (candidate.evidence_record.
    trigger_reasons) - deterministisk grupperingsnyckel för detective/
    stats.py::compute_breakdown_by_signal_type(). "unknown" när candidate
    saknas eller inte triggades av någon specifik anledning - gissar
    aldrig en signaltyp."""
    if candidate is None or not candidate.evidence_record.trigger_reasons:
        return "unknown"
    return ",".join(sorted(candidate.evidence_record.trigger_reasons))
