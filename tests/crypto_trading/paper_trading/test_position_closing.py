from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_OPENED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5,
        max_total_exposure_pct=Decimal("1.0"),
        spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"),
        fee_pct=Decimal("0.0004"),
        max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


def _open_position(position_id="pos-1", instrument="BTCUSDT") -> Position:
    return Position(
        position_id=position_id,
        candidate_id=f"cand-{position_id}",
        instrument=instrument,
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss="49000",
        target="52000",
        size="5000",
        fill_model_version="v1",
        opened_at=_OPENED_AT,
    )


def _seed(repo, position: Position) -> None:
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{position.position_id}",
            event_type="POSITION_OPENED",
            aggregate_type="position",
            aggregate_id=position.position_id,
            occurred_at=_OPENED_AT,
            run_id="run-1",
            schema_version=1,
            payload={},
        ),
    )


def test_closes_position_on_stop_loss_trigger_with_correct_exit_reason(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    # price_lookup: instrument -> (low, high, current, funding_rate)
    price_lookup = {
        "BTCUSDT": (Decimal("48500"), Decimal("49500"), Decimal("48600"), Decimal("0.0001"))
    }

    closed = close_triggered_positions(
        repo,
        price_lookup,
        now=_OPENED_AT + timedelta(hours=1),
        risk_limits=_risk_limits(),
        run_id="run-1",
    )

    assert len(closed) == 1
    assert closed[0].status == "CLOSED"
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].simulated_fill_exit != closed[0].theoretical_exit
    assert closed[0].fees is not None
    assert closed[0].funding is not None

    reloaded = repo.get_position("pos-1")
    assert reloaded.status == "CLOSED"


def test_closes_position_on_time_limit_trigger(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    price_lookup = {
        "BTCUSDT": (Decimal("49900"), Decimal("50100"), Decimal("50050"), Decimal("0.0001"))
    }

    closed = close_triggered_positions(
        repo,
        price_lookup,
        now=_OPENED_AT + timedelta(hours=25),
        risk_limits=_risk_limits(),
        run_id="run-1",
    )

    assert len(closed) == 1
    assert closed[0].exit_reason == "time_limit"


def test_leaves_position_open_when_nothing_triggers(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    price_lookup = {
        "BTCUSDT": (Decimal("49900"), Decimal("50100"), Decimal("50000"), Decimal("0.0001"))
    }

    closed = close_triggered_positions(
        repo,
        price_lookup,
        now=_OPENED_AT + timedelta(hours=1),
        risk_limits=_risk_limits(),
        run_id="run-1",
    )

    assert closed == []
    reloaded = repo.get_position("pos-1")
    assert reloaded.status == "OPEN_POSITION"


def test_closing_is_idempotent_when_called_twice(tmp_path):
    """SPEC §8.6: ingen dubbel CLOSED-event."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    price_lookup = {
        "BTCUSDT": (Decimal("48500"), Decimal("49500"), Decimal("48600"), Decimal("0.0001"))
    }

    close_triggered_positions(
        repo,
        price_lookup,
        now=_OPENED_AT + timedelta(hours=1),
        risk_limits=_risk_limits(),
        run_id="run-1",
    )
    second = close_triggered_positions(
        repo,
        price_lookup,
        now=_OPENED_AT + timedelta(hours=2),
        risk_limits=_risk_limits(),
        run_id="run-2",
    )

    assert second == []  # redan CLOSED, plockas inte upp av find_open_positions
    event_count = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = 'POSITION_CLOSED'"
    ).fetchone()["n"]
    assert event_count == 1
