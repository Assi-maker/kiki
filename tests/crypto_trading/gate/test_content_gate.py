"""P1 Gate content requirements (2026-09-28).

Before: CONFIRMED meant "all 7 AI roles answered ok, QA accepted the format
and there was capacity". Now CONFIRMED also requires, ENFORCED:
- a reference price and a risk plan that is consistent with a LONG
  (stop < reference < target), and a QA verdict without listed violations
  (structural conflict conditions);
- reward:risk >= 1;
- a fresh signal (age at the gate <= the primary evidence timeframe).
Forecast-based conditions (bullish probability/dominance, bearish
dominance, confidence, uncertainty) are evaluated and logged in SHADOW only:
on 403 analysed candidates the forecast had no predictive power (user
decision 2026-09-28, docs/superpowers/reports/2026-09-28-safety-and-gate-implementation.md)."""
from datetime import timedelta
from decimal import Decimal

from crypto_trading.config.loader import GatePolicyConfig, get_settings
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from tests.crypto_trading.gate.test_risk_signal_gate import (
    _NOW,
    _forecast,
    _full_candidate,
    _qa,
    _risk,
)

POLICY = GatePolicyConfig()


def _gate(candidate, now=_NOW + timedelta(minutes=10), policy=POLICY):
    return evaluate_risk_signal_gate(candidate, 0, 5, policy=policy, now=now)


def _risk_levels(sl, tp):
    return _risk().model_copy(update={"suggested_stop_loss": sl, "suggested_target": tp})


def test_the_realistic_happy_path_is_confirmed_with_metrics():
    decision = _gate(_full_candidate())
    assert decision.outcome == "CONFIRMED"
    assert decision.enforced_failed == []
    assert decision.metrics["risk_reward"] == "2"
    assert decision.metrics["signal_age_minutes"] == 10.0
    assert decision.metrics["candidate_score"] == 0.8


def test_reward_below_risk_is_not_confirmed():
    decision = _gate(_full_candidate(risk=_risk_levels("95", "104")))  # RR 0.8
    assert decision.outcome == "NO_TRADE"
    assert decision.enforced_failed == ["RR_BELOW_MINIMUM"]
    assert "content_gate:RR_BELOW_MINIMUM" in decision.reasons


def test_reward_equal_to_risk_passes():
    assert _gate(_full_candidate(risk=_risk_levels("95", "105"))).outcome == "CONFIRMED"


def test_a_risk_plan_that_contradicts_a_long_is_a_conflict():
    for sl, tp in (("101", "110"), ("95", "99"), ("100", "110")):
        decision = _gate(_full_candidate(risk=_risk_levels(sl, tp)))
        assert decision.outcome == "NO_TRADE"
        assert "CONFLICT_RISK_LEVELS_CONTRADICT_LONG" in decision.enforced_failed


def test_unparseable_risk_levels_fail_closed():
    decision = _gate(_full_candidate(risk=_risk_levels("ca 3 % under", "110")))
    assert decision.outcome == "NO_TRADE"
    assert "RISK_LEVELS_UNPARSEABLE" in decision.enforced_failed


def test_missing_reference_price_fails_closed():
    candidate = _full_candidate().model_copy(update={"reference_price": None})
    decision = _gate(candidate)
    assert decision.outcome == "NO_TRADE"
    assert "REFERENCE_PRICE_MISSING" in decision.enforced_failed


def test_qa_that_passes_while_listing_violations_is_a_conflict():
    qa = _qa().model_copy(update={"violations": ["forecast contradicts thesis"]})
    decision = _gate(_full_candidate(qa=qa))
    assert decision.outcome == "NO_TRADE"
    assert "CONFLICT_QA_PASSED_WITH_VIOLATIONS" in decision.enforced_failed


def test_a_stale_signal_is_not_confirmed():
    decision = _gate(_full_candidate(), now=_NOW + timedelta(minutes=31))
    assert decision.outcome == "NO_TRADE"
    assert decision.enforced_failed == ["SIGNAL_STALE"]


def test_signal_exactly_at_the_limit_is_fresh():
    assert _gate(_full_candidate(), now=_NOW + timedelta(minutes=30)).outcome == "CONFIRMED"


def test_the_freshness_limit_is_the_primary_evidence_timeframe():
    """Not an arbitrary number: evidence is computed on the last closed candle
    of the primary timeframe; after one more candle it describes the past."""
    settings = get_settings()
    primary = settings.pipeline.screener_timeframes[0]
    assert primary.endswith("m")
    assert settings.gate.max_signal_age_minutes == int(primary[:-1])


def test_bearish_forecast_does_not_block_live_it_is_shadow_only():
    """User decision 2026-09-28: forecast has no demonstrated edge - its
    conditions are computed and logged, never enforced."""
    bearish = _forecast().model_copy(update={"scenario_probabilities": {
        "bullish": 0.2, "neutral": 0.35, "bearish": 0.45}})
    decision = _gate(_full_candidate(forecast=bearish))
    assert decision.outcome == "CONFIRMED"
    assert decision.shadow["would_block"] is True
    assert set(decision.shadow["failed"]) == {
        "SHADOW_BULLISH_NOT_DOMINANT", "SHADOW_BEARISH_DOMINANT", "SHADOW_BULLISH_NOT_MAJORITY",
    }


def test_free_form_scenario_names_are_parsed():
    odd = _forecast().model_copy(update={"scenario_probabilities": {
        "bullish_continuation": 0.2, "neutral_consolidation": 0.35, "bearish_reversal": 0.45}})
    decision = _gate(_full_candidate(forecast=odd))
    assert decision.metrics["bull_probability"] == 0.2
    assert decision.metrics["bear_probability"] == 0.45
    assert decision.metrics["neutral_probability"] == 0.35


def test_uncertainty_and_confidence_are_recorded():
    decision = _gate(_full_candidate())
    assert decision.metrics["forecast_confidence"] == 0.6
    assert 0 < decision.metrics["forecast_uncertainty"] < 1


def test_a_bullish_majority_forecast_passes_the_shadow_too():
    decision = _gate(_full_candidate())  # 0.6 / 0.3 / 0.1
    assert decision.shadow["failed"] == []
    assert decision.shadow["would_block"] is False


def test_enforced_failures_are_all_reported_not_just_the_first():
    decision = _gate(_full_candidate(risk=_risk_levels("95", "104")), now=_NOW + timedelta(hours=2))
    assert set(decision.enforced_failed) == {"RR_BELOW_MINIMUM", "SIGNAL_STALE"}


def test_existing_hard_rules_still_come_first():
    decision = evaluate_risk_signal_gate(_full_candidate(qa=_qa(passed=False)), 0, 5,
                                         policy=POLICY, now=_NOW)
    assert decision.outcome == "REJECTED"
    decision = evaluate_risk_signal_gate(_full_candidate(), 5, 5, policy=POLICY, now=_NOW)
    assert decision.outcome == "NO_TRADE"
    assert any("max_concurrent_positions" in r for r in decision.reasons)


def test_min_risk_reward_is_configured_at_one():
    assert get_settings().gate.min_risk_reward == Decimal("1")


def test_only_the_gate_can_produce_confirmed():
    """Structural: no module other than the Gate constructs a CONFIRMED
    outcome, so AI form alone can never create CONFIRMED."""
    import pathlib
    import re

    import crypto_trading
    root = pathlib.Path(crypto_trading.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'outcome\s*=\s*"CONFIRMED"', text) and path.name != "risk_signal_gate.py":
            offenders.append(str(path))
    assert offenders == []
