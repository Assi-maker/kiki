"""2026-09-29: a failed Forecast Agent call is persisted as the runner's
placeholder (status="failed", scenario_probabilities={}). It must read back
as a normal candidate - not CorruptCandidateStateError - and the Gate must
still fail closed on it (NO_TRADE, never CONFIRMED/REJECTED)."""
from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.agents.runner import RealClaudeRunner
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from crypto_trading.schemas.assessments import ForecastAssessment
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_discovery_wiring import _persisted_candidate_in_status


def _runner_placeholder() -> ForecastAssessment:
    runner = RealClaudeRunner.__new__(RealClaudeRunner)  # only the pure placeholder builder is used
    agent = AgentDefinition(
        name="crypto-forecast-agent", description="", tools=[], system_prompt="",
    )
    return runner._failed_assessment(agent, ForecastAssessment, "run-x")


def test_a_failed_forecast_placeholder_round_trips_through_the_repository(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    placeholder = _runner_placeholder()
    assert placeholder.scenario_probabilities == {}  # exactly what production stores
    repo.save_assessment("cand-1", "forecast", placeholder)

    candidate = repo.get_candidate("cand-1")  # raised CorruptCandidateStateError before the fix

    assert candidate.forecast.status == "failed"
    assert candidate.forecast.scenario_probabilities == {}
    assert repo._conn.execute(
        "SELECT count(*) FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED'"
    ).fetchone()[0] == 0


def test_the_gate_still_fails_closed_on_a_failed_forecast(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    repo.save_assessment("cand-1", "forecast", _runner_placeholder())
    decision = evaluate_risk_signal_gate(repo.get_candidate("cand-1"), 0, 4)
    assert decision.outcome == "NO_TRADE"
    assert "missing_or_failed_assessment:forecast" in decision.reasons
