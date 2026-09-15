import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.guardian.authority import decide_open_position, decide_pre_entry
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.paper_trading.guardian_authority_shadow import (
    _position_factors,
    _resolve_on_close,
    advance_shadow,
    run_guardian_authority_shadow_tick,
    seed_shadow_for_position,
    update_shadow_heuristics_from_resolved_shadow_observations,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _position(position_id="pos-1", opened_at=_NOW, instrument="BTCUSDT") -> Position:
    return Position(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("5000"), fill_model_version="v1",
        opened_at=opened_at,
    )


def _seed_real_position(repo, **overrides) -> Position:
    position = _position(**overrides)
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{position.position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position.position_id,
            occurred_at=position.opened_at, run_id="seed", schema_version=1, payload={},
        ),
    )
    return position


def _seed_observation(repo, position_id, state="HOLD", factors=None, observed_at=_NOW, run_id="obs-run"):
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id=f"obs:{position_id}:{observed_at.isoformat()}",
            position_id=position_id, observed_at=observed_at, state=state,
            decay_score=Decimal("0"), progress_ratio=Decimal("0"), unrealized_pnl=Decimal("0"),
            factors=factors or {}, run_id=run_id,
        )
    )


def _seed_always_on_heuristic(repo, heuristic_id="h-1", adjustment=0.2):
    """An "always-on" heuristic (empty condition_json matches every
    factors dict, per guardian/authority.py's own documented semantics) -
    same precedent tests/crypto_trading/guardian/test_tick.py's own
    _seed_always_on_heuristic already established for driving
    evaluate_heuristics' summed score above/below a chosen threshold
    without hand-computing specific factor values."""
    repo.upsert_guardian_authority_heuristic(
        heuristic_id=heuristic_id, description="always-on test heuristic",
        condition_json="{}", adjustment=adjustment, confidence=0.8,
        sample_size=10, updated_at=_NOW,
    )


def _shadow_row(repo, position_id="pos-1"):
    return repo.get_guardian_authority_shadow(position_id)


def _settings_with_shadow(enabled: bool, tighten_threshold=0.15, close_threshold=0.45) -> "Settings":
    settings = _market_settings(top_n=1)
    settings.guardian = GuardianConfig(
        authority_shadow_enabled=enabled,
        authority_tighten_threshold=tighten_threshold,
        authority_close_threshold=close_threshold,
    )
    return settings


# ---------------------------------------------------------------------------
# seed_shadow_for_position
# ---------------------------------------------------------------------------


def test_seed_shadow_for_position_creates_a_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    row = _shadow_row(repo)
    assert row is not None
    assert row["shadow_id"] == "pos-1"
    assert row["position_id"] == "pos-1"
    assert row["candidate_id"] == "pos-1"
    assert row["instrument"] == "BTCUSDT"
    assert row["status"] == "OBSERVING"
    assert row["mfe"] == "0"
    assert row["mae"] == "0"
    assert row["shadow_decision"] is None
    assert row["run_id"] == "run-1"


def test_seed_shadow_for_position_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    seed_shadow_for_position(repo, position, _NOW + timedelta(minutes=1), "run-2")
    rows = repo._conn.execute(
        "SELECT * FROM guardian_authority_shadow_observations"
    ).fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["run_id"] == "run-1"  # first call wins, second is a no-op


# ---------------------------------------------------------------------------
# _position_factors
# ---------------------------------------------------------------------------


def test_position_factors_returns_empty_dict_when_no_observation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert _position_factors(repo, "pos-1") == {}


