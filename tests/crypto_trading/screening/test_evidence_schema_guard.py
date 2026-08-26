"""AC5: CandidateEvidenceRecord har inget fält som kan tolkas som
AI-confidence, forecast-sannolikhet eller trade-kvalitet. candidate_score
är typmässigt och namnmässigt oförväxlingsbart med de fälten som tillkommer
i Phase 3 (ForecastAssessment.scenario_probabilities m.fl., SPEC §4-tabellen)."""

from crypto_trading.schemas.assessments import ForecastAssessment
from crypto_trading.schemas.evidence import CandidateEvidenceRecord

_FORBIDDEN_NAME_FRAGMENTS = ("confidence", "probability", "quality_score", "trade_quality")


def test_candidate_evidence_record_has_no_ai_confidence_or_forecast_field():
    for field_name in CandidateEvidenceRecord.model_fields:
        assert field_name != "confidence"
        assert not any(frag in field_name for frag in _FORBIDDEN_NAME_FRAGMENTS)


def test_candidate_score_field_name_never_collides_with_forecast_assessment_fields():
    evidence_fields = set(CandidateEvidenceRecord.model_fields.keys())
    forecast_fields = set(ForecastAssessment.model_fields.keys())
    assert "candidate_score" in evidence_fields
    assert "candidate_score" not in forecast_fields
    assert evidence_fields.isdisjoint(forecast_fields)


def test_candidate_score_is_a_plain_float_not_a_probability_distribution():
    assert CandidateEvidenceRecord.model_fields["candidate_score"].annotation is float
