from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.performance.live_track_report import build_live_report
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def test_build_live_report_includes_every_live_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTC-USDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50010", None, None, _NOW
    )
    repo.close_live_execution(
        "pos-1", "target", "52000", _NOW, realized_fees_usdt="0.08", realized_funding_usdt="-0.01"
    )

    report = build_live_report(repo)

    assert len(report["live_positions"]) == 1
    row = report["live_positions"][0]
    assert row["position_id"] == "pos-1"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"
    assert row["leverage"] == "10"
    assert row["exit_reason"] == "target"
    assert row["realized_fees_usdt"] == "0.08"
    assert row["realized_funding_usdt"] == "-0.01"
    # (52000 - 50010) * 0.002 - 0.08 + (-0.01) = 3.98 - 0.08 - 0.01 = 3.89
    assert Decimal(report["total_live_pnl_usdt"]) == Decimal("3.89")


def test_build_live_report_empty_when_no_live_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    report = build_live_report(repo)

    assert report["live_positions"] == []
    assert report["total_live_pnl_usdt"] == "0"
