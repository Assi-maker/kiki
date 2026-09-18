"""Tests for the GODFATHER priority-boost ranking integration in
crypto_trading/screening/candidate_engine.py::prioritize_and_apply_budget
(2026-09-18 expansion). See crypto_trading/godfather/priority_boost.py's own
module docstring for the full design/safety reasoning this exercises."""

from datetime import UTC, datetime

from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.screening.candidate_engine import (
    prioritize_and_apply_budget,
    process_evidence,
)
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _evidence(
    instrument, candidate_score, trigger_reasons=("momentum_breakout",)
) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=candidate_score,
        trigger_reasons=list(trigger_reasons),
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _candidate(repo, instrument, score, trigger_reasons=("momentum_breakout",)):
    candidate = process_evidence(
        repo, _evidence(instrument, score, trigger_reasons),
        discovery_run_id="run-1", created_at=_NOW,
    )
    assert candidate is not None
    return candidate


def _enabled_settings():
    settings = _settings()
    return settings.model_copy(
        update={"godfather": settings.godfather.model_copy(update={"priority_boost_enabled": True})}
    )


def test_ranking_is_byte_identical_when_settings_is_none(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    low = _candidate(repo, "AAAUSDT", 0.5)
    high = _candidate(repo, "BBBUSDT", 0.6)

    within, _over = prioritize_and_apply_budget(
        repo, [low, high], {}, 2, _NOW, "run-1",
    )

    assert [c.instrument for c in within] == ["BBBUSDT", "AAAUSDT"]


def test_ranking_is_byte_identical_when_priority_boost_disabled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    low = _candidate(repo, "AAAUSDT", 0.5)
    high = _candidate(repo, "BBBUSDT", 0.6)
    settings = _settings()
    assert settings.godfather.priority_boost_enabled is False

    within, _over = prioritize_and_apply_budget(
        repo, [low, high], {}, 2, _NOW, "run-1", settings=settings,
    )

    assert [c.instrument for c in within] == ["BBBUSDT", "AAAUSDT"]


def test_ranking_never_reads_the_table_when_disabled(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "t.db")
    low = _candidate(repo, "AAAUSDT", 0.5)

    def _boom():
        raise AssertionError("must not read godfather_priority_heuristics when disabled")

    monkeypatch.setattr(repo, "find_godfather_priority_heuristics", _boom)

    prioritize_and_apply_budget(repo, [low], {}, 1, _NOW, "run-1", settings=_settings())


def test_priority_boost_reorders_a_lower_scored_candidate_above_a_higher_one(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    low_score = _candidate(repo, "AAAUSDT", 0.5, trigger_reasons=("momentum_breakout",))
    high_score = _candidate(repo, "BBBUSDT", 0.6, trigger_reasons=("volume_spike",))
    # A live priority heuristic strongly boosts momentum_breakout candidates -
    # enough to overcome AAAUSDT's 0.1 lower raw candidate_score.
    repo.upsert_godfather_priority_heuristic(
        heuristic_id="godfather-priority:test",
        description="test",
        condition_json='{"trigger_reasons": ["momentum_breakout"]}',
        adjustment=0.5,
        confidence=0.8,
        sample_size=40,
        updated_at=_NOW,
    )

    within, _over = prioritize_and_apply_budget(
        repo, [low_score, high_score], {}, 2, _NOW, "run-1", settings=_enabled_settings(),
    )

    assert [c.instrument for c in within] == ["AAAUSDT", "BBBUSDT"]


def test_priority_boost_never_mutates_the_underlying_candidate_score(tmp_path):
    """The one invariant this whole design depends on: evidence_record.
    candidate_score - baked into compute_evidence_hash()/the idempotency key
    and read by Guardian Authority's own PRE_ENTRY_VETO heuristics - must
    stay exactly what evaluate_candidate() computed, no matter what the
    ranking overlay does."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _candidate(repo, "AAAUSDT", 0.5)
    original_score = candidate.evidence_record.candidate_score
    original_hash = candidate.evidence_hash
    repo.upsert_godfather_priority_heuristic(
        heuristic_id="godfather-priority:test",
        description="test",
        condition_json='{"trigger_reasons": ["momentum_breakout"]}',
        adjustment=0.5,
        confidence=0.8,
        sample_size=40,
        updated_at=_NOW,
    )

    within, _over = prioritize_and_apply_budget(
        repo, [candidate], {}, 1, _NOW, "run-1", settings=_enabled_settings(),
    )

    assert within[0].evidence_record.candidate_score == original_score
    assert within[0].evidence_hash == original_hash
    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded.evidence_record.candidate_score == original_score


def test_priority_boost_can_never_promote_a_candidate_past_the_hard_eligibility_gate(tmp_path):
    """The ranking overlay only ever reorders candidates ALREADY passed in -
    it cannot create one. A 'not_a_candidate' evidence never even reaches
    prioritize_and_apply_budget in the first place (process_evidence returns
    None), regardless of any live priority heuristic."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.upsert_godfather_priority_heuristic(
        heuristic_id="godfather-priority:test",
        description="test",
        condition_json='{"instrument": "ZZZUSDT"}',
        adjustment=0.9,
        confidence=0.9,
        sample_size=40,
        updated_at=_NOW,
    )
    evidence = _evidence("ZZZUSDT", 0.5)
    evidence = evidence.model_copy(update={"outcome": "not_a_candidate"})

    candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert candidate is None
