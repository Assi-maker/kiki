from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_profit_protection_activation_watermark_is_none_before_first_activation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.get_profit_protection_activated_at() is None


def test_profit_protection_activation_watermark_set_once_wins(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    second = datetime(2026, 9, 11, 11, 0, tzinfo=UTC)

    first_call = repo.set_profit_protection_activated_at_if_missing(first)
    second_call = repo.set_profit_protection_activated_at_if_missing(second)

    assert first_call is True
    assert second_call is False
    assert repo.get_profit_protection_activated_at() == first


def test_profit_protection_shadow_positions_table_exists(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    columns = {
        row["name"]
        for row in repo._conn.execute(
            "PRAGMA table_info(profit_protection_shadow_positions)"
        ).fetchall()
    }
    assert columns == {
        "shadow_id", "position_id", "instrument", "threshold_pct", "entry_price",
        "original_stop_loss", "target", "threshold_price", "opened_at", "status",
        "threshold_reached", "threshold_reached_at", "breakeven_stop_loss", "mfe", "mae",
        "exit_reason", "theoretical_exit", "simulated_fill_exit", "fees", "funding",
        "closed_at", "shadow_realized_pnl", "hypothetical_baseline_exit_reason",
        "hypothetical_baseline_pnl", "pnl_difference", "created_at", "updated_at",
    }


def _seed_shadow_kwargs(**overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", entry_price=Decimal("50000"),
        original_stop_loss=Decimal("49000"), target=Decimal("52000"),
        threshold_price=Decimal("50500"), opened_at=_NOW, created_at=_NOW,
    )
    defaults.update(overrides)
    return defaults


def test_seed_profit_protection_shadow_creates_a_row_with_open_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    created = repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    assert created is True
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "OPEN"
    assert row["threshold_reached"] == 0
    assert row["entry_price"] == "50000"


def test_seed_profit_protection_shadow_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first = repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    second = repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    assert first is True
    assert second is False


def test_find_open_profit_protection_shadows_excludes_closed_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(shadow_id="a", position_id="a"))
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(shadow_id="b", position_id="b"))
    repo.close_profit_protection_shadow(
        shadow_id="a", exit_reason="target", theoretical_exit=Decimal("52000"),
        simulated_fill_exit=Decimal("51974"), fees=Decimal("2"), funding=Decimal("0"),
        closed_at=_NOW, shadow_realized_pnl=Decimal("100"), updated_at=_NOW,
    )
    open_rows = repo.find_open_profit_protection_shadows()
    assert [r["shadow_id"] for r in open_rows] == ["b"]


def test_find_all_profit_protection_shadows_returns_open_and_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    assert len(repo.find_all_profit_protection_shadows()) == 1
