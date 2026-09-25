"""Tests for crypto_trading/godfather/path.py - the real price path.

Every later conclusion in this subsystem is derived from this
reconstruction, so the exactness property is tested directly rather than
assumed: a price put into a Guardian observation must come back out
identical.
"""

from datetime import timedelta
from decimal import Decimal

from crypto_trading.godfather.path import (
    compute_path_metrics,
    reconstruct_price_path,
)
from tests.crypto_trading.godfather.intelligence_fixtures import (
    make_position,
    observation_row,
)

_WATCH = Decimal("0.35")
_EXIT = Decimal("0.75")


def test_reconstruction_recovers_the_exact_price_guardian_observed():
    position = make_position()
    prices = [Decimal("100"), Decimal("103.5"), Decimal("98.25")]
    rows = [observation_row(position, i * 10, price) for i, price in enumerate(prices)]

    points = reconstruct_price_path(position, rows)

    assert [p.price for p in points] == prices
    assert all(p.price_cross_check_ok for p in points)


def test_reconstruction_falls_back_to_progress_ratio_for_a_zero_size_position():
    """An exposure-blocked position has size 0, so the P/L inversion
    divides by zero. The progress-ratio inversion still works, and losing
    the path of those trades entirely would silently drop them from every
    later statistic."""
    position = make_position(size=Decimal("0"))
    row = observation_row(position, 10, Decimal("105"))

    points = reconstruct_price_path(position, row and [row])

    assert len(points) == 1
    assert points[0].price == Decimal("105")


def test_reconstruction_drops_observations_recorded_after_the_close():
    """34 of 122 real positions have at least one Guardian tick landing
    after close. Keeping them would feed post-close prices into MFE/MAE
    and into the counterfactual engine's notion of "known at time T"."""
    position = make_position()
    inside = observation_row(position, 10, Decimal("104"))
    after_close = observation_row(position, 10_000, Decimal("130"))

    points = reconstruct_price_path(position, [inside, after_close])

    assert len(points) == 1
    assert points[0].price == Decimal("104")


def test_reconstruction_drops_unparseable_rows_instead_of_guessing():
    position = make_position()
    good = observation_row(position, 5, Decimal("101"))
    broken = {**observation_row(position, 6, Decimal("102")), "unrealized_pnl": "not-a-number"}
    undated = {**observation_row(position, 7, Decimal("103")), "observed_at": "nonsense"}

    points = reconstruct_price_path(position, [good, broken, undated])

    assert len(points) == 1


def test_reconstruction_returns_points_in_chronological_order():
    position = make_position()
    rows = [
        observation_row(position, 30, Decimal("102")),
        observation_row(position, 10, Decimal("101")),
        observation_row(position, 20, Decimal("103")),
    ]

    points = reconstruct_price_path(position, rows)

    assert [p.minutes_in_trade for p in points] == [10, 20, 30]


def test_metrics_capture_mfe_mae_and_time_to_each():
    position = make_position()
    rows = [
        observation_row(position, 0, Decimal("100")),
        observation_row(position, 30, Decimal("106")),
        observation_row(position, 60, Decimal("94")),
        observation_row(position, 90, Decimal("99")),
    ]

    metrics = compute_path_metrics(
        position, reconstruct_price_path(position, rows), _WATCH, _EXIT
    )

    assert metrics.mfe_price == Decimal("106")
    assert metrics.mae_price == Decimal("94")
    assert metrics.minutes_to_mfe == 30
    assert metrics.minutes_to_mae == 60
    assert metrics.mfe_pct == Decimal("6")
    assert metrics.mae_pct == Decimal("-6")


def test_metrics_report_giveback_of_a_favourable_move():
    position = make_position()
    rows = [
        observation_row(position, 0, Decimal("100")),
        observation_row(position, 30, Decimal("110")),
        observation_row(position, 60, Decimal("105")),
    ]

    metrics = compute_path_metrics(
        position, reconstruct_price_path(position, rows), _WATCH, _EXIT
    )

    # Half of a +100 USDT unrealized gain handed back.
    assert metrics.giveback_ratio == Decimal("0.5")
    assert metrics.minutes_since_mfe_at_close == 30


def test_metrics_leave_giveback_undefined_when_the_trade_never_went_favourable():
    position = make_position()
    rows = [observation_row(position, m, Decimal("98")) for m in (0, 30, 60)]

    metrics = compute_path_metrics(
        position, reconstruct_price_path(position, rows), _WATCH, _EXIT
    )

    assert metrics.giveback_ratio is None


def test_metrics_record_when_the_position_first_became_questionable_and_invalid():
    position = make_position()
    rows = [
        observation_row(position, 0, Decimal("100"), decay="0.1"),
        observation_row(position, 30, Decimal("99"), decay="0.4"),
        observation_row(position, 60, Decimal("96"), decay="0.9"),
    ]

    metrics = compute_path_metrics(
        position, reconstruct_price_path(position, rows), _WATCH, _EXIT
    )

    assert metrics.first_questionable_minutes == 30
    assert metrics.first_invalid_minutes == 60


def test_metrics_detect_target_and_stop_touches():
    position = make_position()
    rows = [
        observation_row(position, 10, Decimal("111")),
        observation_row(position, 20, Decimal("94")),
    ]

    metrics = compute_path_metrics(
        position, reconstruct_price_path(position, rows), _WATCH, _EXIT
    )

    assert metrics.minutes_to_target_touch == 10
    assert metrics.minutes_to_sl_touch == 20


def test_metrics_of_an_empty_path_are_all_unknown_rather_than_zero():
    metrics = compute_path_metrics(make_position(), [], _WATCH, _EXIT)

    assert metrics.point_count == 0
    assert metrics.mfe_pct is None
    assert metrics.mae_pct is None
    assert metrics.giveback_ratio is None


def test_short_position_signs_are_reported_in_the_positions_own_favour():
    """A SHORT that fell has a POSITIVE favourable excursion, so a LONG
    and a SHORT read identically downstream."""
    position = make_position(
        direction="SHORT", stop_loss=Decimal("105"), target=Decimal("90")
    )
    rows = [
        observation_row(position, 0, Decimal("100")),
        observation_row(position, 30, Decimal("95")),
    ]
    points = reconstruct_price_path(position, rows)

    metrics = compute_path_metrics(position, points, _WATCH, _EXIT)

    assert metrics.mfe_price == Decimal("95")
    assert metrics.mfe_pct == Decimal("5")


def test_path_points_are_stamped_with_minutes_since_the_position_opened():
    position = make_position()
    row = observation_row(position, 45, Decimal("102"))

    points = reconstruct_price_path(position, [row])

    assert points[0].minutes_in_trade == 45
    assert points[0].observed_at == position.opened_at + timedelta(minutes=45)
