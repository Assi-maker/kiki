"""Realized P/L with provenance (2026-09-26 bugfix).

A position closed only by the LIVE exit mirror has no PAPER exit data, so
`compute_pnl` raised `NoneType - Decimal` and stopped the Telegram daily
report and GODFATHER strategist learning. The outcome is now PAPER (exactly
as before), the LIVE execution's verified exchange exit, or UNVERIFIABLE -
never invented, never crashing, never learned from when unverifiable."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.config.loader import get_settings
from crypto_trading.godfather.priority_boost import _priority_boost_evidence_pool
from crypto_trading.guardian.self_improvement import (
    _build_context,
    _pre_entry_veto_evidence_pool,
    _take_profit_evidence_pool,
)
from crypto_trading.notify_loop import run_notify_tick
from crypto_trading.paper_trading.execution import (
    compute_pnl,
    realized_pnl_for,
    resolve_realized_pnl,
)
from crypto_trading.paper_trading.live_execution import (
    close_time_limit_positions,
    reconcile_active_executions,
)
from crypto_trading.performance.metrics import (
    compute_drawdown,
    compute_equity_curve,
    trade_pnls,
)
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.db import init_schema
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.guardian.test_self_improvement_pre_entry_pool import (
    _evidence,
    _seed_closed_position,
)

_FEE = Decimal("0.0004")
_T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _open(repo, pid, opened_at, size=Decimal("1000"), with_candidate=True):
    if with_candidate:
        repo.create_candidate_with_event(
            Candidate(
                candidate_id=pid, idempotency_key=f"k-{pid}", instrument="BTCUSDT",
                discovery_run_id="run-0", evidence_hash=f"h-{pid}", status="CONFIRMED",
                evidence_record=_evidence("BTCUSDT", 0.9, ("momentum_breakout",), opened_at),
                created_at=opened_at, updated_at=opened_at,
            ),
            Event(
                event_id=f"CANDIDATE_CREATED:{pid}", event_type="CANDIDATE_CREATED",
                aggregate_type="candidate", aggregate_id=pid, occurred_at=opened_at,
                run_id="run-0", schema_version=1, payload={},
            ),
        )
    repo.create_position_with_event(
        Position(
            position_id=pid, candidate_id=pid, instrument="BTCUSDT", direction="LONG",
            status="OPEN_POSITION", theoretical_entry=Decimal("100"),
            simulated_fill_entry=Decimal("100"), stop_loss=Decimal("90"),
            target=Decimal("120"), size=size, fill_model_version="v1", opened_at=opened_at,
        ),
        Event(
            event_id=f"POSITION_OPENED:{pid}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=pid, occurred_at=opened_at,
            run_id="run-0", schema_version=1, payload={},
        ),
    )


def _live_only_close(
    repo, pid, closed_at, exit_price, exit_reason="stop_loss", fill_source=None,
    qty="10", entry="100", with_candidate=True, observations=(),
):
    """A position the LIVE exit mirror closed: live row CLOSED with an
    exchange exit, `positions` row CLOSED with NO paper exit data."""
    opened_at = closed_at - timedelta(hours=2)
    _open(repo, pid, opened_at, with_candidate=with_candidate)
    for index, (observed_at, unrealized) in enumerate(observations):
        repo.save_guardian_observation(
            GuardianObservation(
                observation_id=f"obs-{pid}-{index}", position_id=pid, observed_at=observed_at,
                state="HOLD", decay_score=Decimal("0.1"), progress_ratio=Decimal("0.5"),
                unrealized_pnl=Decimal(str(unrealized)),
                factors={"time_decay": 0.0, "momentum_decay": 0.0, "volume_decay": 0.0,
                         "funding_decay": 0.0, "secondary_confirmation_lost": 0.0,
                         "market_regime": 0.0},
                run_id="run-0",
            )
        )
    repo.claim_live_execution(pid, opened_at, "100", "1000", "10")
    repo.update_live_execution_submitted(
        pid, f"c-{pid}", f"e-{pid}", qty, entry, None, None, opened_at
    )
    repo.close_live_execution(pid, exit_reason, exit_price, closed_at, exit_fill_source=fill_source)
    repo.close_position_for_live_exit(pid, exit_reason, closed_at)
    return repo.get_position(pid)


# --- the resolver ---------------------------------------------------------


def test_paper_closed_is_exactly_compute_pnl(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_position(repo, "paper", _T0, Decimal("99"))
    position = repo.get_position("paper")
    realized = realized_pnl_for(repo, position, _FEE)
    assert (realized.status, realized.source) == ("VERIFIED", "PAPER")
    assert realized.pnl_usdt == compute_pnl(position)
    assert realized.paper_size_equivalent(position) == compute_pnl(position)


@pytest.mark.parametrize("fill_source", ["EXCHANGE_ORDER", "MARKET_CLOSE"])
def test_live_closed_with_verified_exchange_exit_uses_the_live_fill(tmp_path, fill_source):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _live_only_close(repo, "lv", _T0, "103", "target", fill_source)
    realized = realized_pnl_for(repo, position, _FEE)
    assert (realized.status, realized.source, realized.fees_source) == (
        "VERIFIED", "LIVE", "MODELLED"
    )
    # 10 x (103 - 100) at the LIVE size, minus 1000 x 0.0004 modelled fees
    assert realized.pnl_usdt == Decimal("30") - Decimal("0.4")
    assert realized.net_return == Decimal("29.6") / Decimal("1000")
    # paper-size units only via the real net return, never the raw LIVE USDT
    assert realized.paper_size_equivalent(position) == Decimal("29.6")


def test_live_closed_prefers_recorded_exchange_fees_and_funding(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _live_only_close(repo, "lv", _T0, "103", "target", "EXCHANGE_ORDER")
    row = repo.get_live_execution("lv") | {
        "realized_fees_usdt": "1.1", "realized_funding_usdt": "0.2",
    }
    realized = resolve_realized_pnl(position, row, _FEE)
    assert realized.fees_source == "EXCHANGE"
    assert realized.pnl_usdt == Decimal("30") - Decimal("1.1") - Decimal("0.2")


@pytest.mark.parametrize("exit_reason", ["TIME_LIMIT", "GUARDIAN_EXIT"])
def test_legacy_market_close_rows_are_verified_by_their_only_write_path(tmp_path, exit_reason):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _live_only_close(repo, "lv", _T0, "98", exit_reason, fill_source=None)
    realized = realized_pnl_for(repo, position, _FEE)
    assert (realized.status, realized.source) == ("VERIFIED", "LIVE")
    assert realized.pnl_usdt == Decimal("-20") - Decimal("0.4")


@pytest.mark.parametrize(
    ("exit_reason", "fill_source", "reason"),
    [
        ("stop_loss", None, "LIVE_EXIT_SOURCE_UNRECORDED"),
        ("target", None, "LIVE_EXIT_SOURCE_UNRECORDED"),
        ("stop_loss", "TICKER", "LIVE_EXIT_FROM_TICKER"),
    ],
)
def test_live_closed_without_a_verified_exit_is_unverifiable(
    tmp_path, exit_reason, fill_source, reason
):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _live_only_close(repo, "lv", _T0, "99", exit_reason, fill_source)
    realized = realized_pnl_for(repo, position, _FEE)
    assert (realized.status, realized.reason, realized.pnl_usdt) == ("UNVERIFIABLE", reason, None)
    assert realized.paper_size_equivalent(position) is None


def test_missing_paper_exit_and_no_live_row_is_unverifiable_not_a_crash(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open(repo, "p", _T0 - timedelta(hours=2))
    repo.close_position_for_live_exit("p", "stop_loss", _T0)
    realized = realized_pnl_for(repo, repo.get_position("p"), _FEE)
    assert (realized.status, realized.reason) == (
        "UNVERIFIABLE", "NO_PAPER_EXIT_AND_NO_LIVE_EXECUTION"
    )


def test_live_row_with_missing_fill_data_is_unverifiable(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _live_only_close(repo, "lv", _T0, "0", "TIME_LIMIT", "MARKET_CLOSE")
    realized = realized_pnl_for(repo, position, _FEE)
    assert (realized.status, realized.reason) == ("UNVERIFIABLE", "LIVE_FILL_DATA_MISSING")


# --- LIVE exits record how their price was obtained ------------------------


class _Connector:
    def __init__(self, history):
        self._history = history

    def get_position(self, symbol):
        return None  # flat

    def get_order_history(self, symbol, start_time_ms, limit=50):
        return self._history

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "101"}


class _Ticker:
    def get_ticker(self, symbol):
        return {"lastPrice": "99"}


def _active_live(repo, pid, claimed_at):
    _open(repo, pid, claimed_at, with_candidate=False)
    repo.claim_live_execution(pid, claimed_at, "100", "1000", "10")
    repo.update_live_execution_submitted(pid, "c", "e", "10", "100", None, None, claimed_at)


def test_reconcile_records_exchange_order_and_ticker_sources(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _active_live(repo, "by-order", _T0)
    stop_fill = {
        "type": "STOP_MARKET", "status": "FILLED", "side": "SELL", "positionSide": "LONG",
        "updateTime": int((_T0 + timedelta(minutes=5)).timestamp() * 1000), "avgPrice": "95",
    }
    reconcile_active_executions(
        repo, _Connector([stop_fill]), _Ticker(), "r", _T0 + timedelta(hours=1)
    )
    assert repo.get_live_execution("by-order")["exit_fill_source"] == "EXCHANGE_ORDER"

    _active_live(repo, "by-ticker", _T0)
    reconcile_active_executions(repo, _Connector([]), _Ticker(), "r", _T0 + timedelta(hours=1))
    assert repo.get_live_execution("by-ticker")["exit_fill_source"] == "TICKER"
    assert realized_pnl_for(repo, repo.get_position("by-ticker"), _FEE).status == "UNVERIFIABLE"


def test_time_limit_close_records_market_close_source(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _active_live(repo, "tl", _T0 - timedelta(hours=7))

    class _Open(_Connector):
        def get_position(self, symbol):
            return {"symbol": symbol, "positionAmt": "10"}

    close_time_limit_positions(repo, _Open([]), 6, "r", _T0)
    assert repo.get_live_execution("tl")["exit_fill_source"] == "MARKET_CLOSE"
    realized = realized_pnl_for(repo, repo.get_position("tl"), _FEE)
    assert (realized.status, realized.source) == ("VERIFIED", "LIVE")


def test_migration_adds_exit_fill_source_to_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    repo = SQLiteRepository(path)
    repo._conn.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("ALTER TABLE live_executions DROP COLUMN exit_fill_source")
    init_schema(conn)
    init_schema(conn)  # idempotent
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(live_executions)")}
    assert "exit_fill_source" in columns


# --- reporting keeps going -------------------------------------------------


def _mixed_history(repo):
    _seed_closed_position(repo, "paper-win", _T0 - timedelta(hours=5), Decimal("101"))
    _seed_closed_position(repo, "paper-loss", _T0 - timedelta(hours=4), Decimal("99"))
    _live_only_close(repo, "live-verified", _T0 - timedelta(hours=3), "97", "TIME_LIMIT", None)
    _live_only_close(repo, "live-unverifiable", _T0 - timedelta(hours=2), "99", "stop_loss", None)


def test_paper_metrics_leave_live_only_positions_out_instead_of_crashing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _mixed_history(repo)
    closed = repo.find_closed_positions()
    assert len(closed) == 4
    assert trade_pnls(closed) == [Decimal("10"), Decimal("-10")]  # PAPER only, never LIVE USDT
    assert compute_drawdown(closed) == Decimal("10")
    assert len(compute_equity_curve(closed)) == 2


def test_notify_daily_report_completes_with_an_unverifiable_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _mixed_history(repo)

    class _Notifier:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

    # CLOSED messages (already robust since 4052d09, own tests) need a
    # forecast record these fixtures do not create - this test is about the
    # daily report, which computed P/L over EVERY closed position.
    for position in repo.find_closed_positions():
        repo.record_telegram_event(f"CLOSED:{position.position_id}", "CLOSED", _T0)
        repo.record_telegram_event(f"CONFIRMED:{position.candidate_id}", "CONFIRMED", _T0)
    notifier = _Notifier()
    run_notify_tick(notifier, repo, get_settings())
    status = repo._conn.execute(
        "SELECT status FROM runs WHERE run_type = 'notify' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()["status"]
    assert status == "ok"
    assert any("PnL" in message or "pnl" in message.lower() for message in notifier.sent)


# --- learning never uses an unverifiable outcome ---------------------------


def test_learning_pools_skip_unverifiable_and_use_verified_live(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _mixed_history(repo)
    veto = _pre_entry_veto_evidence_pool(repo)
    boost = _priority_boost_evidence_pool(repo)
    # paper-win, paper-loss, live-verified - never live-unverifiable
    assert len(veto) == len(boost) == 3
    assert sorted(row[2] for row in veto) == [False, True, True]  # veto correct on the 2 losses
    assert sorted(row[2] for row in boost) == [False, False, True]


def test_take_profit_pool_compares_live_outcomes_in_paper_units(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    # LIVE: 10 x (97 - 100) - 0.4 = -30.4 at 1000 notional -> -3.04% -> -30.4 at paper size 1000
    _live_only_close(
        repo, "live-verified", _T0, "97", "TIME_LIMIT", None,
        observations=[(_T0 - timedelta(hours=1), "-31"), (_T0 - timedelta(minutes=30), "-30")],
    )
    _live_only_close(
        repo, "live-unverifiable", _T0, "99", "stop_loss", None,
        observations=[(_T0 - timedelta(hours=1), "5")],
    )
    pool = _take_profit_evidence_pool(repo)
    assert [row[2] for row in sorted(pool)] == [False, True]  # -31 < -30.4 < -30


def test_strategist_context_builds_and_marks_the_pnl_source(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _mixed_history(repo)
    context = _build_context(repo, get_settings(), "run-x")
    rows = context["closed_position_entry_outcomes"]
    assert len(rows) == 3
    assert sorted(row["pnl_source"] for row in rows) == ["LIVE", "PAPER", "PAPER"]
    assert all(row["pnl_usdt"] != "None" for row in rows)
