"""One-off script: runs `run_full_chain_historical_replay` across the real
21-day historical window (data pre-fetched by `full_chain_data_prep.py`)
using `MockAgentRunner` - a FREE (zero Anthropic API cost) end-to-end wiring/
correctness proof of the mechanical pipeline (screening, ranking, Gate,
PRE_ENTRY_VETO, TIGHTEN_SL/CLOSE_EARLY/TAKE_PROFIT, SL/TP/time-limit exits,
Profit Protection) against real market data, before any real-AI phase spends
real money. Not part of the reusable library API - a one-shot verification
script, kept for reproducibility."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.agents.runner import AgentRunner, MockAgentRunner
from crypto_trading.backtest.full_chain_data_prep import prepare
from crypto_trading.backtest.full_chain_replay import (
    HistoricalDataSource,
    run_full_chain_historical_replay,
)
from crypto_trading.config.loader import get_settings
from crypto_trading.logging import new_run_id
from crypto_trading.schemas.assessments import AssessmentBase
from crypto_trading.storage.repository import SQLiteRepository

_START = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
_END = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
_CACHE_DIR = Path("backtest_output/full_chain_cache")
_OUTPUT_DIR = Path("backtest_output/full_chain_mock_verification")

_ROLE_AGENT_NAMES = [
    "crypto-bull-thesis",
    "crypto-bear-adversarial",
    "crypto-technical-analyst",
    "crypto-news-sentiment",
    "crypto-forecast-agent",
    "crypto-risk-agent",
    "crypto-qa-gate",
]


def _mock_fixtures(run_id: str, now: datetime) -> dict[str, AssessmentBase]:
    """Plausible, clearly-synthetic CONFIRMED-leaning fixtures for every AI
    role the pipeline can call EXCEPT the risk role (see
    `_ReferencePriceAwareRunner` below - a static suggested_stop_loss/
    suggested_target could never be a sane absolute price across instruments
    ranging from ~$0.01 to ~$100,000). Both GODFATHER strategist roles
    return an EMPTY proposal list - a mocked strategist proposing "real"
    heuristics would be fabricated evidence, not a legitimate wiring test."""
    from crypto_trading.schemas.assessments import (
        BearAdversarialAssessment,
        BullThesisAssessment,
        ForecastAssessment,
        GodfatherPriorityStrategistAssessment,
        GodfatherStrategistAssessment,
        NewsSentimentAssessment,
        OpportunityScreenAssessment,
        QAAssessment,
        TechnicalAssessment,
    )
    from crypto_trading.schemas.guardian import GuardianAssessment

    base = {"run_id": run_id, "created_at": now, "status": "ok"}
    fixtures: dict[str, AssessmentBase] = {
        "crypto-bull-thesis": BullThesisAssessment(
            agent_name="crypto-bull-thesis",
            hypothesis="Synthetic mock hypothesis for wiring verification.",
            catalyst="Synthetic mock catalyst.", setup="Synthetic mock setup.", **base,
        ),
        "crypto-bear-adversarial": BearAdversarialAssessment(
            agent_name="crypto-bear-adversarial",
            counterarguments=["Synthetic mock counterargument."],
            alternative_explanations=["Synthetic mock alternative explanation."],
            falsification_conditions="Synthetic mock falsification condition.", **base,
        ),
        "crypto-technical-analyst": TechnicalAssessment(
            agent_name="crypto-technical-analyst", market_data={},
            interpretation="Synthetic mock technical interpretation.", **base,
        ),
        "crypto-news-sentiment": NewsSentimentAssessment(
            agent_name="crypto-news-sentiment", verified_facts=[], source_claims=[],
            interpretation="No synthetic news signal.", **base,
        ),
        "crypto-forecast-agent": ForecastAssessment(
            agent_name="crypto-forecast-agent",
            scenario_probabilities={"up": 0.34, "flat": 0.33, "down": 0.33},
            horizon="24h", forecast_version="mock-v1", **base,
        ),
        "crypto-qa-gate": QAAssessment(
            agent_name="crypto-qa-gate", passed=True, violations=[], **base
        ),
        "crypto-opportunity-screener": OpportunityScreenAssessment(
            agent_name="crypto-opportunity-screener", opportunity_score=0.5,
            reasoning="Synthetic mock screener score.", **base,
        ),
        "crypto-guardian": GuardianAssessment(
            agent_name="crypto-guardian", reasoning="Synthetic mock Guardian narration.", **base,
        ),
        "crypto-godfather-strategist": GodfatherStrategistAssessment(
            agent_name="crypto-godfather-strategist", proposed_heuristics=[], **base,
        ),
        "crypto-godfather-priority-strategist": GodfatherPriorityStrategistAssessment(
            agent_name="crypto-godfather-priority-strategist", proposed_heuristics=[], **base,
        ),
    }
    return fixtures


_RISK_AGENT_NAME = "crypto-risk-agent"
# -2%/+4% from reference_price (2:1 reward:risk) - a plausible, non-fabricated proxy
_STOP_LOSS_FRACTION = Decimal("0.02")
_TARGET_FRACTION = Decimal("0.04")


class _ReferencePriceAwareRunner(AgentRunner):
    """Wraps a MockAgentRunner for every role except `crypto-risk-agent`,
    for which it computes a per-candidate, per-instrument REALISTIC absolute
    stop_loss/target from that candidate's own real `reference_price`
    (already present in the risk role's context, see
    `orchestrator.py::_build_context`) - a plain MockAgentRunner returns the
    identical fixture for every call, which cannot be a sane absolute price
    across instruments spanning ~$0.01 to ~$100,000. This is test-script
    infrastructure only, not a change to `agents/runner.py` itself."""

    def __init__(self, delegate: MockAgentRunner) -> None:
        self._delegate = delegate

    def run(self, agent_def: AgentDefinition, context: dict, output_schema):
        if agent_def.name != _RISK_AGENT_NAME:
            result = self._delegate.run(agent_def, context, output_schema)
            self.last_call_billed = self._delegate.last_call_billed
            self.last_call_cost_usd = self._delegate.last_call_cost_usd
            return result

        reference_price_raw = context.get("reference_price")
        reference_price = Decimal(reference_price_raw) if reference_price_raw else Decimal("1")
        self.last_call_billed = True
        self.last_call_cost_usd = Decimal("0")
        return output_schema(
            agent_name=agent_def.name,
            run_id=context.get("run_id", "unknown"),
            created_at=datetime.now(UTC),
            status="ok",
            suggested_stop_loss=str(reference_price * (1 - _STOP_LOSS_FRACTION)),
            suggested_target=str(reference_price * (1 + _TARGET_FRACTION)),
            downside="Synthetic mock downside.", liquidity_risk="Synthetic mock liquidity risk.",
            model_risk="Synthetic mock model risk.", timing_risk="Synthetic mock timing risk.",
        )


def main() -> None:
    settings = get_settings()
    settings = settings.model_copy(
        update={
            "guardian": settings.guardian.model_copy(update={"authority_enabled": True}),
            "godfather": settings.godfather.model_copy(update={"priority_boost_enabled": True}),
        }
    )

    universe, contracts_raw, dataset, manifest = prepare(_START, _END, _CACHE_DIR)
    print("universe manifest:", json.dumps(manifest, indent=2))

    source = HistoricalDataSource(dataset, universe)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = _OUTPUT_DIR / "mock_verification.db"
    if db_path.exists():
        raise SystemExit(
            f"refusing to reuse an existing db: {db_path} - remove it first for a fresh run"
        )
    repo = SQLiteRepository(db_path)

    run_id = new_run_id()
    runner = _ReferencePriceAwareRunner(MockAgentRunner(fixtures=_mock_fixtures(run_id, _START)))

    result = run_full_chain_historical_replay(
        repo, runner, settings, source, _START, _END, run_id, screener_runner=runner,
    )

    all_positions = repo.find_all_positions(limit=100000)
    by_exit_reason: dict[str, int] = {}
    n_open = 0
    for position in all_positions:
        if position.status == "OPEN_POSITION":
            n_open += 1
        else:
            by_exit_reason[position.exit_reason or "unknown"] = (
                by_exit_reason.get(position.exit_reason or "unknown", 0) + 1
            )

    summary = {
        "driver_result": result,
        "n_positions_total": len(all_positions),
        "n_positions_open_at_end": n_open,
        "n_positions_closed_by_exit_reason": by_exit_reason,
        "db_path": str(db_path),
    }
    (_OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
