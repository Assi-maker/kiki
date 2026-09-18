from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from crypto_trading.config.loader import get_settings
from crypto_trading.live_execution_loop import run_live_execution_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class _SpyConnector:
    def __init__(self, order_status="FILLED"):
        self.place_calls = 0
        self._order_status = order_status

    def set_leverage(self, symbol, leverage=10, side="LONG"):
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.place_calls += 1
        return {"orderId": "ex-1", "avgPrice": "50010"}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        return {
            "orderId": "ex-1", "status": self._order_status,
            "executedQty": "0.002" if self._order_status == "FILLED" else "0",
            "avgPrice": "50010",
        }

    def get_all_positions(self):
        # Gated on place_calls, not _order_status: the new per-symbol LIVE
        # safety gate (live_execution.py's _has_active_live_position_for_
        # symbol) queries the exchange BEFORE an entry is ever placed, and
        # must see nothing there yet - only after place_entry_order_with_sl_
        # tp has actually been called does a position exist to report.
        if self.place_calls > 0 and self._order_status == "FILLED":
            return [{"symbol": "BTC-USDT", "positionAmt": "0.002"}]
        return []

    def get_position(self, symbol):
        if self.place_calls > 0 and self._order_status == "FILLED":
            return {"symbol": symbol, "positionAmt": "0.002"}
        return None

    def get_balance(self):
        return {"availableMargin": "100.00"}

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "0"}


class _SpyMarketDataConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "50000"}


def _seed_open_position(repo, position_id="pos-1"):
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    # Spec §17.3's authoritative signal timestamp - fresh at _NOW, well
    # within the default signal_ttl_seconds, so these pre-existing tests
    # keep exercising claim/submit exactly as before the TTL check existed.
    confirmed_event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{position_id}:CONFIRMED",
        event_type="CANDIDATE_TRANSITIONED", aggregate_type="candidate",
        aggregate_id=position_id, occurred_at=_NOW, run_id="seed",
        schema_version=1, payload={"from": "UNDER_AI_ANALYSIS", "to": "CONFIRMED"},
    )
    repo.transition_candidate_with_event(position_id, "CONFIRMED", _NOW, confirmed_event)


def test_run_live_execution_tick_processes_pending_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector()

    run_live_execution_tick(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"


def test_run_live_execution_tick_resolves_a_pending_entry_across_ticks_without_duplicate_order(tmp_path):
    """End-to-end (2026-09-06 safety audit, Risk D fix): an order left
    uncertain (still "NEW") after one tick must be resolved - never
    resubmitted - by resolve_pending_entries() on a later tick."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector(order_status="NEW")

    run_live_execution_tick(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )
    assert repo.get_live_execution("pos-1")["phase"] == "ENTRY_SUBMITTED"
    assert connector.place_calls == 1

    connector._order_status = "FILLED"
    run_live_execution_tick(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW + timedelta(minutes=1),
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert connector.place_calls == 1  # still exactly one - resolution never resubmits


def test_run_live_execution_tick_never_crashes_the_caller_on_unexpected_error(tmp_path):
    class _ExplodingConnector(_SpyConnector):
        def get_balance(self):
            # get_balance() is on the real call path (has_sufficient_live_capacity,
            # called from process_pending_positions) - unlike get_all_positions,
            # which nothing in live_execution.py calls directly.
            raise RuntimeError("simulated crash")

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)

    # must not raise
    run_live_execution_tick(
        repo, _ExplodingConnector(), _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )


def _with_profit_protection_enabled(settings, enabled=True):
    return settings.model_copy(
        update={"live_execution": settings.live_execution.model_copy(
            update={"profit_protection_enabled": enabled}
        )}
    )


def test_run_live_execution_tick_never_calls_profit_protection_when_disabled(tmp_path):
    """Task 7 wiring: profit_protection_enabled defaults to False, so
    run_live_profit_protection_tick must never be invoked during a normal
    tick unless a caller explicitly opts in via config."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector()
    settings = get_settings()
    assert settings.live_execution.profit_protection_enabled is False

    with patch(
        "crypto_trading.live_execution_loop.run_live_profit_protection_tick"
    ) as mock_pp:
        run_live_execution_tick(
            repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
            {"BTC-USDT": Decimal("0")}, settings, _NOW,
        )

    mock_pp.assert_not_called()


def test_run_live_execution_tick_calls_profit_protection_in_correct_order_when_enabled(tmp_path):
    """Task 7 wiring / design spec "Integration point": when enabled,
    run_live_profit_protection_tick must run after reconcile_active_
    executions (so it scans a freshly-reconciled ACTIVE list) and before
    close_time_limit_positions/process_pending_positions (so it never
    races a same-tick close or a new-entry capacity check)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector()
    settings = _with_profit_protection_enabled(get_settings())
    call_order = []

    def _recorder(name):
        def _fn(*args, **kwargs):
            call_order.append(name)
        return _fn

    with (
        patch(
            "crypto_trading.live_execution_loop.reconcile_active_executions",
            side_effect=_recorder("reconcile_active_executions"),
        ) as mock_reconcile,
        patch(
            "crypto_trading.live_execution_loop.run_live_profit_protection_tick",
            side_effect=_recorder("run_live_profit_protection_tick"),
        ) as mock_pp,
        patch(
            "crypto_trading.live_execution_loop.close_guardian_exit_positions",
            side_effect=_recorder("close_guardian_exit_positions"),
        ),
        patch(
            "crypto_trading.live_execution_loop.close_time_limit_positions",
            side_effect=_recorder("close_time_limit_positions"),
        ),
        patch(
            "crypto_trading.live_execution_loop.process_pending_positions",
            side_effect=_recorder("process_pending_positions"),
        ),
    ):
        run_live_execution_tick(
            repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
            {"BTC-USDT": Decimal("0")}, settings, _NOW,
        )

    assert call_order == [
        "reconcile_active_executions",
        "run_live_profit_protection_tick",
        "close_guardian_exit_positions",
        "close_time_limit_positions",
        "process_pending_positions",
    ]

    assert mock_pp.call_count == 1
    pp_args = mock_pp.call_args.args
    assert pp_args[0] is repo
    assert pp_args[1] is connector
    assert pp_args[2] == settings.live_execution.profit_protection_threshold_pct
    assert pp_args[4] == _NOW
    # Same run_id this tick used for reconcile_active_executions - proves
    # the call isn't accidentally minting/forwarding a different run.
    assert pp_args[3] == mock_reconcile.call_args.args[3]
