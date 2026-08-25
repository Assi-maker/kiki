import sqlite3
import threading
import time
from datetime import UTC, datetime

import pytest

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.storage.repository import SQLiteRepository


def _make_evidence() -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=datetime.now(UTC),
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
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _make_candidate(candidate_id: str) -> Candidate:
    now = datetime.now(UTC)
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CANDIDATE",
        evidence_record=_make_evidence(),
        created_at=now,
        updated_at=now,
    )


def _make_event(candidate_id: str) -> Event:
    return Event(
        event_id=f"CANDIDATE_CREATED:{candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=datetime.now(UTC),
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def test_busy_timeout_lets_writer_wait_for_lock_and_succeed(tmp_path):
    """Anslutning A tvingas hålla ett skriv-lås öppet i en kontrollerad tid.
    Anslutning B:s skrivning under tiden ska VÄNTA (inte misslyckas direkt)
    och sedan lyckas när A släpper låset - bevisar att busy_timeout faktiskt
    används, deterministiskt, utan att förlita sig på trådschemaläggning."""
    db_path = tmp_path / "concurrent_wait.db"
    repo_b = SQLiteRepository(db_path, busy_timeout_ms=2000)

    hold_seconds = 0.4
    lock_acquired = threading.Event()

    def hold_write_lock():
        # repo_a skapas HÄR, inne i tråden - en sqlite3-anslutning är
        # trådbunden (check_same_thread=True som default) och kan inte
        # skapas i huvudtråden men användas i en annan tråd (upptäckt vid
        # exekvering: "SQLite objects created in a thread can only be used
        # in that same thread").
        repo_a = SQLiteRepository(db_path, busy_timeout_ms=2000)
        repo_a._conn.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        time.sleep(hold_seconds)
        repo_a._conn.commit()

    holder_thread = threading.Thread(target=hold_write_lock)
    holder_thread.start()
    assert lock_acquired.wait(timeout=2), "connection A never acquired the write lock"

    started_at = time.monotonic()
    created = repo_b.create_candidate_with_event(
        _make_candidate("cand-waits"), _make_event("cand-waits")
    )
    elapsed = time.monotonic() - started_at
    holder_thread.join(timeout=2)

    assert created is True
    # B måste faktiskt ha VÄNTAT på A:s lås - inte misslyckats direkt, inte
    # lyckats innan A ens tog låset.
    assert elapsed >= hold_seconds * 0.5, (
        f"B:s skrivning verkar inte ha väntat på A:s lås (elapsed={elapsed:.3f}s)"
    )
    assert elapsed < 2.0, f"B väntade orimligt länge (elapsed={elapsed:.3f}s)"

    verify_repo = SQLiteRepository(db_path, busy_timeout_ms=2000)
    assert verify_repo.get_candidate("cand-waits") is not None
    count = verify_repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1  # ingen dubblett, ingen korruption


def test_busy_timeout_is_respected_write_fails_after_timeout_elapses(tmp_path):
    """Anslutning A håller låset LÄNGRE än anslutning B:s egen busy_timeout.
    B:s skrivning ska misslyckas efter ungefär B:s busy_timeout - varken
    direkt (vilket skulle bevisa att busy_timeout ignoreras) eller efter hela
    A:s hålltid (vilket skulle bevisa att B väntade på fel/inget villkor)."""
    db_path = tmp_path / "concurrent_timeout.db"
    short_timeout_ms = 200
    repo_b = SQLiteRepository(db_path, busy_timeout_ms=short_timeout_ms)

    hold_seconds = 1.0  # betydligt längre än B:s busy_timeout (0.2s)
    lock_acquired = threading.Event()

    def hold_write_lock():
        # repo_a skapas i tråden - se kommentar i föregående test.
        repo_a = SQLiteRepository(db_path, busy_timeout_ms=2000)
        repo_a._conn.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        time.sleep(hold_seconds)
        repo_a._conn.commit()

    holder_thread = threading.Thread(target=hold_write_lock)
    holder_thread.start()
    assert lock_acquired.wait(timeout=2), "connection A never acquired the write lock"

    started_at = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        repo_b.create_candidate_with_event(
            _make_candidate("cand-times-out"), _make_event("cand-times-out")
        )
    elapsed = time.monotonic() - started_at
    holder_thread.join(timeout=2)

    assert elapsed >= (short_timeout_ms / 1000) * 0.5, (
        f"B misslyckades för snabbt ({elapsed:.3f}s) - busy_timeout verkar ignorerat"
    )
    assert elapsed < hold_seconds, (
        f"B väntade lika länge som A höll låset ({elapsed:.3f}s) - busy_timeout verkar inte styra väntetiden"
    )

    # Ingen korruption: B:s misslyckade skrivning lämnade ingen rad (rollback
    # skedde i create_candidate_with_event); A skrev aldrig något själv.
    verify_repo = SQLiteRepository(db_path, busy_timeout_ms=2000)
    count = verify_repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0
