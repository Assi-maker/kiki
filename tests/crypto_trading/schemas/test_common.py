from typing import get_args

from crypto_trading.schemas.common import (
    AssessmentStatus,
    CandidateStatus,
    DataQualityStatus,
    PositionStatus,
)


def test_candidate_status_has_exactly_eight_values():
    assert set(get_args(CandidateStatus)) == {
        "CANDIDATE",
        "DATA_INVALID",
        "BUDGET_LIMITED",
        "UNDER_AI_ANALYSIS",
        "ANALYSIS_INTERRUPTED",
        "REJECTED",
        "NO_TRADE",
        "CONFIRMED",
    }


def test_candidate_status_has_no_unknown_state_member():
    assert "UNKNOWN_STATE" not in get_args(CandidateStatus)


def test_position_status_values():
    assert set(get_args(PositionStatus)) == {"OPEN_POSITION", "CLOSED"}


def test_assessment_status_values():
    assert set(get_args(AssessmentStatus)) == {"ok", "failed", "timeout"}


def test_data_quality_status_values():
    assert set(get_args(DataQualityStatus)) == {"ok", "degraded", "invalid"}
