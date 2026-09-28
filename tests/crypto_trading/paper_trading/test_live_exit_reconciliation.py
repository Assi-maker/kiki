"""LIVE exit reconciliation (2026-09-28).

The 2026-09-26..28 forensic found 4 of 34 LIVE exits priced from the ticker
(UNVERIFIABLE) although the exchange's own order history had the real fill,
3 of them with a wrong exit reason or price in the DB (a PP stop recorded as
'target', two manual/external MARKET closes recorded as 'stop_loss'), and no
real fees or funding stored at all. This module re-derives every CLOSED LIVE
exit from the exchange's order history and income ledger - read-only - and
records VERIFIED or UNVERIFIABLE, never a guess.

Order dicts below are copied from real BingX allOrders responses."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.execution import realized_pnl_for
from crypto_trading.paper_trading.live_exit_reconciliation import (
    reconcile_exit,
    verify_closed_live_exits,
)
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.paper_trading.test_live_execution import _open_position

PID = "f8009c7abf9dec25d39603de9275c58bd883f7bd780f6de8a60b4df1a0a207ec"
LV = "lv" + PID[:24]
T_ENTRY = 1790443634000  # 2026-09-26T17:27:14Z
T_EXIT = 1790446600000  # 2026-09-26T18:16:40Z


def _ms(ms):
    return datetime.fromtimestamp(ms / 1000, UTC)


def _order(type_, side, status, avg, qty, t, client="", commission="0", profit="0", stop=""):
    return {
        "type": type_,
        "side": side,
        "positionSide": "LONG",
        "status": status,
        "avgPrice": avg,
        "executedQty": qty,
        "time": t,
        "updateTime": t,
        "clientOrderId": client,
        "orderId": t + len(client) + len(type_),
        "commission": commission,
        "profit": profit,
        "stopPrice": stop,
    }


def _entry(qty="20433", avg="0.04901", t=T_ENTRY):
    return _order("MARKET", "BUY", "FILLED", avg, qty, t, client=LV + "e", commission="-0.500669")


def _kas_orders():
    return [
        _entry(),
        _order("TAKE_PROFIT_MARKET", "SELL", "CANCELLED", "0.00000", "0", T_ENTRY, stop="0.05100"),
        _order("STOP_MARKET", "SELL", "CANCELLED", "0.00000", "0", T_ENTRY, stop="0.04650"),
        _order(
            "STOP_MARKET",
            "SELL",
            "FILLED",
            "0.04873",
            "20433",
            T_EXIT,
            client=LV + "pp",
            commission="-0.497866",
            profit="-5.6043",
            stop="0.04900",
        ),
    ]


def test_a_profit_protection_stop_is_verified_with_real_price_fees_and_pnl():
    result = reconcile_exit(PID, Decimal("20433"), _kas_orders(), [])
    assert result.verification == "VERIFIED"
    assert result.classification == "PROFIT_PROTECTION_STOP"
    assert result.exit_price == Decimal("0.04873")
    assert result.entry_filled_at == _ms(T_ENTRY)
    assert result.exit_filled_at == _ms(T_EXIT)
    assert result.fees_usdt == Decimal("0.998535")  # cost, positive
    assert result.funding_usdt == Decimal("0")
    assert result.exchange_realized_pnl_usdt == Decimal("-5.6043")


@pytest.mark.parametrize(
    "type_,client,expected",
    [
        ("STOP_MARKET", "", "EXCHANGE_STOP_LOSS"),
        ("TAKE_PROFIT_MARKET", "", "EXCHANGE_TAKE_PROFIT"),
        ("MARKET", LV + "x", "BOT_TIME_LIMIT_CLOSE"),
        ("MARKET", LV + "g", "BOT_GUARDIAN_CLOSE"),
        ("MARKET", "", "EXTERNAL_CLOSE"),
        ("MARKET", "someone-else", "EXTERNAL_CLOSE"),
        ("LIQUIDATION", "", "OTHER:LIQUIDATION"),
    ],
)
def test_exit_orders_are_classified_from_type_and_our_own_client_id(type_, client, expected):
    orders = [_entry(), _order(type_, "SELL", "FILLED", "0.0500", "20433", T_EXIT, client=client)]
    result = reconcile_exit(PID, Decimal("20433"), orders, [])
    assert result.verification == "VERIFIED"
    assert result.classification == expected


def test_no_entry_fill_is_unverifiable():
    orders = [_order("STOP_MARKET", "SELL", "FILLED", "0.0487", "20433", T_EXIT)]
    result = reconcile_exit(PID, Decimal("20433"), orders, [])
    assert result.verification == "UNVERIFIABLE"
    assert result.reason == "ENTRY_FILL_NOT_FOUND"
    assert result.exit_price is None


def test_no_exit_fill_yet_is_unverifiable():
    result = reconcile_exit(PID, Decimal("20433"), [_entry()], [])
    assert result.verification == "UNVERIFIABLE"
    assert result.reason == "EXIT_FILL_NOT_FOUND"


def test_partial_exit_fills_are_combined_at_their_weighted_price():
    orders = [
        _entry(qty="100"),
        _order(
            "MARKET", "SELL", "FILLED", "0.0500", "40", T_EXIT, client=LV + "x", commission="-0.1"
        ),
        _order(
            "MARKET",
            "SELL",
            "FILLED",
            "0.0510",
            "60",
            T_EXIT + 1000,
            client=LV + "x",
            commission="-0.2",
        ),
    ]
    result = reconcile_exit(PID, Decimal("100"), orders, [])
    assert result.verification == "VERIFIED"
    assert result.exit_price == Decimal("0.0506")
    assert result.exit_filled_at == _ms(T_EXIT + 1000)
    assert result.classification == "BOT_TIME_LIMIT_CLOSE"


def test_mixed_exit_orders_are_marked_mixed_not_guessed():
    orders = [
        _entry(qty="100"),
        _order("STOP_MARKET", "SELL", "FILLED", "0.0490", "40", T_EXIT),
        _order("MARKET", "SELL", "FILLED", "0.0480", "60", T_EXIT + 1000),
    ]
    result = reconcile_exit(PID, Decimal("100"), orders, [])
    assert result.classification == "MIXED:EXCHANGE_STOP_LOSS+EXTERNAL_CLOSE"


def test_an_exit_quantity_that_does_not_match_the_entry_is_unverifiable():
    orders = [_entry(qty="100"), _order("MARKET", "SELL", "FILLED", "0.05", "150", T_EXIT)]
    result = reconcile_exit(PID, Decimal("100"), orders, [])
    assert result.verification == "UNVERIFIABLE"
    assert result.reason == "EXIT_QTY_MISMATCH"


def test_a_later_trade_on_the_same_symbol_is_never_mixed_in():
    later = T_EXIT + 3_600_000
    orders = _kas_orders() + [
        _order(
            "MARKET",
            "BUY",
            "FILLED",
            "0.0495",
            "20000",
            later,
            client="lvOTHERPOSITION000000000000e",
        ),
        _order("STOP_MARKET", "SELL", "FILLED", "0.0480", "20000", later + 60_000),
    ]
    result = reconcile_exit(PID, Decimal("20433"), orders, [])
    assert result.exit_price == Decimal("0.04873")
    assert result.exit_filled_at == _ms(T_EXIT)


def test_only_funding_inside_the_holding_window_counts_as_a_cost():
    funding = [
        {
            "symbol": "KAS-USDT",
            "incomeType": "FUNDING_FEE",
            "income": "0.25",
            "time": T_ENTRY + 60_000,
        },
        {
            "symbol": "KAS-USDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.05",
            "time": T_ENTRY + 120_000,
        },
        {
            "symbol": "KAS-USDT",
            "incomeType": "FUNDING_FEE",
            "income": "9.99",
            "time": T_EXIT + 60_000,
        },
        {
            "symbol": "KAS-USDT",
            "incomeType": "FUNDING_FEE",
            "income": "9.99",
            "time": T_ENTRY - 60_000,
        },
    ]
    result = reconcile_exit(PID, Decimal("20433"), _kas_orders(), funding)
    assert result.funding_usdt == Decimal("-0.20")  # received 0.20 net -> negative cost


# ---------------------------------------------------------------------------
# verify_closed_live_exits: DB + read-only connector
# ---------------------------------------------------------------------------

_CLAIMED = _ms(T_ENTRY) - timedelta(seconds=20)


class _ReadOnlyConnector:
    """Has ONLY the two read-only calls. Any other attribute - every order-
    placing, cancelling or closing method - raises, so a test would fail if
    reconciliation ever reached for one."""

    def __init__(self, orders=None, income=None, raises=None):
        self._orders = orders if orders is not None else _kas_orders()
        self._income = income or []
        self._raises = raises
        self.calls = []

    def get_order_history(self, symbol, start_time_ms, limit=50, end_time_ms=None):
        self.calls.append(("get_order_history", symbol, start_time_ms))
        if self._raises:
            raise self._raises
        return self._orders

    def get_income(self, symbol, income_type, start_time_ms, end_time_ms, limit=1000):
        self.calls.append(("get_income", symbol, income_type))
        if self._raises:
            raise self._raises
        return self._income

    def __getattr__(self, name):
        raise AssertionError(f"reconciliation must never call {name}")


def _closed_live(
    repo, fill_source="TICKER", exit_reason="target", exit_price="0.04890", closed_at=None
):
    _open_position(repo, position_id=PID, opened_at=_CLAIMED)
    repo.claim_live_execution_if_symbol_free(PID, _CLAIMED, "100", "1000", "10")
    repo.update_live_execution_submitted(
        PID,
        entry_client_order_id=LV + "e",
        entry_exchange_order_id=LV + "e",
        entry_quantity="20433",
        exchange_fill_entry="0.04901",
        sl_exchange_order_id=None,
        tp_exchange_order_id=None,
        updated_at=_CLAIMED,
    )
    repo.close_live_execution(
        PID, exit_reason, exit_price, closed_at or _ms(T_EXIT), exit_fill_source=fill_source
    )
    repo.close_position_for_live_exit(PID, exit_reason, closed_at or _ms(T_EXIT))


def test_a_ticker_priced_exit_becomes_verified_from_exchange_evidence(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    before = realized_pnl_for(repo, repo.get_position(PID))
    assert before.status == "UNVERIFIABLE"

    verify_closed_live_exits(repo, _ReadOnlyConnector(), "r1", _ms(T_EXIT) + timedelta(minutes=5))

    row = repo.get_live_execution(PID)
    assert row["exit_verification"] == "VERIFIED"
    assert row["exit_classification"] == "PROFIT_PROTECTION_STOP"
    assert row["exchange_fill_exit"] == "0.04873"
    assert row["exit_fill_source"] == "EXCHANGE_ORDER"
    assert row["exit_reason"] == "target"  # the bot's own record is kept, never rewritten
    assert Decimal(row["realized_fees_usdt"]) == Decimal("0.998535")
    assert Decimal(row["realized_funding_usdt"]) == Decimal("0")
    assert Decimal(row["exchange_realized_pnl_usdt"]) == Decimal("-5.6043")
    assert row["entry_filled_at"] == _ms(T_ENTRY).isoformat()
    assert row["exit_filled_at"] == _ms(T_EXIT).isoformat()
    after = realized_pnl_for(repo, repo.get_position(PID))
    assert after.status == "VERIFIED" and after.fees_source == "EXCHANGE"
    expected = (Decimal("0.04873") - Decimal("0.04901")) * 20433 - Decimal("0.998535")
    assert after.pnl_usdt == expected


def test_an_exit_not_yet_in_history_is_retried_not_persisted(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    connector = _ReadOnlyConnector(orders=[_entry()])
    verify_closed_live_exits(repo, connector, "r1", _ms(T_EXIT) + timedelta(minutes=5))
    assert repo.get_live_execution(PID)["exit_verification"] is None  # try again next tick


def test_after_the_grace_period_an_unprovable_exit_is_recorded_unverifiable(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo, fill_source="MARKET_CLOSE", exit_reason="TIME_LIMIT")
    connector = _ReadOnlyConnector(orders=[_entry()])
    verify_closed_live_exits(repo, connector, "r1", _ms(T_EXIT) + timedelta(days=3))
    row = repo.get_live_execution(PID)
    assert row["exit_verification"] == "UNVERIFIABLE"
    assert row["exit_verification_reason"] == "EXIT_FILL_NOT_FOUND"
    assert row["exchange_fill_exit"] == "0.04890"  # untouched without evidence
    # Evidence contradicts the recorded fill: never learned from.
    assert realized_pnl_for(repo, repo.get_position(PID)).status == "UNVERIFIABLE"


def test_an_exchange_error_changes_nothing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    connector = _ReadOnlyConnector(raises=ConnectorUnavailableError("down"))
    verify_closed_live_exits(repo, connector, "r1", _ms(T_EXIT) + timedelta(days=3))
    row = repo.get_live_execution(PID)
    assert row["exit_verification"] is None
    assert row["exchange_fill_exit"] == "0.04890"


def test_a_verified_row_is_never_reprocessed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    verify_closed_live_exits(repo, _ReadOnlyConnector(), "r1", _ms(T_EXIT) + timedelta(minutes=5))
    connector = _ReadOnlyConnector()
    verify_closed_live_exits(repo, connector, "r2", _ms(T_EXIT) + timedelta(minutes=6))
    assert connector.calls == []


def test_active_positions_are_never_touched(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, position_id=PID, opened_at=_CLAIMED)
    repo.claim_live_execution_if_symbol_free(PID, _CLAIMED, "100", "1000", "10")
    repo.update_live_execution_submitted(
        PID,
        entry_client_order_id=LV + "e",
        entry_exchange_order_id=LV + "e",
        entry_quantity="20433",
        exchange_fill_entry="0.04901",
        sl_exchange_order_id=None,
        tp_exchange_order_id=None,
        updated_at=_CLAIMED,
    )
    connector = _ReadOnlyConnector()
    verify_closed_live_exits(repo, connector, "r1", _ms(T_EXIT))
    assert connector.calls == []
    assert repo.get_live_execution(PID)["phase"] == "ACTIVE"


def test_an_external_close_is_verified_and_labelled_as_such(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo, exit_reason="stop_loss")
    orders = [
        _entry(),
        _order(
            "MARKET",
            "SELL",
            "FILLED",
            "0.04950",
            "20433",
            T_EXIT,
            commission="-0.5",
            profit="10.01",
        ),
    ]
    verify_closed_live_exits(
        repo, _ReadOnlyConnector(orders=orders), "r1", _ms(T_EXIT) + timedelta(minutes=5)
    )
    row = repo.get_live_execution(PID)
    assert row["exit_classification"] == "EXTERNAL_CLOSE"
    assert row["exit_reason"] == "stop_loss"  # kept; the verified label sits next to it


class _WindowRecordingConnector(_ReadOnlyConnector):
    def get_order_history(self, symbol, start_time_ms, limit=50, end_time_ms=None):
        self.calls.append(("orders", start_time_ms, end_time_ms))
        return self._orders

    def get_income(self, symbol, income_type, start_time_ms, end_time_ms, limit=1000):
        self.calls.append(("income", start_time_ms, end_time_ms))
        return self._income


def test_exchange_queries_never_span_more_than_seven_days(tmp_path):
    """Found by the read-only dry run 2026-09-28: every row older than a week
    failed with BingX 109400 because the query ran from the claim to now."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    connector = _WindowRecordingConnector()
    verify_closed_live_exits(repo, connector, "r1", _ms(T_EXIT) + timedelta(days=30))
    assert len(connector.calls) == 2
    for _, start, end in connector.calls:
        assert end is not None and end > start
        assert end - start <= 7 * 24 * 3600 * 1000
        assert end >= T_EXIT  # the window still covers the exit


