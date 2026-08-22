from __future__ import annotations

from intelligence.schemas.opportunity import Opportunity, OpportunityStatus

REQUIRED_FOR_REPORTED: frozenset[str] = frozenset(
    {"research", "opportunity", "market", "forecast", "risk", "bear", "qa"}
)

_TERMINAL_FROM_REJECTED: frozenset[OpportunityStatus] = frozenset({"approved", "reported"})


def can_transition(opportunity: Opportunity, target: OpportunityStatus) -> tuple[bool, str]:
    if opportunity.status == "rejected" and target in _TERMINAL_FROM_REJECTED:
        return False, "opportunity är rejected och kan inte transitionera till approved/reported"

    if target in ("approved", "reported"):
        for field in REQUIRED_FOR_REPORTED:
            assessment = getattr(opportunity, field)
            if assessment is None:
                return False, f"saknar obligatorisk assessment: {field}"
            if assessment.status != "ok":
                return False, f"assessment {field} har status={assessment.status}, kräver 'ok'"
        qa = opportunity.qa
        if qa is not None and qa.passed is not True:
            return False, "qa.passed är inte True"

    return True, "ok"
