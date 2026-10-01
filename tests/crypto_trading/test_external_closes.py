"""P1 (2026-10-01): closes the bot did not make are logged explicitly and kept
out of the bot's own exit performance."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.external_closes import backfill, exit_source, is_external
from crypto_trading.paper_trading.live_exit_reconciliation import verify_closed_live_exits
from crypto_trading.performance.live_track_report import build_live_report
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.paper_trading.test_live_exit_reconciliation import (
    PID,
    T_EXIT,
    _closed_live,
    _entry,
    _kas_orders,
    _ms,
    _order,
    _ReadOnlyConnector,
)

_LATER = _ms(T_EXIT) + timedelta(minutes=5)


def _manual_orders():
    return [
        _entry(),
        _order("MARKET", "SELL", "FILLED", "0.04950", "20433", T_EXIT,
               commission="-0.5", profit="10.01"),
    ]


@pytest.mark.parametrize("classification,source", [
    ("BOT_TIME_LIMIT_CLOSE", "BOT"),
    ("BOT_GUARDIAN_CLOSE", "BOT"),
    ("EXCHANGE_STOP_LOSS", "EXCHANGE_PROTECTIVE_ORDER"),
    ("EXCHANGE_TAKE_PROFIT", "EXCHANGE_PROTECTIVE_ORDER"),
    ("PROFIT_PROTECTION_STOP", "EXCHANGE_PROTECTIVE_ORDER"),
    ("EXTERNAL_CLOSE", "EXTERNAL_MANUAL"),
    ("OTHER:LIQUIDATION", "EXCHANGE_FORCED"),
    ("MIXED:EXCHANGE_STOP_LOSS+EXTERNAL_CLOSE", "MIXED_INCLUDES_EXTERNAL"),
    ("MIXED:EXCHANGE_STOP_LOSS+EXCHANGE_TAKE_PROFIT", "EXCHANGE_PROTECTIVE_ORDER"),
    (None, "UNKNOWN"),
])
def test_exit_source_comes_from_the_exchange_classification(classification, source):
    assert exit_source(classification) == source


def test_only_non_bot_sources_count_as_external():
    assert not is_external("BOT")
    assert not is_external("EXCHANGE_PROTECTIVE_ORDER")
    assert not is_external("UNKNOWN")  # unverified: never claimed as manual
    assert is_external("EXTERNAL_MANUAL") and is_external("EXCHANGE_FORCED")


def test_a_verified_manual_close_is_logged_with_every_field(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo, exit_reason="stop_loss")
    verify_closed_live_exits(repo, _ReadOnlyConnector(orders=_manual_orders()), "r1", _LATER)
    row = repo.get_external_close(PID)
    assert row is not None
    assert row["symbol"] == repo.get_position(PID).instrument
    assert row["direction"] == "LONG"
    assert row["entry_price"] == "0.04901"
    assert row["close_price"] == "0.04950"
    assert row["source"] == "EXTERNAL_MANUAL"
    assert row["classification"] == "EXTERNAL_CLOSE"
    assert row["closed_at"] == _ms(T_EXIT).isoformat()
    assert row["detected_at"] == _LATER.isoformat()
    fees = Decimal(repo.get_live_execution(PID)["realized_fees_usdt"])
    assert Decimal(row["pnl_usdt"]) == Decimal("10.01") - fees
    assert row["reason"] is None


def test_the_bots_own_protective_stop_is_not_logged_as_external(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    verify_closed_live_exits(repo, _ReadOnlyConnector(orders=_kas_orders()), "r1", _LATER)
    assert repo.get_live_execution(PID)["exit_classification"] == "PROFIT_PROTECTION_STOP"
    assert repo.get_external_close(PID) is None


def test_a_reason_can_be_noted_and_is_never_overwritten_by_a_rerun(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo, exit_reason="stop_loss")
    verify_closed_live_exits(repo, _ReadOnlyConnector(orders=_manual_orders()), "r1", _LATER)
    noted = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert repo.set_external_close_reason(PID, "took profit by hand", noted) is True
    assert backfill(repo, noted + timedelta(hours=1)) == 1  # idempotent re-record
    row = repo.get_external_close(PID)
    assert row["reason"] == "took profit by hand"
    assert row["detected_at"] == _LATER.isoformat()
    assert repo.set_external_close_reason("unknown-position", "x", noted) is False


def test_backfill_records_already_verified_history(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo, exit_reason="stop_loss")
    verify_closed_live_exits(repo, _ReadOnlyConnector(orders=_manual_orders()), "r1", _LATER)
    repo._conn.execute("DELETE FROM external_closes")
    repo._conn.commit()
    assert backfill(repo, _LATER) == 1
    assert repo.list_external_closes()[0]["position_id"] == PID


def test_live_report_keeps_manual_closes_out_of_bot_performance(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo, exit_reason="stop_loss")
    verify_closed_live_exits(repo, _ReadOnlyConnector(orders=_manual_orders()), "r1", _LATER)
    report = build_live_report(repo)
    assert report["external_closed"] == 1 and report["bot_managed_closed"] == 0
    assert Decimal(report["bot_managed_pnl_usdt"]) == 0
    assert Decimal(report["external_close_pnl_usdt"]) == Decimal(report["total_live_pnl_usdt"])
    assert report["live_positions"][0]["exit_source"] == "EXTERNAL_MANUAL"


def test_external_close_logging_never_touches_an_order(tmp_path):
    # _ReadOnlyConnector raises on any non-read attribute.
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo, exit_reason="stop_loss")
    connector = _ReadOnlyConnector(orders=_manual_orders())
    verify_closed_live_exits(repo, connector, "r1", _LATER)
    assert {c[0] for c in connector.calls} <= {"get_order_history", "get_income"}
