from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.event import Event
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

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _evidence(
    instrument="BTCUSDT",
    outcome="worth_deeper_analysis",
    data_quality_status="ok",
    candidate_score=0.5,
    trigger_reasons=None,
) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=False, metric="m", value=0.0, baseline=0.0, threshold=1.0)
    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=candidate_score,
        trigger_reasons=trigger_reasons or [],
        data_quality_status=data_quality_status,
        outcome=outcome,
    )


def _rejection_event(candidate_id: str) -> Event:
    return Event(
        event_id=f"REJECTED:{candidate_id}",
        event_type="CANDIDATE_REJECTED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def test_process_evidence_creates_candidate_when_signal_triggered(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(trigger_reasons=["price_volatility"])

    candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert candidate is not None
    assert candidate.status == "CANDIDATE"
    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded is not None
    assert reloaded.status == "CANDIDATE"


def test_process_evidence_returns_none_and_persists_nothing_when_not_a_candidate(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(outcome="not_a_candidate", trigger_reasons=[])

    candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert candidate is None
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_process_evidence_creates_data_invalid_candidate_regardless_of_outcome(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(data_quality_status="invalid", outcome="not_a_candidate")

    candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert candidate is not None
    assert candidate.status == "DATA_INVALID"
    events = repo._conn.execute(
        "SELECT event_type FROM events WHERE aggregate_id = ? ORDER BY seq",
        (candidate.candidate_id,),
    ).fetchall()
    event_types = [e["event_type"] for e in events]
    assert "CANDIDATE_CREATED" in event_types
    assert "CANDIDATE_TRANSITIONED" in event_types


def test_process_evidence_is_idempotent_on_identical_evidence_and_run(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(trigger_reasons=["price_volatility"])

    first = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)
    second = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert first.candidate_id == second.candidate_id
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1


def test_process_evidence_skips_reanalysis_within_cooldown_when_score_unchanged(tmp_path):
    """AC3: en tidigare REJECTED-candidate återanalyseras inte inom
    cooldown-fönstret om evidensen inte förändrats över tröskeln."""
    repo = SQLiteRepository(tmp_path / "t.db")
    first_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)
    first = process_evidence(repo, first_evidence, discovery_run_id="run-1", created_at=_NOW)
    repo.transition_candidate_with_event(
        first.candidate_id, "REJECTED", _NOW, _rejection_event(first.candidate_id)
    )

    later = _NOW + timedelta(minutes=30)  # inom 60 min cooldown
    similar_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.55)

    result = process_evidence(
        repo,
        similar_evidence,
        discovery_run_id="run-2",
        created_at=later,
        cooldown_minutes=60,
        evidence_change_threshold=0.15,
    )

    assert result is None
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1


def test_process_evidence_allows_reanalysis_when_evidence_changed_enough(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)
    first = process_evidence(repo, first_evidence, discovery_run_id="run-1", created_at=_NOW)
    repo.transition_candidate_with_event(
        first.candidate_id, "REJECTED", _NOW, _rejection_event(first.candidate_id)
    )

    later = _NOW + timedelta(minutes=30)
    changed_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.9)

    result = process_evidence(
        repo,
        changed_evidence,
        discovery_run_id="run-2",
        created_at=later,
        cooldown_minutes=60,
        evidence_change_threshold=0.15,
    )

    assert result is not None
    assert result.status == "CANDIDATE"


def test_process_evidence_allows_reanalysis_after_cooldown_expires(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)
    first = process_evidence(repo, first_evidence, discovery_run_id="run-1", created_at=_NOW)
    repo.transition_candidate_with_event(
        first.candidate_id, "REJECTED", _NOW, _rejection_event(first.candidate_id)
    )

    after_cooldown = _NOW + timedelta(minutes=61)
    same_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)

    result = process_evidence(
        repo,
        same_evidence,
        discovery_run_id="run-2",
        created_at=after_cooldown,
        cooldown_minutes=60,
        evidence_change_threshold=0.15,
    )

    assert result is not None


def _candidate_via_process_evidence(repo, instrument, score, run_id="run-1", at=_NOW):
    evidence = _evidence(instrument=instrument, trigger_reasons=["price_volatility"], candidate_score=score)
    return process_evidence(repo, evidence, discovery_run_id=run_id, created_at=at)


def test_prioritize_and_apply_budget_keeps_highest_score_within_budget(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    low = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.2)
    high = _candidate_via_process_evidence(repo, "BBBUSDT", score=0.9)

    within, over = prioritize_and_apply_budget(
        repo,
        [low, high],
        liquidity_by_instrument={},
        max_candidates_per_discovery_run=1,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert [c.instrument for c in within] == ["BBBUSDT"]
    assert [c.instrument for c in over] == ["AAAUSDT"]


def test_prioritize_and_apply_budget_transitions_excess_to_budget_limited(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    low = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.2)
    high = _candidate_via_process_evidence(repo, "BBBUSDT", score=0.9)

    prioritize_and_apply_budget(
        repo,
        [low, high],
        liquidity_by_instrument={},
        max_candidates_per_discovery_run=1,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    reloaded_low = repo.get_candidate(low.candidate_id)
    reloaded_high = repo.get_candidate(high.candidate_id)
    assert reloaded_low.status == "BUDGET_LIMITED"
    assert reloaded_high.status == "CANDIDATE"  # oförändrad - inga AI-anrop i denna fas


def test_prioritize_and_apply_budget_never_marks_excess_as_rejected(tmp_path):
    """SPEC §10: BUDGET_LIMITED, aldrig REJECTED - skiljer resursbrist från
    sakligt underkännande."""
    repo = SQLiteRepository(tmp_path / "t.db")
    only = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.2)

    prioritize_and_apply_budget(
        repo,
        [only],
        liquidity_by_instrument={},
        max_candidates_per_discovery_run=0,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    reloaded = repo.get_candidate(only.candidate_id)
    assert reloaded.status == "BUDGET_LIMITED"


def test_prioritize_and_apply_budget_uses_liquidity_as_tiebreaker(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.5)
    b = _candidate_via_process_evidence(repo, "BBBUSDT", score=0.5)  # samma score

    within, _over = prioritize_and_apply_budget(
        repo,
        [a, b],
        liquidity_by_instrument={"AAAUSDT": Decimal("1000"), "BBBUSDT": Decimal("9000")},
        max_candidates_per_discovery_run=1,
        evaluated_at=_NOW,
        run_id="run-1",
    )

    assert [c.instrument for c in within] == ["BBBUSDT"]  # högre likviditet vinner vid oavgjort
