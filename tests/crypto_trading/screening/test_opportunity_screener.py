from __future__ import annotations

from datetime import UTC, datetime

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.schemas.assessments import OpportunityScreenAssessment
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.screening.candidate_engine import (
    apply_opportunity_screening,
    process_evidence,
)
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _evidence(instrument="BTCUSDT", candidate_score=0.5) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=False, metric="m", value=0.0, baseline=0.0, threshold=1.0)
    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=candidate_score,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _candidate(repo, instrument, score, run_id="run-1"):
    return process_evidence(
        repo, _evidence(instrument, score), discovery_run_id=run_id, created_at=_NOW
    )


def _agent_def() -> AgentDefinition:
    return AgentDefinition(
        name="crypto-opportunity-screener", description="d", tools=["Read"], system_prompt="p"
    )


class _ScriptedScreenerRunner(AgentRunner):
    """Testdubblett som returnerar en per-candidate-konfigurerad
    OpportunityScreenAssessment (till skillnad från MockAgentRunner, som
    bara stödjer en enda fixture per agentnamn - otillräckligt här
    eftersom varje kandidat i en förscreening måste kunna få en egen
    poäng). Spårar vilka candidate_id den faktiskt anropades för, så att
    tester kan bevisa att kandidater bortom förscreeningspoolen ALDRIG
    kostar ett AI-anrop."""

    def __init__(self, scores_by_instrument: dict[str, float | None]):
        self._scores_by_instrument = scores_by_instrument
        self.called_for_instruments: list[str] = []

    def run(self, agent_def, context, output_schema):
        instrument = context["instrument"]
        self.called_for_instruments.append(instrument)
        score = self._scores_by_instrument.get(instrument)
        status = "failed" if score is None else "ok"
        return output_schema(
            agent_name=agent_def.name,
            run_id=context["run_id"],
            created_at=_NOW,
            status=status,
            opportunity_score=score or 0.0,
            reasoning="" if score is None else f"score {score}",
        )


def test_shadow_mode_screens_but_never_changes_who_proceeds(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate(repo, "AAAUSDT", score=0.5)
    b = _candidate(repo, "BBBUSDT", score=0.4)
    runner = _ScriptedScreenerRunner({"AAAUSDT": 9.0, "BBBUSDT": 1.0})

    result = apply_opportunity_screening(
        repo,
        [a, b],
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=5,
        max_candidates_for_full_analysis=1,
        enforce=False,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert {c.candidate_id for c in result} == {a.candidate_id, b.candidate_id}
    assert repo.get_candidate(a.candidate_id).status == "CANDIDATE"
    assert repo.get_candidate(b.candidate_id).status == "CANDIDATE"


def test_shadow_mode_still_calls_screener_for_data_collection(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate(repo, "AAAUSDT", score=0.5)
    runner = _ScriptedScreenerRunner({"AAAUSDT": 5.0})

    apply_opportunity_screening(
        repo,
        [a],
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=5,
        max_candidates_for_full_analysis=1,
        enforce=False,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert runner.called_for_instruments == ["AAAUSDT"]


def test_enforce_mode_selects_only_top_scored_candidates(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate(repo, "AAAUSDT", score=0.5)
    b = _candidate(repo, "BBBUSDT", score=0.5)
    c = _candidate(repo, "CCCUSDT", score=0.5)
    runner = _ScriptedScreenerRunner({"AAAUSDT": 9.0, "BBBUSDT": 1.0, "CCCUSDT": 5.0})

    result = apply_opportunity_screening(
        repo,
        [a, b, c],
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=5,
        max_candidates_for_full_analysis=2,
        enforce=True,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert {c.instrument for c in result} == {"AAAUSDT", "CCCUSDT"}


def test_enforce_mode_transitions_not_selected_to_budget_limited(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate(repo, "AAAUSDT", score=0.5)
    b = _candidate(repo, "BBBUSDT", score=0.5)
    runner = _ScriptedScreenerRunner({"AAAUSDT": 9.0, "BBBUSDT": 1.0})

    apply_opportunity_screening(
        repo,
        [a, b],
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=5,
        max_candidates_for_full_analysis=1,
        enforce=True,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert repo.get_candidate(a.candidate_id).status == "CANDIDATE"
    assert repo.get_candidate(b.candidate_id).status == "BUDGET_LIMITED"


def test_enforce_mode_never_selects_a_candidate_whose_screening_failed(tmp_path):
    """Fail-closed: ett misslyckat screening-anrop får ALDRIG befordra en
    kandidat till den dyra fulla analysen, även om alla andra kandidater
    också ser svaga ut."""
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate(repo, "AAAUSDT", score=0.5)
    b = _candidate(repo, "BBBUSDT", score=0.5)
    runner = _ScriptedScreenerRunner({"AAAUSDT": None, "BBBUSDT": 0.1})

    result = apply_opportunity_screening(
        repo,
        [a, b],
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=5,
        max_candidates_for_full_analysis=2,
        enforce=True,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert {c.instrument for c in result} == {"BBBUSDT"}
    assert repo.get_candidate(a.candidate_id).status == "BUDGET_LIMITED"


def test_prescreen_pool_is_capped_and_remainder_never_calls_the_screener(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidates = [_candidate(repo, f"SYM{i}USDT", score=0.5) for i in range(4)]
    runner = _ScriptedScreenerRunner({f"SYM{i}USDT": float(i) for i in range(4)})

    apply_opportunity_screening(
        repo,
        candidates,
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=2,
        max_candidates_for_full_analysis=1,
        enforce=False,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert len(runner.called_for_instruments) == 2
    assert set(runner.called_for_instruments) == {"SYM0USDT", "SYM1USDT"}


def test_enforce_mode_sends_remainder_beyond_prescreen_pool_to_budget_limited(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidates = [_candidate(repo, f"SYM{i}USDT", score=0.5) for i in range(4)]
    runner = _ScriptedScreenerRunner({"SYM0USDT": 9.0, "SYM1USDT": 8.0})

    result = apply_opportunity_screening(
        repo,
        candidates,
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=2,
        max_candidates_for_full_analysis=1,
        enforce=True,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert {c.instrument for c in result} == {"SYM0USDT"}
    assert repo.get_candidate(candidates[2].candidate_id).status == "BUDGET_LIMITED"
    assert repo.get_candidate(candidates[3].candidate_id).status == "BUDGET_LIMITED"


def test_screening_persists_assessment_for_every_prescreened_candidate(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate(repo, "AAAUSDT", score=0.5)
    runner = _ScriptedScreenerRunner({"AAAUSDT": 7.0})

    apply_opportunity_screening(
        repo,
        [a],
        _agent_def(),
        runner,
        max_candidates_for_ai_prescreen=5,
        max_candidates_for_full_analysis=1,
        enforce=False,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    row = repo._conn.execute(
        "SELECT payload FROM assessments WHERE candidate_id = ? AND field_name = ?",
        (a.candidate_id, "opportunity_screen"),
    ).fetchone()
    assert row is not None
    import json

    payload = json.loads(row["payload"])
    assert payload["opportunity_score"] == 7.0
