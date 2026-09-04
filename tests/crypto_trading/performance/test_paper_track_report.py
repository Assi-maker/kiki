from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.performance.paper_track_report import build_report
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _NoopConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "0"}


def test_build_report_includes_demo_comparison_for_matched_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTC-USDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    repo.close_position_with_event(
        position_id="pos-1", theoretical_exit=Decimal("52000"),
        simulated_fill_exit=Decimal("51975"), exit_reason="target",
        fees=Decimal("0.4"), funding=Decimal("0"), closed_at=_NOW,
        event=Event(event_id="POSITION_CLOSED:pos-1", event_type="POSITION_CLOSED",
                    aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
                    run_id="seed", schema_version=1, payload={}),
    )
    repo.claim_demo_execution("pos-1", _NOW)
    repo.update_demo_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.02", "50040", "sl-1", "tp-1", _NOW
    )
    repo.close_demo_execution("pos-1", "target", "51980", _NOW)

    report = build_report(repo, _NoopConnector(), log_glob="nonexistent-*.log")

    comparison = report["demo_comparison"]
    assert len(comparison) == 1
    row = comparison[0]
    assert row["position_id"] == "pos-1"
    assert row["paper_exit_reason"] == "target"
    assert row["demo_exit_reason"] == "target"
    assert row["paper_fill_exit"] == "51975"
    assert row["demo_fill_exit"] == "51980"
