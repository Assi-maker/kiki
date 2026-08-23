from datetime import UTC, datetime

from intelligence.schemas.opportunity import Opportunity


def _base():
    return dict(
        opportunity_id="opp-1",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="Ovanlig aktivitet kring X",
        summary="Kort sammanfattning",
        time_horizon="7 dagar",
        liquidity="okänd",
    )


def test_default_status_is_candidate():
    opp = Opportunity(**_base())
    assert opp.status == "candidate"


def test_assessments_default_to_none():
    opp = Opportunity(**_base())
    assert opp.research is None
    assert opp.opportunity is None
    assert opp.market is None
    assert opp.forecast is None
    assert opp.risk is None
    assert opp.bear is None
    assert opp.qa is None
    assert opp.score is None


def test_status_rejects_invalid_value():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Opportunity(**_base(), status="finished")
