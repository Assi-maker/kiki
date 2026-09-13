from datetime import UTC, datetime, timedelta

from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def test_claim_live_profit_protection_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    first = repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )
    second = repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )

    assert first is True
    assert second is False


def test_get_live_profit_protection_returns_none_before_claim(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    assert repo.get_live_profit_protection("pos-1") is None


def test_get_live_profit_protection_returns_full_row_after_claim(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )

    row = repo.get_live_profit_protection("pos-1")
    assert row["position_id"] == "pos-1"
    assert row["status"] == "CLAIMED"
    assert row["threshold_pct"] == "0.01"
    assert row["trigger_mark_price"] == "50500"
    assert row["breakeven_price"] == "50000"
    assert row["new_sl_client_order_id"] == "pp-cid-1"
    assert row["old_sl_order_id"] is None
    assert row["old_sl_price"] is None
    assert row["new_sl_order_id"] is None
    assert row["last_error"] is None
    assert row["claimed_at"] == _NOW.isoformat()
    assert row["updated_at"] == _NOW.isoformat()


def test_find_claimed_live_profit_protection_returns_only_claimed_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )
    repo.claim_live_profit_protection(
        "pos-2", "0.01", "60600", "60000", "pp-cid-2", _NOW
    )
    repo.set_live_profit_protection_status("pos-2", "SL_REPLACED", _NOW)

    claimed = repo.find_claimed_live_profit_protection()

    assert [row["position_id"] for row in claimed] == ["pos-1"]
    assert claimed[0]["status"] == "CLAIMED"


def test_update_live_profit_protection_old_sl(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )

    repo.update_live_profit_protection_old_sl(
        "pos-1", "old-sl-order-1", "49000", _NOW + timedelta(seconds=1)
    )

    row = repo.get_live_profit_protection("pos-1")
    assert row["old_sl_order_id"] == "old-sl-order-1"
    assert row["old_sl_price"] == "49000"
    assert row["updated_at"] == (_NOW + timedelta(seconds=1)).isoformat()
    # unrelated fields untouched:
    assert row["status"] == "CLAIMED"
    assert row["new_sl_order_id"] is None


def test_update_live_profit_protection_new_sl_does_not_clobber_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )
    repo.update_live_profit_protection_old_sl(
        "pos-1", "old-sl-order-1", "49000", _NOW
    )

    repo.update_live_profit_protection_new_sl(
        "pos-1", "new-sl-order-1", _NOW + timedelta(seconds=2)
    )

    row = repo.get_live_profit_protection("pos-1")
    assert row["new_sl_order_id"] == "new-sl-order-1"
    assert row["updated_at"] == (_NOW + timedelta(seconds=2)).isoformat()
    # status update must never clobber a previously-recorded new_sl_order_id,
    # and this update must never clobber status:
    assert row["status"] == "CLAIMED"
    assert row["old_sl_order_id"] == "old-sl-order-1"
    assert row["old_sl_price"] == "49000"


def test_set_live_profit_protection_status_does_not_clobber_new_sl_order_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )
    repo.update_live_profit_protection_new_sl(
        "pos-1", "new-sl-order-1", _NOW + timedelta(seconds=2)
    )

    repo.set_live_profit_protection_status(
        "pos-1", "SL_REPLACED", _NOW + timedelta(seconds=3)
    )

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "SL_REPLACED"
    assert row["updated_at"] == (_NOW + timedelta(seconds=3)).isoformat()
    # status change must never clobber the previously-recorded new_sl_order_id:
    assert row["new_sl_order_id"] == "new-sl-order-1"
    assert row["last_error"] is None


def test_set_live_profit_protection_status_records_last_error(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.claim_live_profit_protection(
        "pos-1", "0.01", "50500", "50000", "pp-cid-1", _NOW
    )

    repo.set_live_profit_protection_status(
        "pos-1", "ABORTED_AMBIGUOUS_SL", _NOW, last_error="found 2 STOP_MARKET orders"
    )

    row = repo.get_live_profit_protection("pos-1")
    assert row["status"] == "ABORTED_AMBIGUOUS_SL"
    assert row["last_error"] == "found 2 STOP_MARKET orders"
