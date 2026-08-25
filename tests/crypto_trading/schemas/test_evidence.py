from datetime import UTC, datetime

from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
    compute_candidate_idempotency_key,
    compute_evidence_hash,
)


def _make_record(evaluated_at=None) -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h", "4h"],
        evaluated_at=evaluated_at or datetime.now(UTC),
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="pct_change_1h", value=3.2, baseline=0.5, threshold=2.0
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=False, metric="rsi", value=55.0, baseline=50.0, threshold=70.0
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="volume_zscore", value=3.1, baseline=1.0, threshold=2.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=False, metric="funding_rate", value=0.01, baseline=0.01, threshold=0.05
        ),
        candidate_score=0.71,
        trigger_reasons=["price_volatility", "volume"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def test_evidence_record_outcome_never_a_direction():
    record = _make_record()
    assert record.outcome in ("worth_deeper_analysis", "not_a_candidate")


def test_evidence_hash_is_deterministic_for_identical_content():
    r1 = _make_record(evaluated_at=datetime(2026, 1, 1, tzinfo=UTC))
    r2 = _make_record(evaluated_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC))
    # olika evaluated_at, i övrigt identiskt innehåll -> evaluated_at exkluderas ur hashen
    assert compute_evidence_hash(r1) == compute_evidence_hash(r2)


def test_evidence_hash_changes_with_content():
    r1 = _make_record()
    r2 = _make_record()
    r2.candidate_score = 0.99
    assert compute_evidence_hash(r1) != compute_evidence_hash(r2)


def test_idempotency_key_is_deterministic_and_case_insensitive():
    key1 = compute_candidate_idempotency_key("BTCUSDT", "run-1", "hash-abc")
    key2 = compute_candidate_idempotency_key("btcusdt ", "run-1", "hash-abc")
    assert key1 == key2


def test_idempotency_key_differs_for_different_instruments():
    key1 = compute_candidate_idempotency_key("BTCUSDT", "run-1", "hash-abc")
    key2 = compute_candidate_idempotency_key("ETHUSDT", "run-1", "hash-abc")
    assert key1 != key2
