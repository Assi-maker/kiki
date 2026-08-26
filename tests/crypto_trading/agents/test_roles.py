from crypto_trading.agents.roles import ROLE_MAP
from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.candidate import Candidate


def test_role_map_has_all_seven_roles():
    assert set(ROLE_MAP.keys()) == {
        "news_sentiment",
        "technical",
        "bull_thesis",
        "forecast",
        "risk",
        "bear_adversarial",
        "qa",
    }


def test_role_map_assessment_types_match_schemas():
    assert ROLE_MAP["news_sentiment"].assessment_type is NewsSentimentAssessment
    assert ROLE_MAP["technical"].assessment_type is TechnicalAssessment
    assert ROLE_MAP["bull_thesis"].assessment_type is BullThesisAssessment
    assert ROLE_MAP["forecast"].assessment_type is ForecastAssessment
    assert ROLE_MAP["risk"].assessment_type is RiskAssessment
    assert ROLE_MAP["bear_adversarial"].assessment_type is BearAdversarialAssessment
    assert ROLE_MAP["qa"].assessment_type is QAAssessment


def test_role_map_keys_match_candidate_optional_field_names():
    """Strukturell garanti: orchestratorn kan göra setattr(candidate, role, ...)
    rakt av utan en separat översättningstabell."""
    candidate_fields = set(Candidate.model_fields.keys())
    assert set(ROLE_MAP.keys()) <= candidate_fields
