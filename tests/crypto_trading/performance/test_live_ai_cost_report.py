import sqlite3
from datetime import UTC, datetime, timedelta

from crypto_trading.performance.live_ai_cost_report import build_live_ai_cost_report
from crypto_trading.schemas.event import Event
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_discovery_live_budget import _stale_candidate

_T0 = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


def _ai_call(repo, candidate_id, role, cost, at=_T0):
    repo.record_ai_call_event(
        Event(
            event_id=f"AI_CALL_MADE:{candidate_id}:{role}:r",
            event_type="AI_CALL_MADE",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=at,
            run_id="r",
            schema_version=1,
            payload={"role": role, "status": "ok", "cost_usd": str(cost)},
        )
    )


def _transition(repo, candidate_id, to, reason=None, at=_T0):
    payload = {"from": "UNDER_AI_ANALYSIS", "to": to}
    if reason:
        payload["reason"] = reason
    repo.record_event(
        Event(
            event_id=f"CANDIDATE_TRANSITIONED:{candidate_id}:{to}",
            event_type="CANDIDATE_TRANSITIONED",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=at,
            run_id="r",
            schema_version=1,
            payload=payload,
        )
    )


def _gate(repo, n, outcome, at=_T0):
    repo.record_event(
        Event(
            event_id=f"DISCOVERY_LIVE_GATE:run-{n}",
            event_type="DISCOVERY_LIVE_GATE",
            aggregate_type="discovery_run",
            aggregate_id=f"run-{n}",
            occurred_at=at,
            run_id=f"run-{n}",
            schema_version=1,
            payload={"outcome": outcome},
        )
    )


def test_report_splits_ai_cost_between_live_no_signal_and_confirmed_never_live(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    live = _stale_candidate(repo, timedelta(0), "run-a")  # analysed -> CONFIRMED -> real LIVE order
    never_live = _stale_candidate(repo, timedelta(0), "run-b")  # CONFIRMED but LIVE never opened it
    verdict_no = _stale_candidate(repo, timedelta(0), "run-c")  # NO_TRADE
    for cand, status in ((live, "CONFIRMED"), (never_live, "CONFIRMED"), (verdict_no, "NO_TRADE")):
        repo._conn.execute(
            "UPDATE candidates SET status = ? WHERE candidate_id = ?", (status, cand.candidate_id)
        )
        _ai_call(repo, cand.candidate_id, "risk", "0.10")
        _ai_call(repo, cand.candidate_id, "qa", "0.05")
    _transition(repo, live.candidate_id, "CONFIRMED")
    _transition(repo, never_live.candidate_id, "CONFIRMED")
    # Guardian spend is not discovery spend and must be excluded.
    _ai_call(repo, "some-position", "guardian", "9.99")
    repo._conn.execute(
        "INSERT INTO live_executions (position_id, phase, entry_client_order_id, "
        "entry_exchange_order_id, claimed_at, updated_at) VALUES (?, 'ACTIVE', 'c', 'ex', ?, ?)",
        (live.candidate_id, _T0.isoformat(), _T0.isoformat()),
    )
    repo._conn.commit()

    report = build_live_ai_cost_report(sqlite3.connect(tmp_path / "t.db"), _T0 - timedelta(hours=1))

    assert report["discovery_ai_calls"] == 6
    assert report["discovery_ai_cost_usd"] == "0.4500"
    assert report["full_analyses"] == 3
    assert report["confirmed"] == 2
    assert report["live_orders"] == 1
    assert report["ai_cost_per_live_position_usd"] == "0.4500"
    assert report["ai_cost_usd_on_candidates_that_became_live"] == "0.1500"
    assert report["ai_cost_usd_confirmed_never_live"] == "0.1500"
    assert report["ai_cost_usd_no_signal"] == "0.1500"


def test_report_counts_gate_outcomes_blocked_share_and_avoided_analyses(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i, outcome in enumerate(
        ["suppressed_capacity", "suppressed_capacity", "suppressed_capital", "capped", "normal"]
    ):
        _gate(repo, i, outcome)
    _transition(repo, "c1", "BUDGET_LIMITED", "live_slot_budget")
    _transition(repo, "c2", "BUDGET_LIMITED", "live_slot_budget")
    _transition(repo, "c3", "BUDGET_LIMITED", "stale_signal")
    _transition(repo, "c4", "BUDGET_LIMITED")  # ordinary over-budget, not attributed to the gate

    report = build_live_ai_cost_report(sqlite3.connect(tmp_path / "t.db"), _T0 - timedelta(hours=1))

    assert report["gate_outcomes"]["suppressed_capacity"] == 2
    assert report["gate_outcomes"]["suppressed_capital"] == 1
    assert report["gate_blocked_share"] == 0.6
    assert report["avoided_analyses"] == {"live_slot_budget": 2, "stale_signal": 1}


def test_report_respects_the_time_window_and_never_divides_by_zero(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _ai_call(repo, "old", "risk", "1.00", at=_T0 - timedelta(days=3))

    report = build_live_ai_cost_report(sqlite3.connect(tmp_path / "t.db"), _T0 - timedelta(hours=1))

    assert report["discovery_ai_calls"] == 0
    assert report["ai_cost_per_live_position_usd"] is None
    assert report["gate_blocked_share"] is None