def test_position_factors_returns_the_latest_real_observations_factors(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_observation(repo, "pos-1", state="PROTECT", factors={"time_decay": 0.5})
    assert _position_factors(repo, "pos-1") == {"time_decay": 0.5}


# ---------------------------------------------------------------------------
# advance_shadow - the core cold-start proof: empty heuristics -> NO_ACTION
# -> stays OBSERVING, but mfe/mae/last_factors_json still update every tick.
# ---------------------------------------------------------------------------


def test_advance_shadow_empty_heuristics_stays_observing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    shadow = _shadow_row(repo)

    advance_shadow(
        shadow, position, guardian_state="HOLD", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )

    row = _shadow_row(repo)
    assert row["status"] == "OBSERVING"
    assert row["shadow_decision"] is None
    assert row["decided_at"] is None
    assert row["factors_json"] is None  # never written - only decide_guardian_authority_shadow sets it
    assert row["mfe"] == "300"   # 50300 - 50000 (entry = theoretical_entry)
    assert row["mae"] == "-300"  # 49700 - 50000
    assert row["last_factors_json"] == '{"guardian_state": "HOLD"}'


def test_advance_shadow_no_decide_call_when_status_already_decided(tmp_path):
    """Belt-and-suspenders: even if this tick's hypothetical decision would
    again be non-NO_ACTION, advance_shadow must not attempt the decide call
    at all once status is no longer OBSERVING (the repo's own WHERE
    status = 'OBSERVING' guard would refuse it anyway, but this proves the
    Python-level early return, not just the DB-level guard)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    _seed_always_on_heuristic(repo, adjustment=0.2)
    shadow = _shadow_row(repo)

    advance_shadow(
        shadow, position, guardian_state="HOLD", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )
    decided = _shadow_row(repo)
    assert decided["status"] == "DECIDED"
    first_decided_at = decided["decided_at"]
    first_factors_json = decided["factors_json"]

    later = _NOW + timedelta(minutes=1)
    advance_shadow(
        decided, position, guardian_state="PROTECT", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50100"), candle_high=Decimal("50400"), candle_low=Decimal("49600"),
        now=later, repo=repo,
    )
    row = _shadow_row(repo)
    assert row["status"] == "DECIDED"
    assert row["decided_at"] == first_decided_at  # immutable
    assert row["factors_json"] == first_factors_json  # immutable
    assert row["last_factors_json"] == '{"guardian_state": "PROTECT"}'  # scratch keeps updating
    assert row["mfe"] == "400"  # 50400 - 50000, continued running max


# ---------------------------------------------------------------------------
# advance_shadow - a genuine hypothetical intervention (TIGHTEN_SL / CLOSE_EARLY)
# ---------------------------------------------------------------------------


def test_advance_shadow_tighten_sl_transitions_to_decided(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    _seed_always_on_heuristic(repo, adjustment=0.2)  # > tighten_threshold(0.15), < close_threshold(0.45)
    shadow = _shadow_row(repo)

    advance_shadow(
        shadow, position, guardian_state="WATCH", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )

    row = _shadow_row(repo)
    assert row["status"] == "DECIDED"
    assert row["shadow_decision"] == "TIGHTEN_SL"
    assert row["expected_direction"] == "favorable"
    assert row["decided_at"] is not None
    assert row["proposed_new_sl"] is not None
    assert Decimal(row["proposed_new_sl"]) > position.stop_loss
    assert row["factors_json"] == '{"guardian_state": "WATCH"}'


def test_advance_shadow_close_early_transitions_to_decided_with_no_proposed_sl(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    _seed_always_on_heuristic(repo, adjustment=0.5)  # > close_threshold(0.45)
    shadow = _shadow_row(repo)

    advance_shadow(
        shadow, position, guardian_state="EXIT", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )

    row = _shadow_row(repo)
    assert row["status"] == "DECIDED"
    assert row["shadow_decision"] == "CLOSE_EARLY"
    assert row["expected_direction"] == "unfavorable"
    assert row["proposed_new_sl"] is None


def test_advance_shadow_never_writes_to_positions_table(tmp_path):
    """G1-equivalent: advancing a shadow must never mutate the real
    position row, whatever the hypothetical decision is."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    _seed_always_on_heuristic(repo, adjustment=0.5)
    shadow = _shadow_row(repo)
    before = repo.get_position("pos-1")

    advance_shadow(
        shadow, position, guardian_state="EXIT", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )

    after = repo.get_position("pos-1")
    assert before == after
    assert after.status == "OPEN_POSITION"
    assert after.stop_loss == Decimal("49000")  # untouched


# ---------------------------------------------------------------------------
# _resolve_on_close
# ---------------------------------------------------------------------------


def _closed_position(position, exit_reason="stop_loss", pnl_favorable=True) -> Position:
    simulated_fill_exit = (
        position.simulated_fill_entry * Decimal("1.02")
        if pnl_favorable
        else position.simulated_fill_entry * Decimal("0.98")
    )
    return position.model_copy(update={
        "status": "CLOSED", "exit_reason": exit_reason,
        "theoretical_exit": position.theoretical_entry, "simulated_fill_exit": simulated_fill_exit,
        "fees": Decimal("2"), "funding": Decimal("0"), "closed_at": _NOW,
    })


def test_resolve_on_close_observing_writes_retroactive_no_action(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, position, guardian_state="HOLD", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )
    observing = _shadow_row(repo)
    assert observing["status"] == "OBSERVING"

    closed_position = _closed_position(position)
    _resolve_on_close(repo, observing, closed_position, _NOW + timedelta(minutes=5))

    row = _shadow_row(repo)
    assert row["status"] == "RESOLVED"
    assert row["shadow_decision"] == "NO_ACTION"
    assert row["expected_direction"] == "neutral"
    assert row["confidence"] == 1.0
    assert row["factors_json"] == '{"guardian_state": "HOLD"}'  # from last_factors_json
    assert row["actual_exit_reason"] == "stop_loss"
    assert row["expectation_correct"] is None
    assert row["prediction_error"] is None
    assert Decimal(row["actual_pnl_usdt"]) == compute_pnl(closed_position)


def test_resolve_on_close_observing_with_no_ticks_falls_back_to_empty_factors(tmp_path):
    """A shadow that never advanced even once (seeded and closed the same
    tick, no candle in between) has last_factors_json == None - the
    fallback must be '{}', never a crash."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    shadow = _shadow_row(repo)
    assert shadow["last_factors_json"] is None

    closed_position = _closed_position(position)
    _resolve_on_close(repo, shadow, closed_position, _NOW)

    row = _shadow_row(repo)
    assert row["status"] == "RESOLVED"
    assert row["factors_json"] == "{}"


def test_resolve_on_close_decided_tighten_sl_correct_expectation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    _seed_always_on_heuristic(repo, adjustment=0.2)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, position, guardian_state="WATCH", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )
    decided = _shadow_row(repo)
    assert decided["shadow_decision"] == "TIGHTEN_SL"
    assert decided["expected_direction"] == "favorable"

    # Favorable predicted, and the real position DID close with a positive
    # PnL - expectation_correct must be True.
    closed_position = _closed_position(position, exit_reason="target", pnl_favorable=True)
    _resolve_on_close(repo, decided, closed_position, _NOW + timedelta(minutes=5))

    row = _shadow_row(repo)
    assert row["status"] == "RESOLVED"
    assert bool(row["expectation_correct"]) is True
    confidence = decided["confidence"]
    expected_error = (confidence - 1.0) ** 2
    assert row["prediction_error"] == expected_error
    # Immutable fields from decide time must be untouched.
    assert row["shadow_decision"] == "TIGHTEN_SL"
    assert row["expected_direction"] == "favorable"


def test_resolve_on_close_decided_tighten_sl_incorrect_expectation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    _seed_always_on_heuristic(repo, adjustment=0.2)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, position, guardian_state="WATCH", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )
    decided = _shadow_row(repo)

    # Favorable predicted, but the real position closed at a LOSS -
    # expectation_correct must be False.
    closed_position = _closed_position(position, exit_reason="stop_loss", pnl_favorable=False)
    _resolve_on_close(repo, decided, closed_position, _NOW + timedelta(minutes=5))

    row = _shadow_row(repo)
    assert bool(row["expectation_correct"]) is False
    confidence = decided["confidence"]
    expected_error = (confidence - 0.0) ** 2
    assert row["prediction_error"] == expected_error


def test_resolve_on_close_decided_close_early_leaves_expectation_null(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    seed_shadow_for_position(repo, position, _NOW, "run-1")
    _seed_always_on_heuristic(repo, adjustment=0.5)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, position, guardian_state="EXIT", tighten_threshold=0.15, close_threshold=0.45,
        current_price=Decimal("50000"), candle_high=Decimal("50300"), candle_low=Decimal("49700"),
        now=_NOW, repo=repo,
    )
    decided = _shadow_row(repo)
    assert decided["shadow_decision"] == "CLOSE_EARLY"

    closed_position = _closed_position(position, exit_reason="guardian_exit", pnl_favorable=False)
    _resolve_on_close(repo, decided, closed_position, _NOW + timedelta(minutes=5))

    row = _shadow_row(repo)
    assert row["status"] == "RESOLVED"
    assert row["expectation_correct"] is None
    assert row["prediction_error"] is None


# ---------------------------------------------------------------------------
# run_guardian_authority_shadow_tick - orchestration
# ---------------------------------------------------------------------------


def test_tick_flag_off_seeds_nothing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}
    run_guardian_authority_shadow_tick(
        repo, [position], [], price_lookup, _NOW, _settings_with_shadow(enabled=False), "run-1"
    )
    rows = repo._conn.execute("SELECT * FROM guardian_authority_shadow_observations").fetchall()
    assert rows == []


def test_tick_seeds_and_advances_in_the_same_tick(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="HOLD", factors={})
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}

    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")

    row = _shadow_row(repo)
    assert row is not None
    assert row["status"] == "OBSERVING"  # empty heuristics -> NO_ACTION
    assert row["mfe"] == "300"
    assert row["mae"] == "-300"


def test_tick_defers_advance_when_no_guardian_observation_yet(tmp_path):
    """A shadow seeded this same tick, with Guardian never having ticked
    for this position yet, must not crash and must not be advanced - mfe/
    mae stay at their seeded defaults until an observation exists."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}

    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")

    row = _shadow_row(repo)
    assert row is not None
    assert row["status"] == "OBSERVING"
    assert row["mfe"] == "0"
    assert row["mae"] == "0"


def test_tick_defers_advance_when_instrument_missing_from_price_lookup(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="HOLD", factors={})
    settings = _settings_with_shadow(enabled=True)

    run_guardian_authority_shadow_tick(repo, [position], [], {}, _NOW, settings, "run-1")

    row = _shadow_row(repo)
    assert row is not None  # still seeded
    assert row["mfe"] == "0"  # never advanced - no candle this tick


def test_tick_transitions_to_decided_on_tick_n_and_stays_decided_through_later_ticks(tmp_path):
    """The second core proof: a hypothetical intervention registered on
    tick N is immutable through ticks N+1..M, even though the always-on
    heuristic would again qualify every subsequent tick."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="WATCH", factors={})
    _seed_always_on_heuristic(repo, adjustment=0.2)
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}

    # Tick 1 (tick N): decides TIGHTEN_SL.
    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")
    tick_1 = _shadow_row(repo)
    assert tick_1["status"] == "DECIDED"
    assert tick_1["shadow_decision"] == "TIGHTEN_SL"
    first_decided_at = tick_1["decided_at"]
    first_proposed_sl = tick_1["proposed_new_sl"]

    # Tick 2 and tick 3 (N+1, N+2): the same always-on heuristic would
    # again qualify, but the row must stay exactly as tick 1 left it.
    for i in range(1, 3):
        later = _NOW + timedelta(minutes=i)
        run_guardian_authority_shadow_tick(
            repo, [position], [], price_lookup, later, settings, f"run-{i + 1}"
        )
        row = _shadow_row(repo)
        assert row["status"] == "DECIDED"
        assert row["shadow_decision"] == "TIGHTEN_SL"
        assert row["decided_at"] == first_decided_at
        assert row["proposed_new_sl"] == first_proposed_sl


def test_tick_abandons_shadow_when_position_vanishes_from_open_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="HOLD", factors={})
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}
    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")
    assert _shadow_row(repo)["status"] == "OBSERVING"

    later = _NOW + timedelta(minutes=1)
    run_guardian_authority_shadow_tick(repo, [], [], {}, later, settings, "run-2")

    row = _shadow_row(repo)
    assert row["status"] == "ABANDONED"


def test_tick_does_not_abandon_for_a_merely_transient_missing_candle(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="HOLD", factors={})
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}
    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")

    later = _NOW + timedelta(minutes=1)
    run_guardian_authority_shadow_tick(repo, [position], [], {}, later, settings, "run-2")

    row = _shadow_row(repo)
    assert row["status"] == "OBSERVING"  # still open, just no candle this tick


def test_tick_isolates_one_shadows_advance_failure_from_the_rest(tmp_path, monkeypatch, caplog):
    import crypto_trading.paper_trading.guardian_authority_shadow as ga_shadow_module

    repo = SQLiteRepository(tmp_path / "t.db")
    position_1 = _seed_real_position(repo, position_id="pos-1", instrument="BTCUSDT")
    position_2 = _seed_real_position(repo, position_id="pos-2", instrument="ETHUSDT")
    _seed_observation(repo, "pos-1", state="HOLD", factors={})
    _seed_observation(repo, "pos-2", state="HOLD", factors={})
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {
        # Both positions share _position()'s hardcoded theoretical_entry
        # (50000) regardless of instrument, so both price ranges are
        # centered on 50000 too - the point of this test is failure
        # isolation, not per-instrument price realism.
        "BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0")),
        "ETHUSDT": (Decimal("49900"), Decimal("50100"), Decimal("50000"), Decimal("0")),
    }
    real_advance_shadow = ga_shadow_module.advance_shadow

    def _flaky_advance_shadow(shadow, *args, **kwargs):
        if shadow["position_id"] == "pos-1":
            raise ValueError("boom")
        return real_advance_shadow(shadow, *args, **kwargs)

    monkeypatch.setattr(ga_shadow_module, "advance_shadow", _flaky_advance_shadow)

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        ga_shadow_module.run_guardian_authority_shadow_tick(
            repo, [position_1, position_2], [], price_lookup, _NOW, settings, "run-1"
        )  # must not raise despite pos-1's advance always failing

    row_1 = _shadow_row(repo, "pos-1")
    row_2 = _shadow_row(repo, "pos-2")
    assert row_1["status"] == "OBSERVING"
    assert row_1["mfe"] == "0"  # untouched by the failed advance
    assert row_2["status"] == "OBSERVING"
    assert row_2["mfe"] == "100"  # 50100 - 50000, the OTHER shadow still processed normally
    assert "guardian_authority_shadow_advance_failed" in caplog.text
    assert "pos-1" in caplog.text


def test_tick_resolves_closed_position_observing_end_to_end(tmp_path):
    """The core cold-start proof, driven through the full orchestrator: a
    position sitting in OBSERVING for its entire life (empty heuristics,
    hypothetical NO_ACTION on every tick) produces exactly one resolved
    shadow row once it closes - not zero, not N."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="HOLD", factors={})
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}

    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")
    assert _shadow_row(repo)["status"] == "OBSERVING"

    # Real production wiring (monitoring_loop.py): `open_positions` is the
    # PRE-close snapshot captured BEFORE close_triggered_positions runs, so
    # the position closing THIS tick is present in BOTH `open_positions`
    # (still status OPEN_POSITION, unchanged) and `closed_positions` (the
    # post-close Position) simultaneously - never absent from open_positions
    # on its own closing tick (that would wrongly read as an orphan/stranded
    # shadow and get ABANDONED before step 3 ever resolves it).
    closed_position = _closed_position(position, exit_reason="target", pnl_favorable=True)
    later = _NOW + timedelta(minutes=1)
    run_guardian_authority_shadow_tick(
        repo, [position], [closed_position], {}, later, settings, "run-2"
    )

    row = _shadow_row(repo)
    assert row["status"] == "RESOLVED"
    assert row["shadow_decision"] == "NO_ACTION"
    assert row["actual_exit_reason"] == "target"
    resolved_rows = repo.find_resolved_guardian_authority_shadows()
    assert len(resolved_rows) == 1  # exactly one row for this position, not zero, not N


