from datetime import UTC, datetime

from intelligence.reporting.report import render_report, write_report
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.opportunity import Opportunity

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


def _full_opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="opp-42",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="Ovanlig aktivitet kring X",
        summary="Kort sammanfattning",
        time_horizon="7 dagar",
        liquidity="okänd",
        status="reported",
        research=ResearchAssessment(
            **_A,
            verified_facts=["fakta 1"],
            source_references=["https://x.com"],
            assumptions=[],
        ),
        opportunity=OpportunityAssessment(
            **_A,
            observed_data="ovanlig volym",
            hypothesis="efterfrågan stiger",
            interpretation="tidig signal",
        ),
        market=MarketAssessment(
            **_A,
            market_data={"volume_change_pct": 300.0},
            interpretation="ovanlig rörelse",
        ),
        forecast=ForecastAssessment(
            **_A,
            scenarios=[{"description": "fortsätter", "probability": 0.6}],
            confidence=0.7,
            uncertainty="litet underlag",
        ),
        risk=RiskAssessment(
            **_A,
            downside="kan reversera",
            liquidity_risk="låg",
            model_risk="litet urval",
            timing_risk="sent",
        ),
        bear=BearAssessment(
            **_A,
            counterarguments=["kan vara brus"],
            alternative_explanations=["säsong"],
            falsification_conditions="om volymen normaliseras inom 48h",
        ),
        qa=QAAssessment(**_A, passed=True, violations=[]),
        score=0.62,
        score_breakdown={
            "signal_strength": 0.6,
            "data_quality": 0.2,
            "source_reliability": 0.2,
            "potential": 0.7,
            "risk": 0.8,
            "confidence": 0.7,
            "novelty": 0.5,
        },
    )


def test_render_report_contains_required_sections():
    md = render_report(_full_opportunity())
    for heading in [
        "OPPORTUNITY #opp-42",
        "Vad hände?",
        "Varför är detta intressant?",
        "Vilka bevis finns?",
        "Marknadsdata",
        "Vad talar FÖR?",
        "Vad talar EMOT?",
        "Vilka alternativa förklaringar finns?",
        "Vad kan hända?",
        "Sannolikheter:",
        "Risk:",
        "Data quality:",
        "Confidence:",
        "Overall opportunity score:",
        "Time horizon:",
        "Vad skulle falsifiera hypotesen?",
        "Status:",
        "Ej finansiell rådgivning",
    ]:
        assert heading in md, f"saknar rubrik/text: {heading}"


def test_render_report_includes_market_data_and_interpretation():
    # Finding #6: MarketAssessment.market_data / .interpretation previously fed
    # neither scoring nor reporting at all.
    md = render_report(_full_opportunity())
    assert "volume_change_pct: 300.0" in md
    assert "ovanlig rörelse" in md


def test_render_report_handles_missing_market_assessment():
    opp = _full_opportunity()
    opp.market = None
    md = render_report(opp)
    assert "Marknadsdata" in md
    assert "Ej tillgängligt" in md


def test_render_report_does_not_raise_on_differently_shaped_scenario_dict():
    # Finding #2: ForecastAssessment.scenarios is an untyped list[dict] — a
    # schema-valid LLM response can use different keys than "description"/
    # "probability" and still pass pydantic validation. render_report must
    # degrade gracefully (fallback values), never raise KeyError.
    opp = _full_opportunity()
    opp.forecast = ForecastAssessment(
        **_A,
        scenarios=[{"scenario": "up", "prob": 0.6}],
        confidence=0.7,
        uncertainty="litet underlag",
    )
    md = render_report(opp)
    assert "?" in md
    assert "0%" in md


def test_write_report_creates_file(tmp_path):
    path = write_report(_full_opportunity(), dest_dir=tmp_path)
    assert path.exists()
    assert path.name.endswith("-opportunity-opp-42.md")
    assert "OPPORTUNITY #opp-42" in path.read_text(encoding="utf-8")
