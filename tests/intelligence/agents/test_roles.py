from intelligence.agents.loader import load_agent_definition
from intelligence.agents.roles import ROLE_MAP
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)

_EXPECTED_TYPES = {
    "research": ResearchAssessment,
    "opportunity": OpportunityAssessment,
    "market": MarketAssessment,
    "forecast": ForecastAssessment,
    "risk": RiskAssessment,
    "bear": BearAssessment,
    "qa": QAAssessment,
}


def test_all_seven_roles_present():
    assert set(ROLE_MAP.keys()) == set(_EXPECTED_TYPES.keys())


def test_role_assessment_types_match():
    for role, spec in ROLE_MAP.items():
        assert spec.assessment_type is _EXPECTED_TYPES[role]


def test_all_agent_files_exist_and_load():
    for _role, spec in ROLE_MAP.items():
        definition = load_agent_definition(spec.agent_file)
        assert definition.name
