from __future__ import annotations

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.roles import ROLE_MAP
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.schemas.assessments import QAAssessment
from crypto_trading.schemas.candidate import Candidate


def run_qa_gate(candidate: Candidate, runner: AgentRunner, run_id: str) -> QAAssessment:
    """Tunn wrapper runt roll #7 (samma anropsform som de övriga sex) - eget
    modul för att matcha SPEC §3:s filstruktur (gate/qa_gate.py)."""
    spec = ROLE_MAP["qa"]
    agent_def = load_agent_definition(spec.agent_file)
    context = {
        "candidate_id": candidate.candidate_id,
        "instrument": candidate.instrument,
        "news_sentiment": candidate.news_sentiment.model_dump(mode="json")
        if candidate.news_sentiment
        else None,
        "technical": candidate.technical.model_dump(mode="json") if candidate.technical else None,
        "bull_thesis": candidate.bull_thesis.model_dump(mode="json")
        if candidate.bull_thesis
        else None,
        "forecast": candidate.forecast.model_dump(mode="json") if candidate.forecast else None,
        "risk": candidate.risk.model_dump(mode="json") if candidate.risk else None,
        "bear_adversarial": candidate.bear_adversarial.model_dump(mode="json")
        if candidate.bear_adversarial
        else None,
        "run_id": run_id,
    }
    return runner.run(agent_def, context, QAAssessment)
