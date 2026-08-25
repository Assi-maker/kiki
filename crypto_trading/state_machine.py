from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from crypto_trading.schemas.event import Event

if TYPE_CHECKING:
    from crypto_trading.storage.repository import Repository

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "CANDIDATE": frozenset({"DATA_INVALID", "BUDGET_LIMITED", "UNDER_AI_ANALYSIS"}),
    "UNDER_AI_ANALYSIS": frozenset({"ANALYSIS_INTERRUPTED", "REJECTED", "NO_TRADE", "CONFIRMED"}),
    "ANALYSIS_INTERRUPTED": frozenset({"UNDER_AI_ANALYSIS"}),
    "DATA_INVALID": frozenset(),
    "BUDGET_LIMITED": frozenset(),
    "REJECTED": frozenset(),
    "NO_TRADE": frozenset(),
    "CONFIRMED": frozenset(),
}


def can_transition(current_status: str, target_status: str) -> tuple[bool, str]:
    """Ren, deterministisk gate-funktion: (bool, reason), aldrig en exception.

    OBS: `current_status` typas medvetet som `str`, inte `CandidateStatus` -
    detta är ett andra, oberoende skyddslager (bälte-och-hängslen), inte en
    väg för att hantera korrupt lagrad data. Om `current_status` inte finns i
    `ALLOWED_TRANSITIONS` nekas övergången fail-closed med en förklaring -
    det skapar ALDRIG något "UNKNOWN_STATE"-domänvärde eller något annat
    domänobjekt. En faktiskt korrupt lagrad candidate-rad (oavsett om felet
    sitter i `status`, `evidence_record` eller en timestamp) hanteras
    uteslutande av `storage.repository.SQLiteRepository.get_candidate()` via
    `CorruptCandidateStateError` + ett `CORRUPT_STATE_DETECTED`-event, INNAN
    ett `Candidate`-objekt någonsin skulle kunna nå denna funktion (se Task
    10). Denna funktion ser alltså i praktiken bara redan validerade
    `CandidateStatus`-värden - grenen nedan är ett defensivt nej, inte en
    förväntad körväg."""
    allowed_targets = ALLOWED_TRANSITIONS.get(current_status)
    if allowed_targets is None:
        return False, f"unknown source state: {current_status!r}"
    if target_status not in allowed_targets:
        return False, f"transition {current_status} -> {target_status} is not allowed"
    return True, "ok"


def sweep_interrupted_analyses(
    repo: "Repository", swept_at: datetime, run_id: str
) -> list[str]:
    """Vid start av discovery-processen: varje candidate som redan ligger i
    UNDER_AI_ANALYSIS är per definition föräldralös (denna process skrev den
    inte - den startar precis nu). Sveper dem till ANALYSIS_INTERRUPTED,
    enkelriktat - återupplivar ALDRIG automatiskt (SPEC §8.5, Phase 0-design)."""
    interrupted_ids: list[str] = []
    for candidate in repo.find_candidates_by_status("UNDER_AI_ANALYSIS"):
        allowed, reason = can_transition(candidate.status, "ANALYSIS_INTERRUPTED")
        if not allowed:
            raise AssertionError(f"sweep produced an illegal transition: {reason}")
        event = Event(
            event_id=f"ANALYSIS_INTERRUPTED_DETECTED:{candidate.candidate_id}:{run_id}",
            event_type="ANALYSIS_INTERRUPTED_DETECTED",
            aggregate_type="candidate",
            aggregate_id=candidate.candidate_id,
            occurred_at=swept_at,
            run_id=run_id,
            schema_version=1,
            payload={"previous_status": candidate.status},
        )
        repo.transition_candidate_with_event(
            candidate.candidate_id, "ANALYSIS_INTERRUPTED", swept_at, event
        )
        interrupted_ids.append(candidate.candidate_id)
    return interrupted_ids
