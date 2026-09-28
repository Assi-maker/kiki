"""P0 Safety Kernel on every live stop replacement (2026-09-28).

Profit Protection and Guardian Authority each already verify their own
move, but the kernel is the one deterministic rule set above both: a LONG
stop may only move up, and never to within the liquidation buffer. Nothing
is placed or cancelled when it refuses."""
from decimal import Decimal

from crypto_trading.paper_trading.live_profit_protection import run_live_profit_protection_tick
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.guardian import test_authority_live as ga
from tests.crypto_trading.paper_trading import test_live_profit_protection as pp


def test_profit_protection_never_lowers_a_stop_that_is_already_above_break_even(tmp_path):
    """A stop already tightened above entry (e.g. by Guardian Authority):
    PP's break-even move would LOOSEN it. The kernel refuses."""
    repo = SQLiteRepository(tmp_path / "t.db")
    pp._open_active_live_position(repo)
    connector = pp._SpyConnector(
        positions=[pp._ABOVE_THRESHOLD_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "50300"}],
        lookup_order={"orderId": "new-sl-1", "status": "NEW"}, stateful_orders=True,
    )

    run_live_profit_protection_tick(repo, connector, pp._THRESHOLD, "r1", pp._NOW)

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ABORTED_SAFETY_KERNEL"
    assert "STOP_LOOSENING_FORBIDDEN" in row["last_error"]
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_profit_protection_normal_break_even_move_is_still_allowed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    pp._open_active_live_position(repo)
    connector = pp._SpyConnector(
        positions=[pp._ABOVE_THRESHOLD_POSITION], open_orders=pp._ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"}, stateful_orders=True,
    )
    run_live_profit_protection_tick(repo, connector, pp._THRESHOLD, "r1", pp._NOW)
    assert repo.get_live_profit_protection("pos-1")["status"] == "SL_REPLACED"


def test_guardian_authority_cannot_place_a_stop_next_to_liquidation(tmp_path):
    """A (legacy) live stop at 45000 at 10x: a 'tightening' to 45600 passes
    the authority's own strictly-higher rule, but still sits inside the
    liquidation buffer (estimated liquidation 45500, buffer 2 % = 46500)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    ga._open_active_live_position(repo)
    connector = ga._SpyConnector(
        positions=[ga._LIVE_POSITION],
        open_orders=[{"type": "STOP_MARKET", "orderId": "old-sl-1", "stopPrice": "45000"}],
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )

    ga._apply(repo, connector, new_sl=Decimal("45600"))

    row = ga._row(repo)
    assert row["status"] == "ABORTED_SAFETY_KERNEL"
    assert "LIQUIDATION_TOO_CLOSE" in row["last_error"]
    assert connector.place_calls == []
    assert connector.cancel_calls == []


def test_guardian_authority_normal_tightening_is_still_allowed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    ga._open_active_live_position(repo)
    connector = ga._SpyConnector(
        positions=[ga._LIVE_POSITION], open_orders=ga._ONE_OLD_SL,
        lookup_order={"orderId": "new-sl-1", "status": "NEW"},
    )
    ga._apply(repo, connector)
    assert ga._row(repo)["status"] == "SL_REPLACED"
