"""P1 (2026-10-01): entry-path AI pipeline = Risk Agent + deterministic QA.

Covers config validation, the deterministic QA checks, the orchestrator
running only the configured roles (no QA model call, no AI_CALL_MADE for it),
the Gate's required roles, the CONFIRMED Telegram text without Bull/Forecast,
and that the production yaml carries the decision.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.config.loader import AiPipelineConfig, get_settings
from crypto_trading.gate.qa_checks import AGENT_NAME, check_candidate, run_deterministic_qa
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from crypto_trading.notify.telegram import format_confirmed_message
from crypto_trading.orchestrator import Orchestrator, active_roles, ai_calls_per_candidate
from crypto_trading.schemas.assessments import ForecastAssessment, QAAssessment
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.notify.test_telegram import _open_position
from tests.crypto_trading.test_orchestrator import (
    _NOW,
    _happy_fixtures,
    _persisted_candidate_in_under_ai_analysis,
    _settings,
)

P1 = AiPipelineConfig(entry_roles=["risk"], deterministic_qa=True,
                      opportunity_screening_shadow_enabled=False)


class _CountingRunner(MockAgentRunner):
    def __init__(self, fixtures):
        super().__init__(fixtures=fixtures)
        self.calls: list[str] = []

    def run(self, agent_def, context, output_schema):
        self.calls.append(agent_def.name)
        return super().run(agent_def, context, output_schema)


def _p1_settings():
    return _settings().model_copy(update={"ai_pipeline": P1})


def _candidate_with_risk(stop="53000", target="60000", **update):
    from crypto_trading.schemas.candidate import Candidate
    from tests.crypto_trading.test_orchestrator import _evidence

    risk = _happy_fixtures(stop, target)["crypto-risk-agent"]
    base = dict(
        candidate_id="c", idempotency_key="k", instrument="BTCUSDT", discovery_run_id="r",
        evidence_hash="h", status="UNDER_AI_ANALYSIS", evidence_record=_evidence(),
        created_at=_NOW, updated_at=_NOW, reference_price=Decimal("55000"), risk=risk,
    )
    base.update(update)
    return Candidate(**base)


# ------------------------------------------------------------------ config

def test_config_requires_risk_and_rejects_unknown_or_duplicate_roles():
    with pytest.raises(ValidationError):
        AiPipelineConfig(entry_roles=["forecast"])
    with pytest.raises(ValidationError):
        AiPipelineConfig(entry_roles=["risk", "astrology"])
    with pytest.raises(ValidationError):
        AiPipelineConfig(entry_roles=["risk", "risk"])


def test_code_default_is_the_legacy_chain_and_the_yaml_is_the_p1_decision():
    legacy = AiPipelineConfig()
    assert legacy.entry_roles == [
        "news_sentiment", "technical", "bull_thesis", "forecast", "risk", "bear_adversarial",
    ]
    assert legacy.deterministic_qa is False
    production = get_settings().ai_pipeline
    assert production.entry_roles == ["risk"]
    assert production.deterministic_qa is True
    assert production.opportunity_screening_shadow_enabled is False


def test_active_roles_keep_legacy_order_and_qa_last():
    settings = _settings().model_copy(update={"ai_pipeline": AiPipelineConfig(
        entry_roles=["bear_adversarial", "risk"], deterministic_qa=True)})
    assert active_roles(settings) == ("risk", "bear_adversarial", "qa")
    assert ai_calls_per_candidate(settings) == 2
    assert ai_calls_per_candidate(_p1_settings()) == 1
    assert ai_calls_per_candidate(_settings()) == 7  # legacy: 6 roles + QA model call


# ------------------------------------------------------------ deterministic QA

def test_deterministic_qa_passes_a_well_formed_candidate():
    qa = run_deterministic_qa(_candidate_with_risk(), ("risk",), "run", _NOW)
    assert isinstance(qa, QAAssessment)
    assert qa.agent_name == AGENT_NAME and qa.status == "ok"
    assert qa.passed is True and qa.violations == []


def test_deterministic_qa_flags_missing_or_failed_required_roles():
    candidate = _candidate_with_risk(risk=None)
    assert "MISSING_ASSESSMENT:risk" in check_candidate(candidate, ("risk",))
    failed = _candidate_with_risk()
    failed = failed.model_copy(update={"risk": failed.risk.model_copy(update={"status": "failed"})})
    assert "ASSESSMENT_NOT_OK:risk:failed" in check_candidate(failed, ("risk",))


def test_deterministic_qa_flags_evidence_mismatch_and_bad_data_quality():
    candidate = _candidate_with_risk(instrument="ETHUSDT")
    assert "EVIDENCE_INSTRUMENT_MISMATCH" in check_candidate(candidate, ("risk",))
    degraded = _candidate_with_risk()
    degraded = degraded.model_copy(update={"evidence_record": degraded.evidence_record.model_copy(
        update={"data_quality_status": "degraded"})})
    assert "EVIDENCE_DATA_QUALITY:degraded" in check_candidate(degraded, ("risk",))


@pytest.mark.parametrize("stop,target,code", [
    ("54999.99", "60000", "RISK_STOP_DISTANCE_IMPLAUSIBLE"),   # 0.00002 % stop
    ("1000", "60000", "RISK_STOP_DISTANCE_IMPLAUSIBLE"),       # 98 % stop
    ("53000", "200000", "RISK_TARGET_DISTANCE_IMPLAUSIBLE"),   # +264 % target
    ("-1", "60000", "RISK_STOP_NOT_POSITIVE"),
])
def test_deterministic_qa_catches_malformed_risk_levels(stop, target, code):
    assert code in check_candidate(_candidate_with_risk(stop, target), ("risk",))


def test_deterministic_qa_flags_empty_risk_text_fields():
    candidate = _candidate_with_risk()
    candidate = candidate.model_copy(update={"risk": candidate.risk.model_copy(
        update={"downside": "  "})})
    assert "RISK_FIELD_EMPTY:downside" in check_candidate(candidate, ("risk",))


def test_deterministic_qa_leaves_long_contradiction_to_the_gate():
    # stop above the reference is the Gate's CONFLICT (NO_TRADE), not a QA
    # REJECTED - QA must not duplicate it and change the audit meaning.
    assert check_candidate(_candidate_with_risk("56000", "60000"), ("risk",)) == []


def test_deterministic_qa_checks_legacy_forecast_invariants_when_present():
    forecast = ForecastAssessment(
        agent_name="f", run_id="r", created_at=_NOW, status="ok",
        scenario_probabilities={"bullish": 1.0}, horizon=" ", forecast_version="v",
    )
    candidate = _candidate_with_risk(forecast=forecast)
    assert "FORECAST_HORIZON_MISSING" in check_candidate(candidate, ("risk", "forecast"))


# ------------------------------------------------------------ orchestrator

def test_p1_orchestrator_calls_only_the_risk_agent_and_confirms(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = _CountingRunner(_happy_fixtures())
    result = Orchestrator(repo=repo, runner=runner, settings=_p1_settings()).process_candidate(
        candidate, "run-1", now=_NOW
    )
    assert runner.calls == ["crypto-risk-agent"]
    assert result.status == "CONFIRMED"
    assert result.qa.agent_name == AGENT_NAME
    assert result.news_sentiment is None and result.bull_thesis is None
    assert result.forecast is None and result.bear_adversarial is None
    # one billed call: the deterministic QA step is never counted as AI
    assert repo.count_ai_calls_since(datetime(2000, 1, 1, tzinfo=UTC)) == 1
    assert repo.get_assessment_payload("cand-1", "qa")["agent_name"] == AGENT_NAME


def test_p1_orchestrator_rejects_when_deterministic_qa_fails(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = _CountingRunner(_happy_fixtures(stop_loss="1000"))  # implausible 98 % stop
    result = Orchestrator(repo=repo, runner=runner, settings=_p1_settings()).process_candidate(
        candidate, "run-1", now=_NOW
    )
    assert result.status == "REJECTED"
    assert "RISK_STOP_DISTANCE_IMPLAUSIBLE" in result.qa.violations


def test_p1_failed_risk_answer_is_no_trade_never_confirmed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    fixtures = _happy_fixtures()
    fixtures["crypto-risk-agent"] = fixtures["crypto-risk-agent"].model_copy(
        update={"status": "failed"})
    result = Orchestrator(repo=repo, runner=_CountingRunner(fixtures),
                          settings=_p1_settings()).process_candidate(candidate, "run-1", now=_NOW)
    assert result.status == "NO_TRADE"


def test_legacy_chain_is_unchanged_with_code_defaults(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = _CountingRunner(_happy_fixtures())
    result = Orchestrator(repo=repo, runner=runner, settings=_settings()).process_candidate(
        candidate, "run-1", now=_NOW
    )
    assert runner.calls == [
        "crypto-news-sentiment", "crypto-technical-analyst", "crypto-bull-thesis",
        "crypto-forecast-agent", "crypto-risk-agent", "crypto-bear-adversarial", "crypto-qa-gate",
    ]
    assert result.status == "CONFIRMED"


# --------------------------------------------------------------------- gate

def test_gate_with_p1_roles_needs_only_risk_and_qa():
    candidate = _candidate_with_risk(
        qa=run_deterministic_qa(_candidate_with_risk(), ("risk",), "r", _NOW))
    decision = evaluate_risk_signal_gate(candidate, 0, 4, now=_NOW, required_roles=("risk", "qa"))
    assert decision.outcome == "CONFIRMED"
    # the legacy requirement would call the same candidate incomplete
    legacy = evaluate_risk_signal_gate(candidate, 0, 4, now=_NOW)
    assert legacy.outcome == "NO_TRADE"


def test_gate_still_enforces_long_geometry_and_capacity_with_p1_roles():
    qa = QAAssessment(agent_name=AGENT_NAME, run_id="r", created_at=_NOW, status="ok",
                      passed=True, violations=[])
    bad = _candidate_with_risk("56000", "60000", qa=qa)
    assert evaluate_risk_signal_gate(bad, 0, 4, now=_NOW,
                                     required_roles=("risk", "qa")).outcome == "NO_TRADE"
    full = _candidate_with_risk(qa=qa)
    assert evaluate_risk_signal_gate(full, 4, 4, now=_NOW,
                                     required_roles=("risk", "qa")).outcome == "NO_TRADE"


# ------------------------------------------------------------------ notify

def test_confirmed_message_without_bull_or_forecast():
    candidate = _candidate_with_risk(status="CONFIRMED")
    text = format_confirmed_message(candidate, _open_position())
    assert "CONFIRMED" in text and "Stop-loss" in text
    assert "AI team" not in text and "Forecast" not in text
