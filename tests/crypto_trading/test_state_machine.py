import pytest

from crypto_trading.state_machine import can_transition

_ALL_STATUSES = [
    "CANDIDATE",
    "DATA_INVALID",
    "BUDGET_LIMITED",
    "UNDER_AI_ANALYSIS",
    "ANALYSIS_INTERRUPTED",
    "REJECTED",
    "NO_TRADE",
    "CONFIRMED",
]


@pytest.mark.parametrize(
    "current,target",
    [
        ("CANDIDATE", "DATA_INVALID"),
        ("CANDIDATE", "BUDGET_LIMITED"),
        ("CANDIDATE", "UNDER_AI_ANALYSIS"),
        ("UNDER_AI_ANALYSIS", "ANALYSIS_INTERRUPTED"),
        ("UNDER_AI_ANALYSIS", "REJECTED"),
        ("UNDER_AI_ANALYSIS", "NO_TRADE"),
        ("UNDER_AI_ANALYSIS", "CONFIRMED"),
        ("ANALYSIS_INTERRUPTED", "UNDER_AI_ANALYSIS"),
    ],
)
def test_allowed_transitions(current, target):
    allowed, reason = can_transition(current, target)
    assert allowed is True
    assert reason == "ok"


@pytest.mark.parametrize(
    "current,target",
    [
        ("REJECTED", "CONFIRMED"),
        ("NO_TRADE", "CONFIRMED"),
        ("DATA_INVALID", "CONFIRMED"),
        ("BUDGET_LIMITED", "CONFIRMED"),
        ("CONFIRMED", "UNDER_AI_ANALYSIS"),
        ("CANDIDATE", "CONFIRMED"),
        ("CANDIDATE", "NO_TRADE"),
    ],
)
def test_forbidden_transitions(current, target):
    allowed, reason = can_transition(current, target)
    assert allowed is False
    assert reason  # icke-tom förklaring


@pytest.mark.parametrize("status", _ALL_STATUSES)
def test_terminal_statuses_have_no_outgoing_transitions_except_analysis_interrupted(status):
    if status in ("CANDIDATE", "UNDER_AI_ANALYSIS", "ANALYSIS_INTERRUPTED"):
        return  # dessa har giltiga utgångar, testas ovan
    for target in _ALL_STATUSES:
        allowed, _ = can_transition(status, target)
        assert allowed is False


def test_can_transition_is_defensive_against_unknown_source_status():
    """Detta är EN oberoende, defensiv fail-closed-kontroll i can_transition
    själv - inte ett UNKNOWN_STATE-domänvärde och inte samma mekanism som
    CorruptCandidateStateError (Task 10), som är den faktiska vägen för en
    korrupt lagrad candidate-rad (status, evidence_record eller timestamp).
    can_transition ser aldrig ett sådant fall i praktiken - denna gren är
    bälte-och-hängslen för fallet att funktionen anropas direkt med en
    råsträng som inte kommer från ett giltigt Candidate-objekt."""
    allowed, reason = can_transition("TOTALLY_UNRECOGNIZED", "CONFIRMED")
    assert allowed is False
    assert "unknown source state" in reason
