from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_trading.schemas.detective import DetectiveAnalysisRecord, DetectiveBatchAnalysis

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_detective_batch_analysis_is_an_assessment_base_subclass():
    """Måste vara en AssessmentBase-subklass för att kunna återanvända
    AgentRunner.run()/MockAgentRunner/RealClaudeRunner._failed_assessment()
    rakt av, exakt som OpportunityScreenAssessment redan gör."""
    analysis = DetectiveBatchAnalysis(
        agent_name="crypto-detective",
        run_id="run-1",
        created_at=_NOW,
        status="ok",
        observations=["obs"],
        winning_patterns=["win"],
        losing_patterns=["loss"],
    )
    assert analysis.status == "ok"
    assert analysis.observations == ["obs"]


def test_detective_batch_analysis_requires_all_three_pattern_lists():
    with pytest.raises(ValidationError):
        DetectiveBatchAnalysis(
            agent_name="crypto-detective",
            run_id="run-1",
            created_at=_NOW,
            status="ok",
            observations=["obs"],
            winning_patterns=["win"],
        )


def test_detective_analysis_record_round_trips_all_fields():
    record = DetectiveAnalysisRecord(
        analysis_id="detective-1",
        created_at=_NOW,
        position_ids=["pos-1", "pos-2"],
        win_count=1,
        loss_count=1,
        breakeven_count=0,
        status="ok",
        observations=["obs"],
        winning_patterns=["win"],
        losing_patterns=["loss"],
        stats_snapshot={"a": 1},
        ai_cost_usd=Decimal("0.05"),
    )
    assert record.position_ids == ["pos-1", "pos-2"]
    assert record.ai_cost_usd == Decimal("0.05")


def test_detective_analysis_record_rejects_invalid_status():
    with pytest.raises(ValidationError):
        DetectiveAnalysisRecord(
            analysis_id="detective-1",
            created_at=_NOW,
            position_ids=[],
            win_count=0,
            loss_count=0,
            breakeven_count=0,
            status="bogus",
            observations=[],
            winning_patterns=[],
            losing_patterns=[],
            stats_snapshot={},
            ai_cost_usd=Decimal("0"),
        )