def test_tick_resolves_closed_position_decided_end_to_end(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="WATCH", factors={})
    _seed_always_on_heuristic(repo, adjustment=0.2)
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}

    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")
    assert _shadow_row(repo)["shadow_decision"] == "TIGHTEN_SL"

    closed_position = _closed_position(position, exit_reason="target", pnl_favorable=True)
    later = _NOW + timedelta(minutes=1)
    run_guardian_authority_shadow_tick(
        repo, [position], [closed_position], {}, later, settings, "run-2"
    )

    row = _shadow_row(repo)
    assert row["status"] == "RESOLVED"
    assert row["shadow_decision"] == "TIGHTEN_SL"  # immutable, set at decide time
    assert bool(row["expectation_correct"]) is True


def test_tick_never_writes_to_positions_or_real_guardian_authority_decisions_tables(tmp_path):
    """Global Constraints / AC1: real PAPER positions and the REAL decision
    table must be byte-identically unaffected by this module."""
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _seed_real_position(repo)
    _seed_observation(repo, "pos-1", state="EXIT", factors={})
    _seed_always_on_heuristic(repo, adjustment=0.9)  # would CLOSE_EARLY if it were real
    settings = _settings_with_shadow(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50300"), Decimal("50000"), Decimal("0"))}

    before = repo.get_position("pos-1")
    run_guardian_authority_shadow_tick(repo, [position], [], price_lookup, _NOW, settings, "run-1")
    after = repo.get_position("pos-1")

    assert before == after
    assert after.status == "OPEN_POSITION"
    real_decisions = repo._conn.execute("SELECT * FROM guardian_authority_decisions").fetchall()
    assert real_decisions == []
    real_heuristics_only_the_always_on_one = repo._conn.execute(
        "SELECT * FROM guardian_authority_heuristics"
    ).fetchall()
    assert len(real_heuristics_only_the_always_on_one) == 1  # the one WE seeded, untouched/unadded-to


# ---------------------------------------------------------------------------
# Task 8: update_shadow_heuristics_from_resolved_shadow_observations
#
# Same fixture-building approach as guardian/authority.py's own Task 9 test
# suite (test_authority.py) - resolved rows are built directly via the
# repo's own state-machine methods (seed -> decide -> resolve), not through
# a full run_guardian_authority_shadow_tick(), for deterministic,
# hand-verifiable sample sizes at exactly the _MIN_SAMPLE_SIZE boundary.
# ---------------------------------------------------------------------------

_MID_FACTORS = {
    "time_decay": 0.5,
    "momentum_decay": 0.5,
    "volume_decay": 0.5,
    "funding_decay": 0.5,
    "secondary_confirmation_lost": 0.5,
    "market_regime": 0.5,
}


def _resolved_tighten_sl_shadow(
    repo, idx: int, guardian_state: str, factors: dict, correct: bool, prefix: str = "shd"
) -> None:
    """Builds ONE resolved (status='RESOLVED') shadow row with
    shadow_decision='TIGHTEN_SL' and a real expectation_correct - the only
    row shape update_shadow_heuristics_from_resolved_shadow_observations
    tallies. shadow_id/position_id are unique per row (shadow_id is this
    table's PRIMARY KEY, 1:1 with position_id) so 30 rows means 30 distinct
    positions, exactly like the real Task 9 fixture's own distinct
    decision_ids per row."""
    position_id = f"{prefix}-{idx}"
    now = _NOW + timedelta(minutes=idx)
    factors_json = json.dumps({**factors, "guardian_state": guardian_state})
    repo.seed_guardian_authority_shadow(
        shadow_id=position_id, position_id=position_id, candidate_id=position_id,
        instrument="BTCUSDT", opened_at=now, created_at=now, run_id="run-1",
    )
    repo.decide_guardian_authority_shadow(
        shadow_id=position_id, decision="TIGHTEN_SL", decided_at=now,
        expected_outcome="expect small favorable move", expected_direction="favorable",
        confidence=0.7, factors_json=factors_json, proposed_new_sl=Decimal("105"),
        updated_at=now,
    )
    repo.resolve_guardian_authority_shadow_decided(
        shadow_id=position_id,
        actual_exit_reason="target" if correct else "stop_loss",
        actual_pnl_usdt=Decimal("10") if correct else Decimal("-10"),
        actual_closed_at=now + timedelta(hours=1),
        expectation_correct=correct,
        prediction_error=0.0,
        updated_at=now,
    )


def _resolved_close_early_shadow(repo, idx: int, guardian_state: str, factors: dict, prefix: str = "ce") -> None:
    """A resolved CLOSE_EARLY shadow row - per Task 8's own ruling (reused
    here verbatim for the shadow table), CLOSE_EARLY always resolves with
    expectation_correct=None (no counterfactual-of-inaction mechanism), so
    this must never be tallied."""
    position_id = f"{prefix}-{idx}"
    now = _NOW + timedelta(minutes=idx)
    factors_json = json.dumps({**factors, "guardian_state": guardian_state})
    repo.seed_guardian_authority_shadow(
        shadow_id=position_id, position_id=position_id, candidate_id=position_id,
        instrument="BTCUSDT", opened_at=now, created_at=now, run_id="run-1",
    )
    repo.decide_guardian_authority_shadow(
        shadow_id=position_id, decision="CLOSE_EARLY", decided_at=now,
        expected_outcome="expect unfavorable if left open", expected_direction="unfavorable",
        confidence=0.9, factors_json=factors_json, proposed_new_sl=None,
        updated_at=now,
    )
    repo.resolve_guardian_authority_shadow_decided(
        shadow_id=position_id, actual_exit_reason="GUARDIAN_EXIT",
        actual_pnl_usdt=Decimal("5"), actual_closed_at=now + timedelta(hours=1),
        expectation_correct=None, prediction_error=None, updated_at=now,
    )


def _resolved_no_action_shadow(repo, idx: int, guardian_state: str, prefix: str = "na") -> None:
    """A resolved NO_ACTION shadow row (position closed while the shadow
    was still OBSERVING) - resolve_guardian_authority_shadow_no_action
    always leaves expectation_correct as SQL NULL, so this must never be
    tallied either."""
    position_id = f"{prefix}-{idx}"
    now = _NOW + timedelta(minutes=idx)
    repo.seed_guardian_authority_shadow(
        shadow_id=position_id, position_id=position_id, candidate_id=position_id,
        instrument="BTCUSDT", opened_at=now, created_at=now, run_id="run-1",
    )
    repo.resolve_guardian_authority_shadow_no_action(
        shadow_id=position_id,
        factors_json=json.dumps({"guardian_state": guardian_state}),
        actual_exit_reason="target", actual_pnl_usdt=Decimal("10"),
        actual_closed_at=now + timedelta(hours=1), updated_at=now,
    )


def test_update_shadow_heuristics_miscalibrated_group_gets_negative_adjustment(tmp_path):
    """Mirrors test_update_heuristics_miscalibrated_group_gets_negative_
    adjustment in test_authority.py: 30 resolved TIGHTEN_SL shadow rows, all
    wrong, under one guardian_state -> a SHADOW heuristic row with a
    NEGATIVE adjustment, written to the shadow table only."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(30):
        _resolved_tighten_sl_shadow(repo, i, "PROTECT", _MID_FACTORS, correct=False, prefix="shd-neg")

    updated = update_shadow_heuristics_from_resolved_shadow_observations(repo, _NOW + timedelta(days=1))

    assert updated == 1
    shadow_heuristics = {h["heuristic_id"]: h for h in repo.find_guardian_authority_shadow_heuristics()}
    assert "ga-hc:state:PROTECT" in shadow_heuristics
    heuristic = shadow_heuristics["ga-hc:state:PROTECT"]
    assert heuristic["adjustment"] < 0
    assert heuristic["sample_size"] == 30
    assert json.loads(heuristic["condition_json"]) == {"guardian_state": "PROTECT"}
    # Must never touch the REAL heuristics table.
    assert repo.find_guardian_authority_heuristics() == []


def test_update_shadow_heuristics_well_calibrated_group_gets_positive_adjustment(tmp_path):
    """Mirrors the real function's own positive-adjustment test."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(30):
        _resolved_tighten_sl_shadow(repo, i, "WATCH", _MID_FACTORS, correct=True, prefix="shd-pos")

    update_shadow_heuristics_from_resolved_shadow_observations(repo, _NOW + timedelta(days=1))

    shadow_heuristics = {h["heuristic_id"]: h for h in repo.find_guardian_authority_shadow_heuristics()}
    assert "ga-hc:state:WATCH" in shadow_heuristics
    assert shadow_heuristics["ga-hc:state:WATCH"]["adjustment"] > 0
    assert repo.find_guardian_authority_heuristics() == []


def test_update_shadow_heuristics_respects_minimum_sample_size_threshold(tmp_path):
    """Boundary test: 29 resolved TIGHTEN_SL shadow rows for one state does
    NOT produce a heuristic; 30 for another state does."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(29):
        _resolved_tighten_sl_shadow(repo, i, "HOLD", _MID_FACTORS, correct=False, prefix="shd-29")
    for i in range(30):
        _resolved_tighten_sl_shadow(repo, i, "EXIT", _MID_FACTORS, correct=False, prefix="shd-30")

    update_shadow_heuristics_from_resolved_shadow_observations(repo, _NOW + timedelta(days=1))

    heuristic_ids = {h["heuristic_id"] for h in repo.find_guardian_authority_shadow_heuristics()}
    assert "ga-hc:state:HOLD" not in heuristic_ids  # 29 < threshold
    assert "ga-hc:state:EXIT" in heuristic_ids  # 30 >= threshold


def test_update_shadow_heuristics_is_idempotent_on_rerun(tmp_path):
    """Same idempotent-REPLACE proof as the real function's own test."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(30):
        _resolved_tighten_sl_shadow(repo, i, "PROTECT", _MID_FACTORS, correct=False, prefix="shd-idem")

    update_shadow_heuristics_from_resolved_shadow_observations(repo, _NOW + timedelta(days=1))
    first_count = len(repo.find_guardian_authority_shadow_heuristics())

    second_updated_at = _NOW + timedelta(days=2)
    update_shadow_heuristics_from_resolved_shadow_observations(repo, second_updated_at)
    second_count = len(repo.find_guardian_authority_shadow_heuristics())

    assert first_count == second_count
    shadow_heuristics = {h["heuristic_id"]: h for h in repo.find_guardian_authority_shadow_heuristics()}
    assert shadow_heuristics["ga-hc:state:PROTECT"]["updated_at"] == second_updated_at.isoformat()


def test_update_shadow_heuristics_excludes_close_early_and_no_action_rows(tmp_path):
    """CLOSE_EARLY and NO_ACTION shadow rows both resolve with
    expectation_correct=None (no counterfactual to learn from, same Task 8
    ruling as the real table) - neither may contribute to any tally, and
    together with < _MIN_SAMPLE_SIZE genuine rows nothing crosses the
    threshold, so no heuristic is produced at all."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(40):
        _resolved_close_early_shadow(repo, i, "EXIT", _MID_FACTORS, prefix="ce-excl")
    for i in range(40):
        _resolved_no_action_shadow(repo, i, "HOLD", prefix="na-excl")

    updated = update_shadow_heuristics_from_resolved_shadow_observations(repo, _NOW + timedelta(days=1))

    assert updated == 0
    assert repo.find_guardian_authority_shadow_heuristics() == []


def test_update_shadow_heuristics_isolation_shadow_data_has_zero_effect_on_real_engine(tmp_path):
    """THE isolation proof (task-8-brief.md's HARD REQUIREMENT): seed a
    strongly miscalibrated shadow heuristic via
    update_shadow_heuristics_from_resolved_shadow_observations (guaranteed
    to produce a heuristic that, if it ever leaked into the real engine,
    would flip decide_open_position's decision away from its NO_ACTION
    default and decide_pre_entry's away from APPROVE), then call the REAL
    decide_open_position/decide_pre_entry with an EMPTY real-heuristics
    table and assert the decision is still the plain default - i.e. the
    shadow heuristics genuinely have zero effect on the real engine, not
    merely 'the function wasn't called'."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(30):
        _resolved_tighten_sl_shadow(repo, i, "PROTECT", _MID_FACTORS, correct=False, prefix="shd-iso")

    updated = update_shadow_heuristics_from_resolved_shadow_observations(repo, _NOW + timedelta(days=1))
    assert updated == 1  # sanity: the shadow heuristic really was written
    shadow_heuristic = repo.find_guardian_authority_shadow_heuristics()[0]
    assert abs(shadow_heuristic["adjustment"]) > 0  # a real, non-zero learned adjustment exists

    # The REAL heuristics table is untouched/empty - decide_open_position
    # and decide_pre_entry only ever read find_guardian_authority_heuristics().
    real_heuristics = repo.find_guardian_authority_heuristics()
    assert real_heuristics == []

    decision, _, _, _, proposed_sl = decide_open_position(
        position_factors=_MID_FACTORS,
        guardian_state="PROTECT",  # exact same condition the shadow heuristic matches
        current_sl=Decimal("49000"),
        entry=Decimal("50000"),
        heuristics=real_heuristics,
        tighten_threshold=0.15,
        close_threshold=0.45,
    )
    assert decision == "NO_ACTION"
    assert proposed_sl is None

    pre_entry_decision, _, pre_entry_direction, pre_entry_confidence = decide_pre_entry(
        candidate_evidence={"guardian_state": "PROTECT", **_MID_FACTORS},
        heuristics=real_heuristics,
        veto_threshold=0.15,
    )
    assert pre_entry_decision == "APPROVE"
    assert pre_entry_direction == "neutral"
    assert pre_entry_confidence == 1.0
