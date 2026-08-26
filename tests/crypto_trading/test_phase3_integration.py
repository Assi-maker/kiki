"""Fullständig kedja: CANDIDATE -> UNDER_AI_ANALYSIS -> sju roller -> QA-gate
-> Risk/Signal Gate -> terminal status, uteslutande via MockAgentRunner."""

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.orchestrator import run_discovery_cycle
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_discovery_wiring import _persisted_candidate_in_status
from tests.crypto_trading.test_orchestrator import _happy_fixtures, _settings


def test_end_to_end_confirmed_path(tmp_path):
    """Full kedja: alla sju roller "ok", QA passed=True, Risk/Signal Gate har
    ledig kapacitet -> CONFIRMED."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    results = run_discovery_cycle(repo=repo, runner=runner, settings=_settings(), run_id="run-1")

    assert results[0].status == "CONFIRMED"
    reloaded = repo.get_candidate(results[0].candidate_id)
    assert reloaded.status == "CONFIRMED"
    assert reloaded.qa is not None
    assert reloaded.qa.passed is True


def test_end_to_end_rejected_path(tmp_path):
    """Samma kedja, men QA.passed=False -> REJECTED, aldrig CONFIRMED."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    fixtures = _happy_fixtures()
    fixtures["crypto-qa-gate"] = fixtures["crypto-qa-gate"].model_copy(
        update={"passed": False, "violations": ["intern motsägelse mellan Bull och Risk"]}
    )
    runner = MockAgentRunner(fixtures=fixtures)

    results = run_discovery_cycle(repo=repo, runner=runner, settings=_settings(), run_id="run-1")

    assert results[0].status == "REJECTED"
    reloaded = repo.get_candidate(results[0].candidate_id)
    assert reloaded.status == "REJECTED"


def test_end_to_end_no_trade_path_via_gate_capacity(tmp_path):
    """Samma kedja, alla sju "ok" och QA.passed=True, men
    count_open_positions() >= max_concurrent_positions -> NO_TRADE (AC4)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    settings = _settings()
    # simulera full kapacitet: seeda max_concurrent_positions öppna positioner
    for i in range(settings.risk_limits.max_concurrent_positions):
        repo._conn.execute(
            "INSERT INTO positions (position_id, candidate_id, instrument, direction, status, "
            "theoretical_entry, simulated_fill_entry, stop_loss, target, size, "
            "fill_model_version, opened_at) VALUES "
            f"('p{i}','c{i}','ETHUSDT','LONG','OPEN_POSITION','1','1','1','1','1','v1','2026-08-26')"
        )
    repo._conn.commit()
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    results = run_discovery_cycle(repo=repo, runner=runner, settings=settings, run_id="run-1")

    assert results[0].status == "NO_TRADE"
    reloaded = repo.get_candidate(results[0].candidate_id)
    assert reloaded.status == "NO_TRADE"
