"""Funnel observability (2026-09-29): every discovery cycle records one
DISCOVERY_FUNNEL event, and funnel_report reconstructs where each candidate
stopped - AI, Gate, Risk (Safety Kernel) or execution. No extra AI calls."""
import json
from datetime import UTC, datetime, timedelta

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.paper_trading.replay import run_single_cycle
from crypto_trading.performance import funnel_report
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.paper_trading.test_replay import _build_snapshots, _happy_fixtures, _settings

SINCE = datetime(2000, 1, 1, tzinfo=UTC)
UNTIL = datetime(2100, 1, 1, tzinfo=UTC)


def _funnel_events(repo):
    return [json.loads(r["payload"]) for r in repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'DISCOVERY_FUNNEL'")]


def test_one_cycle_records_one_complete_funnel_event(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    snapshot = _build_snapshots()[1]
    run_single_cycle(snapshot, repo, MockAgentRunner(fixtures=_happy_fixtures()), _settings(), run_id="run-1")

    events = _funnel_events(repo)
    assert len(events) == 1
    f = events[0]
    assert f["markets_scanned"] == len(snapshot.instruments)
    assert f["eligible"] >= f["top_n"] >= f["quant_shortlist"] >= f["candidates_created"] == 1
    for key in ("skipped_rejected_cooldown", "skipped_kernel_reject_cooldown", "data_invalid",
                "budget_limited", "ai_started", "ai_all_roles_ok", "ai_failed", "gate",
                "confirmed", "paper_positions_opened"):
        assert key in f
    assert f["ai_started"] == 1 and f["ai_all_roles_ok"] == 1 and f["ai_failed"] == 0
    assert f["gate"] == {"CONFIRMED": {"all_checks_passed": 1}}
    assert f["confirmed"] == 1 and f["paper_positions_opened"] == 1


def test_a_cycle_with_failing_ai_counts_the_failures(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures(), fail_agents={"crypto-forecast-agent"})
    run_single_cycle(_build_snapshots()[1], repo, runner, _settings(), run_id="run-1")
    f = _funnel_events(repo)[0]
    assert f["ai_started"] == 1 and f["ai_failed"] == 1 and f["confirmed"] == 0
    assert f["gate"]["NO_TRADE"] == {"missing_or_failed_assessment:forecast": 1}


def _one_confirmed_with_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    positions = run_single_cycle(_build_snapshots()[1], repo, MockAgentRunner(fixtures=_happy_fixtures()),
                                 _settings(), run_id="run-1")
    return repo, positions[0]


def test_the_path_stops_at_the_risk_engine_when_the_kernel_rejects(tmp_path):
    repo, position = _one_confirmed_with_position(tmp_path)
    repo.record_safety_kernel_decision(position.position_id, position.opened_at + timedelta(minutes=1),
                                       "REJECT", {"action": "REJECT", "reasons": ["GROUP_RISK_CAP"]})
    paths = funnel_report.candidate_paths(repo._conn, SINCE, UNTIL)
    assert len(paths) == 1
    p = paths[0]
    assert p["ai"] == "ALL_OK" and p["gate"] == "CONFIRMED"
    assert p["risk"] == "REJECT:GROUP_RISK_CAP"
    assert p["stopped_at"] == "RISK_REJECTED"
    assert p["execution"] is None


def test_the_path_reaches_a_closed_live_trade(tmp_path):
    repo, position = _one_confirmed_with_position(tmp_path)
    repo.record_safety_kernel_decision(position.position_id, position.opened_at, "APPROVE",
                                       {"action": "APPROVE", "reasons": []})
    repo.claim_live_execution_if_symbol_free(position.position_id, position.opened_at, "100", "1000", "10")
    repo._conn.execute("UPDATE live_executions SET phase='CLOSED', exit_reason='stop_loss' WHERE position_id=?",
                       (position.position_id,))
    repo._conn.commit()
    p = funnel_report.candidate_paths(repo._conn, SINCE, UNTIL)[0]
    assert p["risk"] == "APPROVE"
    assert p["execution"] == "CLOSED:stop_loss"
    assert p["stopped_at"] == "LIVE_CLOSED"


def test_a_confirmed_position_never_seen_by_the_live_loop_is_reported_as_such(tmp_path):
    repo, _ = _one_confirmed_with_position(tmp_path)
    p = funnel_report.candidate_paths(repo._conn, SINCE, UNTIL)[0]
    assert p["stopped_at"] == "NOT_EVALUATED_BY_LIVE"


def test_the_summary_answers_where_every_candidate_went(tmp_path):
    repo, position = _one_confirmed_with_position(tmp_path)
    repo.record_safety_kernel_decision(position.position_id, position.opened_at, "REJECT",
                                       {"action": "REJECT", "reasons": ["PORTFOLIO_RISK_CAP", "GROUP_RISK_CAP"]})
    s = funnel_report.summarize(repo._conn, SINCE, UNTIL)
    assert s["cycles"] == 1
    assert s["totals"]["candidates_created"] == 1
    assert s["stopped_at"] == {"RISK_REJECTED": 1}
    assert s["risk_rejections"] == {"PORTFOLIO_RISK_CAP+GROUP_RISK_CAP": 1}


def test_a_kernel_reject_cooldown_skip_is_visible_in_the_cycle_funnel(tmp_path):
    from tests.crypto_trading.screening.test_kernel_reject_cooldown import _confirmed_and_kernel_rejected

    repo = SQLiteRepository(tmp_path / "t.db")
    snapshot = _build_snapshots()[1]
    _confirmed_and_kernel_rejected(repo, "BTCUSDT", ["GROUP_RISK_CAP"],
                                   snapshot.simulated_now - timedelta(minutes=10))
    run_single_cycle(snapshot, repo, MockAgentRunner(fixtures=_happy_fixtures()), _settings(), run_id="run-1")
    f = _funnel_events(repo)[0]
    assert f["skipped_kernel_reject_cooldown"] == 1
    assert f["skipped_symbols"]["kernel_reject_cooldown"] == ["BTCUSDT"]
    assert f["candidates_created"] == 0 and f["ai_started"] == 0
