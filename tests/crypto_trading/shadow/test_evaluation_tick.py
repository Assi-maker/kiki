"""Shadow evaluation tick: DB + read-only market data, writes only
shadow_evaluations (2026-09-28)."""
import json
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.shadow.evaluation import run_shadow_evaluation_tick
from crypto_trading.shadow.history import backfill_gate_evaluations
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_discovery_wiring import _persisted_candidate_in_status

DECIDED = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
NOW = DECIDED + timedelta(hours=8)


class _Market:
    """Read-only klines: flat at 55000 before the decision, then up to the
    target. Records every call; has nothing but get_klines."""

    def __init__(self):
        self.calls = []

    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        self.calls.append(symbol)
        out, t = [], start_time_ms
        while t <= end_time_ms and len(out) < limit:
            moment = datetime.fromtimestamp(t / 1000, UTC)
            price = 55000.0 if moment < DECIDED else 55000.0 + (moment - DECIDED).total_seconds() / 60 * 50
            out.append({"time": t, "open": str(price), "high": str(price + 10), "low": str(price - 10),
                        "close": str(price), "volume": "1"})
            t += 60_000
        return out


def _gate_evaluation(repo, cid="cand-1", outcome="CONFIRMED"):
    repo.record_gate_evaluation(cid, DECIDED, outcome, {
        "enforced_failed": [], "shadow": {}, "metrics": {
            "stop_loss": "53000", "target": "60000", "risk_reward": "2.5",
            "bull_probability": 0.2, "bear_probability": 0.45, "neutral_probability": 0.35,
        },
    })


def test_a_ready_candidate_gets_one_shadow_record_with_outcome_and_variants(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    _gate_evaluation(repo)
    market = _Market()

    assert run_shadow_evaluation_tick(repo, market, get_settings(), NOW) == 1
    record = repo.get_shadow_evaluation("cand-1")
    assert record["outcome"]["reason"] == "TP"
    assert record["veto_flags"]["BEARISH_DOMINANT"] is True
    assert record["veto_flags"]["ALT_LONG_WHILE_BTC_FALLING"] is False  # flat BTC
    assert set(record["variants"]) >= {"BE_1.0", "TRAIL_1.0_0.5"}
    assert record["cohort"] == "IN_SAMPLE_HISTORICAL"
    # evaluated once only
    assert run_shadow_evaluation_tick(repo, market, get_settings(), NOW) == 0


def test_a_candidate_whose_window_has_not_passed_is_left_for_later(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    _gate_evaluation(repo)
    assert run_shadow_evaluation_tick(repo, _Market(), get_settings(), DECIDED + timedelta(hours=2)) == 0
    assert repo.get_shadow_evaluation("cand-1") is None


def test_shadow_never_changes_the_candidate_or_any_trading_state(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    _gate_evaluation(repo)
    before = repo.get_candidate("cand-1").status
    run_shadow_evaluation_tick(repo, _Market(), get_settings(), NOW)
    assert repo.get_candidate("cand-1").status == before
    assert repo._conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert repo._conn.execute("SELECT COUNT(*) FROM live_executions").fetchone()[0] == 0


def test_the_shadow_package_has_no_path_to_an_order():
    import crypto_trading.shadow as pkg
    for path in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("bingx_live_trading", "bingx_demo_trading", "place_", "close_position",
                          "cancel_", "set_leverage", "transition_candidate", "claim_live"):
            assert forbidden not in text, (path.name, forbidden)


def test_history_backfill_rebuilds_metrics_but_keeps_the_historical_outcome(tmp_path):
    """For the historical replay: pre-P1 candidates had no gate_evaluations
    row. The backfill recomputes metrics/flags from the stored assessments,
    records the outcome that REALLY happened, and marks the row backfilled."""
    from crypto_trading.agents.runner import MockAgentRunner
    from crypto_trading.orchestrator import run_discovery_cycle
    from tests.crypto_trading.test_discovery_wiring import _NOW
    from tests.crypto_trading.test_orchestrator import _happy_fixtures, _settings

    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    run_discovery_cycle(repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=_settings(),
                        run_id="r", now=_NOW)
    repo._conn.execute("DELETE FROM gate_evaluations")
    repo._conn.execute("UPDATE gate_decisions SET decision = 'CONFIRMED'")
    repo._conn.commit()

    assert backfill_gate_evaluations(repo, get_settings().gate) == 1
    evaluation = repo.get_gate_evaluation("cand-1")
    assert evaluation["outcome"] == "CONFIRMED"
    assert evaluation["detail"]["backfilled"] is True
    assert evaluation["detail"]["metrics"]["risk_reward"] == "2.5"
    assert backfill_gate_evaluations(repo, get_settings().gate) == 0


def test_live_result_is_attached_when_the_candidate_was_really_traded(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "CANDIDATE")
    _gate_evaluation(repo)
    repo._conn.execute(
        "INSERT INTO live_executions (position_id, phase, entry_quantity, exchange_fill_entry, "
        "exchange_realized_pnl_usdt, realized_fees_usdt, realized_funding_usdt, exit_verification, "
        "exit_classification, claimed_at, updated_at) VALUES ('cand-1','CLOSED','0.01','55000',"
        "'20','1','0','VERIFIED','EXCHANGE_TAKE_PROFIT',?,?)",
        (DECIDED.isoformat(), DECIDED.isoformat()),
    )
    repo._conn.commit()
    run_shadow_evaluation_tick(repo, _Market(), get_settings(), NOW)
    live = repo.get_shadow_evaluation("cand-1")["live"]
    assert Decimal(live["net_usdt"]) == Decimal("19")
    assert live["classification"] == "EXCHANGE_TAKE_PROFIT"
    json.dumps(live)


def test_run_forever_backfills_history_once_then_evaluates(tmp_path, monkeypatch):
    import crypto_trading.shadow.evaluation as ev

    calls = []
    monkeypatch.setattr(ev, "backfill_gate_evaluations", lambda repo, policy: calls.append("backfill") or 0)
    monkeypatch.setattr(ev, "run_shadow_evaluation_tick",
                        lambda repo, conn, settings, now, limit=10: calls.append(("tick", limit)) or 0)

    class _Stop(Exception):
        pass

    def _sleep(_):
        raise _Stop

    monkeypatch.setattr(ev.time, "sleep", _sleep)
    import pytest as _pytest
    with _pytest.raises(_Stop):
        ev.run_forever(SQLiteRepository(tmp_path / "t.db"), _Market(), get_settings())
    assert calls == ["backfill", ("tick", 50)]


def test_one_corrupt_historical_candidate_never_stops_the_backfill(tmp_path, monkeypatch):
    from crypto_trading.storage.exceptions import CorruptCandidateStateError

    repo = SQLiteRepository(tmp_path / "t.db")
    for cid in ("bad", "good"):
        _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id=cid)
        repo.save_gate_decision(cid, "NO_TRADE", ["x"], DECIDED)
    real = repo.get_candidate

    def get_candidate(cid):
        if cid == "bad":
            raise CorruptCandidateStateError("corrupt forecast")
        return real(cid)

    monkeypatch.setattr(repo, "get_candidate", get_candidate)
    assert backfill_gate_evaluations(repo, get_settings().gate) == 1
    assert repo.get_gate_evaluation("good") is not None
    assert repo.get_gate_evaluation("bad") is None
