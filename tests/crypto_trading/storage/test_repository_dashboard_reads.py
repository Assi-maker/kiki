from datetime import UTC, datetime

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _evidence() -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.82,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _make_candidate(
    candidate_id: str, created_at: datetime, status: str = "CANDIDATE"
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status=status,
        evidence_record=_evidence(),
        created_at=created_at,
        updated_at=created_at,
    )


def _make_candidate_event(candidate: Candidate) -> Event:
    return Event(
        event_id=f"CANDIDATE_CREATED:{candidate.candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=candidate.created_at,
        run_id=candidate.discovery_run_id,
        schema_version=1,
        payload={},
    )


def _make_position(
    position_id: str, opened_at: datetime, status: str = "OPEN_POSITION"
) -> Position:
    return Position(
        position_id=position_id,
        candidate_id=f"cand-{position_id}",
        instrument="BTCUSDT",
        direction="LONG",
        status=status,
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=opened_at,
    )


def _make_position_event(position: Position) -> Event:
    return Event(
        event_id=f"POSITION_OPENED:{position.position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=position.opened_at,
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def _make_forecast(forecast_id: str, forecast_timestamp: datetime) -> ForecastRecord:
    return ForecastRecord(
        forecast_id=forecast_id,
        candidate_id=f"cand-{forecast_id}",
        instrument="BTCUSDT",
        forecast_timestamp=forecast_timestamp,
        horizon="24h",
        scenario_probabilities={"up": 0.6, "down": 0.4},
        forecast_version="v1",
        market_state_metadata={},
    )


# --- find_all_candidates ---------------------------------------------------


def test_find_all_candidates_returns_empty_on_empty_db(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_all_candidates(limit=10) == []


def test_find_all_candidates_orders_most_recent_first_and_respects_limit_offset(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    for i, hour in enumerate([10, 11, 12]):
        candidate = _make_candidate(f"cand-{i}", datetime(2026, 8, 30, hour, tzinfo=UTC))
        repo.create_candidate_with_event(candidate, _make_candidate_event(candidate))

    all_rows = repo.find_all_candidates(limit=10)
    assert [c.candidate_id for c in all_rows] == ["cand-2", "cand-1", "cand-0"]

    first_page = repo.find_all_candidates(limit=2)
    assert [c.candidate_id for c in first_page] == ["cand-2", "cand-1"]

    second_page = repo.find_all_candidates(limit=2, offset=2)
    assert [c.candidate_id for c in second_page] == ["cand-0"]


def test_find_all_candidates_skips_corrupt_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    valid = _make_candidate("cand-valid", datetime(2026, 8, 30, 10, tzinfo=UTC))
    repo.create_candidate_with_event(valid, _make_candidate_event(valid))
    corrupt = _make_candidate("cand-corrupt", datetime(2026, 8, 30, 11, tzinfo=UTC))
    repo.create_candidate_with_event(corrupt, _make_candidate_event(corrupt))
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' "
        "WHERE candidate_id = 'cand-corrupt'"
    )
    repo._conn.commit()

    result = repo.find_all_candidates(limit=10)

    assert [c.candidate_id for c in result] == ["cand-valid"]


# --- find_all_positions -----------------------------------------------------


def test_find_all_positions_returns_empty_on_empty_db(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_all_positions(limit=10) == []


def test_find_all_positions_includes_closed_and_respects_limit_offset(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    open_pos = _make_position("pos-open", datetime(2026, 8, 30, 10, tzinfo=UTC))
    closed_pos = _make_position(
        "pos-closed", datetime(2026, 8, 30, 11, tzinfo=UTC), status="CLOSED"
    )
    repo.create_position_with_event(open_pos, _make_position_event(open_pos))
    repo.create_position_with_event(closed_pos, _make_position_event(closed_pos))

    all_rows = repo.find_all_positions(limit=10)
    assert {p.position_id for p in all_rows} == {"pos-open", "pos-closed"}
    assert [p.position_id for p in all_rows] == ["pos-closed", "pos-open"]

    first_page = repo.find_all_positions(limit=1)
    assert [p.position_id for p in first_page] == ["pos-closed"]

    second_page = repo.find_all_positions(limit=1, offset=1)
    assert [p.position_id for p in second_page] == ["pos-open"]


# --- get_gate_decision -------------------------------------------------------


def test_get_gate_decision_returns_none_when_missing(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.get_gate_decision("does-not-exist") is None


def test_get_gate_decision_returns_saved_decision(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate("cand-1", _NOW)
    repo.create_candidate_with_event(candidate, _make_candidate_event(candidate))
    repo.save_gate_decision(
        "cand-1", decision="CONFIRMED", reasons=["all checks passed"], evaluated_at=_NOW
    )

    result = repo.get_gate_decision("cand-1")

    assert result["decision"] == "CONFIRMED"
    assert result["reasons"] == ["all checks passed"]


# --- find_latest_run ---------------------------------------------------------


def test_find_latest_run_returns_none_when_no_runs(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_latest_run("discovery") is None


def test_find_latest_run_returns_most_recent_of_given_type(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    repo.start_run("run-old", "discovery", datetime(2026, 8, 30, 10, tzinfo=UTC))
    repo.start_run("run-new", "discovery", datetime(2026, 8, 30, 11, tzinfo=UTC))
    repo.start_run("run-other-type", "monitoring", datetime(2026, 8, 30, 12, tzinfo=UTC))

    result = repo.find_latest_run("discovery")

    assert result["run_id"] == "run-new"


# --- find_recent_runs ---------------------------------------------------------


def test_find_recent_runs_returns_empty_on_empty_db(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_recent_runs(limit=10) == []


def test_find_recent_runs_orders_most_recent_first_across_all_types(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    repo.start_run("run-1", "discovery", datetime(2026, 8, 30, 10, tzinfo=UTC))
    repo.start_run("run-2", "monitoring", datetime(2026, 8, 30, 11, tzinfo=UTC))
    repo.start_run("run-3", "notify", datetime(2026, 8, 30, 12, tzinfo=UTC))

    result = repo.find_recent_runs(limit=10)

    assert [r["run_id"] for r in result] == ["run-3", "run-2", "run-1"]


def test_find_recent_runs_respects_limit_and_offset(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    repo.start_run("run-1", "discovery", datetime(2026, 8, 30, 10, tzinfo=UTC))
    repo.start_run("run-2", "discovery", datetime(2026, 8, 30, 11, tzinfo=UTC))
    repo.start_run("run-3", "discovery", datetime(2026, 8, 30, 12, tzinfo=UTC))

    first_page = repo.find_recent_runs(limit=2)
    assert [r["run_id"] for r in first_page] == ["run-3", "run-2"]

    second_page = repo.find_recent_runs(limit=2, offset=2)
    assert [r["run_id"] for r in second_page] == ["run-1"]


# --- find_all_forecasts -------------------------------------------------------


def test_find_all_forecasts_returns_empty_on_empty_db(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_all_forecasts(limit=10) == []


def test_find_all_forecasts_orders_most_recent_first_and_respects_limit_offset(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    for i, hour in enumerate([10, 11, 12]):
        repo.save_forecast_record(
            _make_forecast(f"fc-{i}", datetime(2026, 8, 30, hour, tzinfo=UTC))
        )

    all_rows = repo.find_all_forecasts(limit=10)
    assert [f.forecast_id for f in all_rows] == ["fc-2", "fc-1", "fc-0"]

    first_page = repo.find_all_forecasts(limit=2)
    assert [f.forecast_id for f in first_page] == ["fc-2", "fc-1"]

    second_page = repo.find_all_forecasts(limit=2, offset=2)
    assert [f.forecast_id for f in second_page] == ["fc-0"]
