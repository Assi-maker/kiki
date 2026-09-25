"""End-to-end tests for the GODFATHER supervisor sweep against a real
SQLite database seeded through the production repository methods."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.godfather.pipeline import run_thesis_tracking
from crypto_trading.godfather.supervisor import (
    render_markdown,
    run_supervisor_sweep,
    sweep_is_due,
)
from crypto_trading.godfather_loop import run_godfather_tick
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.godfather.intelligence_fixtures import evidence_record
from tests.crypto_trading.godfather.test_intelligence_pipeline import _event
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
_BASE = datetime(2026, 9, 1, tzinfo=UTC)
_ENTRY = Decimal("100")


def _seed(repo, index, prices, exit_price, exit_reason, *, size="500", run="run-a", close=True):
    """A trade observed every minute - Guardian's real cadence - so the
    sweep sees an OBSERVED path, not holes."""
    pid = f"pos-{index}"
    opened = _BASE + timedelta(hours=index * 6)
    closed = opened + timedelta(minutes=len(prices))
    repo.create_candidate_with_event(
        Candidate(
            candidate_id=pid, idempotency_key=f"k-{pid}", instrument=f"C{index}-USDT",
            discovery_run_id=f"{run}-{index // 3}", evidence_hash=f"h-{pid}",
            status="CONFIRMED", evidence_record=evidence_record(evaluated_at=opened),
            created_at=opened, updated_at=opened, reference_price=_ENTRY,
        ),
        _event("CANDIDATE_CREATED", pid, opened, "candidate"),
    )
    repo.save_gate_decision(pid, "CONFIRMED", "[]", opened)
    repo.create_position_with_event(
        Position(
            position_id=pid, candidate_id=pid, instrument=f"C{index}-USDT", direction="LONG",
            status="OPEN_POSITION", theoretical_entry=_ENTRY, simulated_fill_entry=_ENTRY,
            stop_loss=Decimal("95"), target=Decimal("110"), size=Decimal(size),
            fill_model_version="v1", opened_at=opened,
        ),
        _event("POSITION_OPENED", pid, opened, "position"),
    )
    for step, price in enumerate(prices):
        price = Decimal(str(price))
        repo.save_guardian_observation(GuardianObservation(
            observation_id=f"{pid}-{step}", position_id=pid,
            observed_at=opened + timedelta(minutes=step), state="HOLD",
            decay_score=Decimal("0.1"), progress_ratio=(price - _ENTRY) / Decimal("10"),
            unrealized_pnl=Decimal(size) * (price - _ENTRY) / _ENTRY,
            factors={"momentum_decay": 0.1, "market_regime": 0.02}, run_id="seed",
        ))
    if close:
        repo.close_position_with_event(
            pid, theoretical_exit=Decimal(str(exit_price)),
            simulated_fill_exit=Decimal(str(exit_price)), exit_reason=exit_reason,
            fees=Decimal("0.2"), funding=Decimal("0"), closed_at=closed,
            event=_event("POSITION_CLOSED", pid, closed, "position"),
        )
    return pid


def _book(repo):
    for i in range(6):
        _seed(repo, i, [100, 100.6, 101.2, 99.9, 104, 108, 109.9], 110, "target")
    for i in range(6, 12):
        _seed(repo, i, [100, 101.2, 100.5, 99, 97, 95.2], 95, "stop_loss")
    _seed(repo, 12, [100, 101.5, 102], 102, "time_limit", size="0")


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(tmp_path / "supervisor.db")


def test_the_sweep_gates_every_policy_and_records_transitions(repo):
    _book(repo)
    report = run_supervisor_sweep(repo, _settings(), _NOW, "run-1")

    policies = {row["policy_id"]: row for row in repo.find_godfather_policies()}
    assert "TIGHTEN_SL_AFTER_FAVORABLE" in policies
    assert "THESIS_POLICY" in policies
    assert "ENTRY_SELECTION_TOP_HALF" in policies
    # 12 trades: nothing can clear a 30-sample gate.
    assert all(row["status"] in ("INSUFFICIENT_DATA", "SUSPECT") for row in policies.values())
    assert len(repo.find_godfather_policy_transitions()) == len(policies)
    assert report["data"]["zero_size_excluded"] == 1
    assert report["ai_calls"] == 0
    assert "## Policy registry" in render_markdown(report)


def test_a_second_sweep_appends_no_transition_when_nothing_changed(repo):
    _book(repo)
    run_supervisor_sweep(repo, _settings(), _NOW, "run-1")
    first = len(repo.find_godfather_policy_transitions())
    run_supervisor_sweep(repo, _settings(), _NOW + timedelta(hours=7), "run-2")

    assert len(repo.find_godfather_policy_transitions()) == first


def test_the_transition_log_is_append_only(repo, tmp_path):
    _book(repo)
    run_supervisor_sweep(repo, _settings(), _NOW, "run-1")
    conn = sqlite3.connect(tmp_path / "supervisor.db")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE godfather_policy_transitions SET to_status = 'PROMOTED'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM godfather_policy_transitions")
    conn.close()


def test_entry_selection_rows_are_advisory_and_counterfactuals_are_engine_v2(repo):
    _book(repo)
    run_supervisor_sweep(repo, _settings(), _NOW, "run-1")

    rows = repo.find_godfather_entry_quality_assessments()
    assert rows and all(row["enforced"] == 0 for row in rows)
    assert all('"selection_verdict"' in row["detail_json"] for row in rows)
    counterfactuals = repo.find_godfather_counterfactuals()
    assert counterfactuals
    assert all('"engine_version": 2' in row["detail_json"] for row in counterfactuals)
    assert not any(row["position_id"] == "pos-12" for row in counterfactuals)


def test_the_sweep_is_due_only_after_its_interval(repo):
    _book(repo)
    settings = _settings()
    assert sweep_is_due(repo, settings, _NOW)
    run_supervisor_sweep(repo, settings, _NOW, "run-1")
    assert not sweep_is_due(repo, settings, _NOW + timedelta(hours=1))
    hours = settings.godfather.supervisor_sweep_interval_hours
    assert sweep_is_due(repo, settings, _NOW + timedelta(hours=hours))


def test_the_loop_runs_the_sweep_and_never_raises(repo):
    _book(repo)
    summary = run_godfather_tick(repo, _settings())

    assert "error" not in summary
    assert summary["supervisor"]["policies"] > 0


def test_thesis_tracking_records_both_decision_components_advisory(repo):
    _book(repo)
    _seed(repo, 20, [100, 100.8, 101.3, 101.1], None, None, close=False)
    written = run_thesis_tracking(repo, _settings(), _BASE + timedelta(days=6), "run-1")
    rows = repo.find_godfather_position_thesis_for_position("pos-20")

    assert written == 1
    assert rows[0]["enforced"] == 0
    assert '"profit_protection"' in rows[0]["features_json"]
    assert '"thesis_action"' in rows[0]["features_json"]
