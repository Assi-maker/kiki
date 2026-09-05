from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.paper_trading.monitoring import check_exit_trigger, compute_hold_hours
from crypto_trading.schemas.trade import Position

_OPENED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _position(stop_loss="49000", target="52000") -> Position:
    return Position(
        position_id="pos-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="50000",
        simulated_fill_entry="50025",
        stop_loss=stop_loss,
        target=target,
        size="5000",
        fill_model_version="v1",
        opened_at=_OPENED_AT,
    )


def test_no_trigger_when_price_stays_within_range():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49500"),
        candle_high=Decimal("50500"),
        current_price=Decimal("50000"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result is None


def test_stop_loss_triggers_at_exact_touch():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49000"),
        candle_high=Decimal("50100"),
        current_price=Decimal("49500"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("stop_loss", Decimal("49000"))


def test_target_triggers_at_exact_touch():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49900"),
        candle_high=Decimal("52000"),
        current_price=Decimal("51500"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("target", Decimal("52000"))


def test_time_limit_triggers_after_max_hold_hours():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49900"),
        candle_high=Decimal("50100"),
        current_price=Decimal("50050"),
        now=_OPENED_AT + timedelta(hours=25),
        max_position_hold_hours=24,
    )
    assert result == ("time_limit", Decimal("50050"))  # current_price är referenspriset


def test_no_time_limit_trigger_before_max_hold_hours():
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("49900"),
        candle_high=Decimal("50100"),
        current_price=Decimal("50050"),
        now=_OPENED_AT + timedelta(hours=23),
        max_position_hold_hours=24,
    )
    assert result is None


def test_gap_down_through_stop_loss_fills_at_candle_low_not_stop_level():
    """AC5, gap ned: priset gappar långt under stop_loss (49000) mellan två
    övervakningstillfällen - fill-priset ska vara candle_low (49500... nej,
    lägre än stop), aldrig den exakta stop-nivån."""
    result = check_exit_trigger(
        _position(stop_loss="49000"),
        candle_low=Decimal("47500"),  # gappade långt under stop
        candle_high=Decimal("49800"),
        current_price=Decimal("47600"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("stop_loss", Decimal("47500"))
    exit_reason, trigger_price = result
    assert trigger_price != Decimal("49000")  # aldrig exakt SL-nivån vid gap
    assert trigger_price < Decimal("49000")  # strikt sämre än stop, konservativt


def test_gap_up_through_target_fills_at_target_not_candle_high():
    """AC5, gap upp: priset gappar långt över target (52000) mellan två
    övervakningstillfällen - fill-priset ska vara target, aldrig det
    gynnsamma extremvärdet (candle_high)."""
    result = check_exit_trigger(
        _position(target="52000"),
        candle_low=Decimal("51800"),
        candle_high=Decimal("54000"),  # gappade långt över target
        current_price=Decimal("53900"),
        now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result == ("target", Decimal("52000"))
    exit_reason, trigger_price = result
    assert trigger_price != Decimal("54000")  # aldrig det gynnsamma extremvärdet
    assert trigger_price == Decimal("52000")  # konservativt: aldrig bättre än target


def test_fill_model_version_is_available_for_the_resulting_position():
    """Påminnelse: fill_model_version sätts på Position-objektet av
    position_opening.py/position_closing.py (Task 7/8) - check_exit_trigger
    själv rör inte Position-persistens, bara beslutet."""
    from crypto_trading.paper_trading.execution import FILL_MODEL_VERSION

    assert FILL_MODEL_VERSION == "v1"


def test_stop_loss_checked_before_time_limit_when_both_true():
    """Deterministisk prioritetsordning: SL/TP kollas alltid före tidsgräns."""
    result = check_exit_trigger(
        _position(),
        candle_low=Decimal("48000"),  # gappar under stop
        candle_high=Decimal("50100"),
        current_price=Decimal("48500"),
        now=_OPENED_AT + timedelta(hours=25),  # också över tidsgränsen
        max_position_hold_hours=24,
    )
    exit_reason, _trigger_price = result
    assert exit_reason == "stop_loss"


def test_compute_hold_hours_matches_elapsed_time():
    now = _OPENED_AT + timedelta(hours=2, minutes=30)

    hours = compute_hold_hours(_position(), now)

    assert hours == Decimal("2.5")


# --- Guardian-assisted exit (2026-09-05) ---
# Prioritetsordning: stop_loss -> target -> time_limit -> guardian_exit.
# guardian_exit stänger ENDAST när guardian_assisted_exit_enabled=True OCH
# guardian_state=="EXIT" - se check_exit_trigger()s docstring.

_WITHIN_RANGE = dict(
    candle_low=Decimal("49900"), candle_high=Decimal("50100"), current_price=Decimal("50000")
)


def test_strong_edge_hold_state_never_exits_even_when_feature_enabled():
    """Stark edge (HOLD) -> ingen exit, oavsett att funktionen är påslagen."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24, guardian_state="HOLD", guardian_assisted_exit_enabled=True,
    )
    assert result is None


def test_weakening_edge_watch_state_never_exits():
    """WATCH = fortsatt bevakning, ingen stängning (punkt 5)."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24, guardian_state="WATCH", guardian_assisted_exit_enabled=True,
    )
    assert result is None


def test_protect_state_alone_never_exits_deterministic_criteria_not_met():
    """PROTECT = tydlig försämring, men stängning kräver de deterministiska
    exit-kriterierna (state=="EXIT") - PROTECT i sig stänger ALDRIG
    (punkt 6), även med funktionen påslagen och positionen inom SL/TP/tid."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24, guardian_state="PROTECT", guardian_assisted_exit_enabled=True,
    )
    assert result is None


def test_clearly_deteriorated_edge_exit_state_can_trigger_guardian_exit():
    """Tydligt försämrad edge (EXIT) -> Guardian kan ge en exit-signal
    (punkt 7), när funktionen är aktiverad och inom time limit."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24, guardian_state="EXIT", guardian_assisted_exit_enabled=True,
    )
    assert result == ("guardian_exit", Decimal("50000"))


def test_exit_state_never_triggers_when_feature_flag_disabled():
    """Shadow-mode tills funktionen aktiveras separat: state=="EXIT" men
    guardian_assisted_exit_enabled=False (default) -> ingen stängning."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24, guardian_state="EXIT", guardian_assisted_exit_enabled=False,
    )
    assert result is None


def test_exit_state_never_triggers_by_default_without_passing_guardian_args():
    """Bakåtkompatibilitet: en anropare som inte känner till funktionen alls
    (inga guardian-argument) får exakt samma beteende som innan ändringen."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24,
    )
    assert result is None


def test_guardian_cannot_extend_position_past_the_hard_time_limit():
    """Guardian får aldrig förlänga en position förbi den hårda time limit
    (punkt: absolut säkerhetsgräns) - även om Guardian-state vore "HOLD"
    (dvs. "behåll") vid exakt tidsgränsen stänger time_limit-logiken ändå."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=25),
        max_position_hold_hours=24, guardian_state="HOLD", guardian_assisted_exit_enabled=True,
    )
    assert result == ("time_limit", Decimal("50000"))


def test_time_limit_wins_over_guardian_exit_when_both_conditions_are_true():
    """Time limit fungerar som absolut fallback: när BÅDE tidsgränsen är
    nådd OCH Guardian-state är "EXIT" samtidigt, vinner time_limit -
    guardian_exit kan strukturellt aldrig nås efter tidsgränsen."""
    result = check_exit_trigger(
        _position(), **_WITHIN_RANGE, now=_OPENED_AT + timedelta(hours=25),
        max_position_hold_hours=24, guardian_state="EXIT", guardian_assisted_exit_enabled=True,
    )
    exit_reason, _ = result
    assert exit_reason == "time_limit"


def test_stop_loss_still_checked_before_guardian_exit():
    """SL/TP har alltid högst prioritet, oförändrat av den nya funktionen."""
    result = check_exit_trigger(
        _position(), candle_low=Decimal("48000"), candle_high=Decimal("50100"),
        current_price=Decimal("48500"), now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24, guardian_state="EXIT", guardian_assisted_exit_enabled=True,
    )
    exit_reason, _ = result
    assert exit_reason == "stop_loss"


def test_positive_pnl_alone_does_not_cause_guardian_exit():
    """Positiv PnL ensam orsakar inte exit: guardian_state kommer alltid
    från den redan deterministiska klassificeringen (classify_guardian_state,
    som aldrig ger EXIT bara av positiv PnL - se test_deterministic.py). Här:
    priset ligger nära target (positiv PnL), men Guardian-state är "PROTECT"
    (inte "EXIT") -> ingen stängning, exakt som om PnL vore negativ."""
    result = check_exit_trigger(
        _position(), candle_low=Decimal("51000"), candle_high=Decimal("51900"),
        current_price=Decimal("51800"), now=_OPENED_AT + timedelta(hours=1),
        max_position_hold_hours=24, guardian_state="PROTECT", guardian_assisted_exit_enabled=True,
    )
    assert result is None
