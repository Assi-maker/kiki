from __future__ import annotations

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
