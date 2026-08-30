from datetime import UTC, datetime

from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_phase5_integration import _run_three_ticks_against_fresh_repo


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    repo = SQLiteRepository(db_path)
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app), repo


def test_system_health_lists_recent_runs_with_errors_and_timing(tmp_path):
    client, repo = _client(tmp_path)
    started = datetime(2026, 8, 30, 10, tzinfo=UTC)
    repo.start_run("run-1", "discovery", started)
    repo.complete_run(
        "run-1",
        datetime(2026, 8, 30, 10, 0, 5, tzinfo=UTC),
        "ok",
        [],
        instruments_scanned=7,
    )

    body = client.get("/api/system-health").json()

    row = next(r for r in body["recent_runs"] if r["run_id"] == "run-1")
    assert row["status"] == "ok"
    assert row["instruments_scanned"] == 7
    assert row["duration_seconds"] == 5.0
    assert row["errors"] == []


def test_system_health_marks_rate_limit_events_as_known_gap(tmp_path):
    client, repo = _client(tmp_path)

    body = client.get("/api/system-health").json()

    assert body["rate_limit_events"] == (
        "unavailable — throttle decisions not persisted (known gap)"
    )


def test_system_health_budget_limited_count_matches_a_real_simulated_run(tmp_path):
    """AC3: kör en riktig simulerad discovery-cykel (samma mönster som
    tests/crypto_trading/test_phase5_integration.py::
    test_daily_cap_blocks_third_candidate_across_three_discovery_ticks_deterministically)
    genom hela den riktiga gate-logiken, sedan verifieras att dashboardens
    siffra exakt matchar repositoryns egen count - inte en hårdkodad
    fixture."""
    db_path = tmp_path / "test.db"
    statuses = _run_three_ticks_against_fresh_repo(db_path)
    assert list(statuses.values()).count("BUDGET_LIMITED") == 1  # baslinje, samma som Fas 5 AC2

    repo = SQLiteRepository(db_path)
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    client = TestClient(app)

    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    expected = repo.count_candidates_by_status_since("BUDGET_LIMITED", day_start)

    body = client.get("/api/system-health").json()

    assert body["budget_limited_candidates_today"] == expected
    assert expected == 1