def test_history_that_stays_unreadable_long_after_the_close_is_recorded_unverifiable(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    connector = _ReadOnlyConnector(raises=ConnectorUnavailableError("109400 range"))
    verify_closed_live_exits(repo, connector, "r1", _ms(T_EXIT) + timedelta(days=8))
    row = repo.get_live_execution(PID)
    assert row["exit_verification"] == "UNVERIFIABLE"
    assert row["exit_verification_reason"] == "EXCHANGE_HISTORY_UNAVAILABLE"


def test_a_close_carrying_another_positions_client_id_is_never_taken_as_ours():
    other = "lvOTHERPOSITION0000000000"
    orders = [
        _entry(),
        _order("MARKET", "SELL", "FILLED", "0.0600", "20433", T_EXIT - 1000, client=other + "x"),
        _order("STOP_MARKET", "SELL", "FILLED", "0.04873", "20433", T_EXIT, client=LV + "pp"),
    ]
    result = reconcile_exit(PID, Decimal("20433"), orders, [])
    assert result.exit_price == Decimal("0.04873")
    assert result.classification == "PROFIT_PROTECTION_STOP"


def test_overlapping_positions_on_the_same_symbol_are_unverifiable(tmp_path):
    """2026-09-12 (two bots running) left two LIVE positions on one symbol at
    once; the exchange merges them, so an exit cannot be attributed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _closed_live(repo)
    other = "a" * 64
    _open_position(repo, position_id=other, opened_at=_CLAIMED)
    repo._conn.execute(
        "INSERT INTO live_executions (position_id, phase, entry_quantity, claimed_at, updated_at, "
        "closed_at) VALUES (?, 'CLOSED', '20433', ?, ?, ?)",
        (
            other,
            (_CLAIMED + timedelta(minutes=5)).isoformat(),
            _CLAIMED.isoformat(),
            (_ms(T_EXIT) - timedelta(minutes=5)).isoformat(),
        ),
    )
    repo._conn.commit()
    verify_closed_live_exits(repo, _ReadOnlyConnector(), "r1", _ms(T_EXIT) + timedelta(days=1))
    row = repo.get_live_execution(PID)
    assert row["exit_verification"] == "UNVERIFIABLE"
    assert row["exit_verification_reason"] == "OVERLAPPING_SAME_SYMBOL_POSITION"
