from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.live_execution import (
    close_guardian_exit_positions,
    close_time_limit_positions,
    has_sufficient_live_capacity,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _open_position(repo, position_id="pos-1", opened_at=_NOW, entry=Decimal("50000")) -> Position:
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=entry,
        simulated_fill_entry=entry, stop_loss=Decimal("49000"), target=Decimal("52000"),
        size=Decimal("1000"), fill_model_version="v1", opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    return position


class _SpyConnector:
    def __init__(self, balance="123.45", all_positions=None, order_status="FILLED"):
        self.calls = []
        self._balance = balance
        self._all_positions = all_positions if all_positions is not None else []
        self._order_status = order_status
        self.leverage_calls = []

    def set_leverage(self, symbol, leverage=10, side="LONG"):
        self.leverage_calls.append((symbol, leverage))
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.calls.append(kwargs)
        return {"orderId": "ex-1", "avgPrice": kwargs.get("stop_loss_price", "0")}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        return {
            "orderId": "ex-1", "status": self._order_status,
            "executedQty": "0.002", "avgPrice": "50030",
        }

    def get_all_positions(self):
        return self._all_positions

    def get_position(self, symbol):
        for p in self._all_positions:
            if p.get("symbol") == symbol:
                return p
        return None

    def get_balance(self):
        return {"availableMargin": self._balance}

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "0"}


class _SpyMarketDataConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "50000"}


def test_has_sufficient_live_capacity_true_when_room_and_margin(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _SpyConnector(balance="50.00", all_positions=[])

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is True


def test_has_sufficient_live_capacity_false_when_margin_short(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _SpyConnector(balance="5.00", all_positions=[])

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is False


def test_has_sufficient_live_capacity_false_when_reconciled_count_at_cap(tmp_path):
    """Local DB says only 3 active, but the exchange's real position list
    (via reconcile_active_executions) confirms all 4 are still genuinely
    open - the reconciled count, not the raw local phase, is authoritative."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(4):
        pid = f"pos-{i}"
        _open_position(repo, pid)
        repo.claim_live_execution(pid, _NOW, "10", "100", "10")
        repo.update_live_execution_submitted(
            pid, f"cid-{i}", f"ex-{i}", "0.002", "50000", None, None, _NOW
        )
    connector = _SpyConnector(
        balance="100.00",
        all_positions=[{"symbol": "BTC-USDT", "positionAmt": "0.002"}] * 4,
    )

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is False


def test_reconcile_active_executions_closes_out_positions_gone_flat_on_exchange(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector(all_positions=[])  # exchange shows nothing open - closed

    count = reconcile_active_executions(repo, connector, _SpyMarketDataConnector(), "r1", _NOW)

    assert count == 0
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"


def test_process_pending_positions_claims_and_submits_when_capacity_available(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert connector.leverage_calls == [("BTC-USDT", 10)]
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"


def test_process_pending_positions_skips_when_capacity_full(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-existing")
    repo.claim_live_execution("pos-existing", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-existing", "cid-0", "ex-0", "0.002", "50000", None, None, _NOW
    )
    _open_position(repo, "pos-new")
    connector = _SpyConnector(
        balance="100.00",
        all_positions=[{"symbol": "BTC-USDT", "positionAmt": "0.002"}],
    )
    settings = get_settings()
    # force max_concurrent_positions=1 for this test via a settings copy
    settings = settings.model_copy(
        update={"live_execution": settings.live_execution.model_copy(
            update={"max_concurrent_positions": 1}
        )}
    )

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert repo.get_live_execution("pos-new") is None  # never claimed
    assert connector.calls == []  # never even attempted an order


def test_process_pending_positions_skips_safely_below_exchange_minimum(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("1000")},  # exchange minimum notional far above 100 USDT
        settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "SKIPPED"
    assert row["last_error"] == "below_exchange_minimum"
    assert connector.calls == []


def test_process_pending_positions_marks_failed_on_unfilled_order(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[], order_status="CANCELED")
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "not filled" in row["last_error"].lower() or "CANCELED" in row["last_error"]


def test_process_pending_positions_marks_failed_on_connector_error(tmp_path):
    class _RaisingConnector(_SpyConnector):
        def place_entry_order_with_sl_tp(self, **kwargs):
            raise ConnectorUnavailableError("boom")

    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _RaisingConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "boom" in row["last_error"]


def test_close_guardian_exit_positions_mirrors_only_after_paper_already_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector()

    close_guardian_exit_positions(repo, connector, "r1", _NOW)
    assert repo.get_live_execution("pos-1")["phase"] == "ACTIVE"  # untouched, PAPER not closed yet

    repo.close_position_with_event(
        position_id="pos-1", theoretical_exit=Decimal("49500"),
        simulated_fill_exit=Decimal("49500"), exit_reason="guardian_exit",
        fees=Decimal("0"), funding=Decimal("0"), closed_at=_NOW,
        event=Event(event_id="POSITION_CLOSED:pos-1", event_type="POSITION_CLOSED",
                    aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
                    run_id="seed", schema_version=1, payload={}),
    )
    close_guardian_exit_positions(repo, connector, "r1", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "GUARDIAN_EXIT"


def test_close_time_limit_positions_uses_the_passed_in_hold_hours(tmp_path):
    """LIVE's own 6h limit, independent of whatever PAPER's is - proven by
    passing a value (2h) that would never trigger under PAPER's 24h."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, opened_at=_NOW - timedelta(hours=3))
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector()

    close_time_limit_positions(repo, connector, max_position_hold_hours=2, run_id="r1", now=_NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "TIME_LIMIT"


def test_recover_stale_claims_looks_up_before_resubmitting(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW - timedelta(seconds=60), "10", "100", "10")
    connector = _SpyConnector(balance="100.00")
    settings = get_settings()

    recover_stale_claims(
        repo, connector, {"BTC-USDT": 3}, {"BTC-USDT": Decimal("0")}, settings,
        "r1", _NOW, stale_after_seconds=30,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
