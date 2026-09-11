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
