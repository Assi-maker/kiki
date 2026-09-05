from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig, RiskLimitsConfig
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_OPENED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5,
        max_total_exposure_pct=Decimal("1.0"),
        max_position_notional_usdt=Decimal("1000000"),
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


def _seed_guardian_observation(repo, position_id: str, state: str, observed_at: datetime) -> None:
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id=f"{position_id}:{observed_at.isoformat()}",
            position_id=position_id,
            observed_at=observed_at,
            state=state,
            decay_score=Decimal("0.9") if state == "EXIT" else Decimal("0.1"),
            progress_ratio=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            factors={},
            run_id="run-guardian",
        )
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


# --- Guardian-assisted exit (2026-09-05) ---
# Shadow-mode default (guardian_config=None eller assisted_exit_enabled=False)
# ska bevara exakt tidigare beteende; aktiverad ska bara stänga på state=="EXIT".

_WITHIN_RANGE_PRICE_LOOKUP = {
    "BTCUSDT": (Decimal("49900"), Decimal("50100"), Decimal("50050"), Decimal("0.0001"))
}


def test_guardian_exit_closes_position_when_enabled_and_state_is_exit(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    observed_at = _OPENED_AT + timedelta(minutes=30)
    _seed_guardian_observation(repo, "pos-1", "EXIT", observed_at)
    guardian_config = GuardianConfig(assisted_exit_enabled=True)

    closed = close_triggered_positions(
        repo, _WITHIN_RANGE_PRICE_LOOKUP, now=_OPENED_AT + timedelta(hours=1),
        risk_limits=_risk_limits(), run_id="run-1", guardian_config=guardian_config,
    )

    assert len(closed) == 1
    assert closed[0].exit_reason == "guardian_exit"
    assert closed[0].status == "CLOSED"


def test_guardian_exit_never_closes_when_feature_disabled_even_with_exit_state(tmp_path):
    """Shadow-mode tills funktionen aktiveras separat (explicit
    användarkrav): en EXIT-observation finns, men assisted_exit_enabled är
    False (default) -> positionen förblir öppen, exakt som idag."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    _seed_guardian_observation(repo, "pos-1", "EXIT", _OPENED_AT + timedelta(minutes=30))
    guardian_config = GuardianConfig(assisted_exit_enabled=False)

    closed = close_triggered_positions(
        repo, _WITHIN_RANGE_PRICE_LOOKUP, now=_OPENED_AT + timedelta(hours=1),
        risk_limits=_risk_limits(), run_id="run-1", guardian_config=guardian_config,
    )

    assert closed == []
    assert repo.get_position("pos-1").status == "OPEN_POSITION"


def test_guardian_exit_never_closes_without_guardian_config_passed_at_all(tmp_path):
    """Bakåtkompatibilitet: en anropare som inte skickar guardian_config
    alls (default None) - t.ex. äldre kod eller tester - får exakt samma
    beteende som innan ändringen, även om en EXIT-observation råkar finnas."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    _seed_guardian_observation(repo, "pos-1", "EXIT", _OPENED_AT + timedelta(minutes=30))

    closed = close_triggered_positions(
        repo, _WITHIN_RANGE_PRICE_LOOKUP, now=_OPENED_AT + timedelta(hours=1),
        risk_limits=_risk_limits(), run_id="run-1",
    )

    assert closed == []


def test_guardian_watch_and_protect_states_never_close_position_even_when_enabled(tmp_path):
    for state in ("HOLD", "WATCH", "PROTECT"):
        repo = SQLiteRepository(tmp_path / f"t-{state}.db")
        _seed(repo, _open_position())
        _seed_guardian_observation(repo, "pos-1", state, _OPENED_AT + timedelta(minutes=30))
        guardian_config = GuardianConfig(assisted_exit_enabled=True)

        closed = close_triggered_positions(
            repo, _WITHIN_RANGE_PRICE_LOOKUP, now=_OPENED_AT + timedelta(hours=1),
            risk_limits=_risk_limits(), run_id="run-1", guardian_config=guardian_config,
        )

        assert closed == [], f"state={state} stängde felaktigt positionen"


def test_time_limit_absolute_fallback_wins_even_with_guardian_exit_enabled(tmp_path):
    """Time limit fungerar som absolut fallback: när tidsgränsen nås
    stänger den befintliga time-limit-logiken positionen oavsett
    Guardian-state (även om ingen Guardian-observation alls finns än)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position())
    guardian_config = GuardianConfig(assisted_exit_enabled=True)

    closed = close_triggered_positions(
        repo, _WITHIN_RANGE_PRICE_LOOKUP, now=_OPENED_AT + timedelta(hours=25),
        risk_limits=_risk_limits(), run_id="run-1", guardian_config=guardian_config,
    )

    assert len(closed) == 1
    assert closed[0].exit_reason == "time_limit"


def test_guardian_exit_on_one_position_never_affects_a_different_open_position(tmp_path):
    """Guardian EXIT påverkar inte andra/nya positioner - guardian_state-
    uppslagningen är strikt per position_id."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed(repo, _open_position(position_id="pos-1", instrument="BTCUSDT"))
    _seed(repo, _open_position(position_id="pos-2", instrument="ETHUSDT"))
    _seed_guardian_observation(repo, "pos-1", "EXIT", _OPENED_AT + timedelta(minutes=30))
    # pos-2 har ingen Guardian-observation alls.
    price_lookup = {
        "BTCUSDT": (Decimal("49900"), Decimal("50100"), Decimal("50050"), Decimal("0.0001")),
        "ETHUSDT": (Decimal("49900"), Decimal("50100"), Decimal("50050"), Decimal("0.0001")),
    }
    guardian_config = GuardianConfig(assisted_exit_enabled=True)

    closed = close_triggered_positions(
        repo, price_lookup, now=_OPENED_AT + timedelta(hours=1),
        risk_limits=_risk_limits(), run_id="run-1", guardian_config=guardian_config,
    )

    closed_ids = {p.position_id for p in closed}
    assert closed_ids == {"pos-1"}
    assert repo.get_position("pos-2").status == "OPEN_POSITION"
