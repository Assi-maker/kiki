from decimal import Decimal

from crypto_trading.guardian.ai_context import build_ai_context, should_invoke_ai


def test_should_invoke_ai_false_when_new_state_is_hold():
    assert should_invoke_ai(previous_observation=None, new_state="HOLD") is False


def test_should_invoke_ai_true_on_first_non_hold_observation():
    assert should_invoke_ai(previous_observation=None, new_state="WATCH") is True


def test_should_invoke_ai_false_when_state_unchanged():
    previous = {"state": "WATCH"}
    assert should_invoke_ai(previous_observation=previous, new_state="WATCH") is False


def test_should_invoke_ai_true_on_transition_between_non_hold_states():
    previous = {"state": "WATCH"}
    assert should_invoke_ai(previous_observation=previous, new_state="PROTECT") is True


def test_should_invoke_ai_false_on_transition_back_to_hold():
    previous = {"state": "WATCH"}
    assert should_invoke_ai(previous_observation=previous, new_state="HOLD") is False


def test_build_ai_context_includes_factors_and_scores():
    context = build_ai_context(
        candidate=None,
        factors={"time_decay": Decimal("0.5")},
        decay_score=Decimal("0.6"),
        progress_ratio=Decimal("0.2"),
        unrealized_pnl=Decimal("15"),
        new_state="PROTECT",
    )
    assert context["decay_score"] == "0.6"
    assert context["new_state"] == "PROTECT"
    assert context["factors"] == {"time_decay": "0.5"}
