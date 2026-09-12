from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.performance.profit_protection_report import (
    _BREAKEVEN_BAND_PCT,
    _classify_reach,
    _conversion_ratio,
    _sample_sizes,
    build_report,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _row(**overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", status="CLOSED", threshold_reached=1,
        entry_price="50000", mfe="1000", mae="-200",
        shadow_realized_pnl="300", hypothetical_baseline_exit_reason="target",
        hypothetical_baseline_pnl="1000", exit_reason="stop_loss",
    )
    defaults.update(overrides)
    return defaults


def test_classify_reach_never_reached(tmp_path):
    row = _row(threshold_reached=0, hypothetical_baseline_pnl=None,
               hypothetical_baseline_exit_reason=None)
    assert _classify_reach(row, position_size=Decimal("5000")) == "never_reached_threshold"


def test_classify_reach_loss(tmp_path):
    row = _row(hypothetical_baseline_pnl="-500")
    assert _classify_reach(row, position_size=Decimal("5000")) == "reached_threshold_baseline_loss"


def test_classify_reach_approx_breakeven(tmp_path):
    row = _row(hypothetical_baseline_pnl="10")  # 10/5000 = 0.002 <= 0.003 band
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_approx_breakeven"
    )


def test_classify_reach_big_winner(tmp_path):
    row = _row(hypothetical_baseline_pnl="1000", hypothetical_baseline_exit_reason="target")
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_big_winner"
    )


def test_classify_reach_moderate_gain(tmp_path):
    row = _row(hypothetical_baseline_pnl="200", hypothetical_baseline_exit_reason="time_limit")
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_moderate_gain"
    )


def test_classify_reach_pending_when_baseline_not_yet_known(tmp_path):
    row = _row(hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None)
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_pending"
    )


def test_classification_buckets_are_exhaustive_and_mutually_exclusive():
    known_buckets = {
        "never_reached_threshold", "reached_threshold_baseline_loss",
        "reached_threshold_baseline_approx_breakeven", "reached_threshold_baseline_big_winner",
        "reached_threshold_baseline_moderate_gain", "reached_threshold_baseline_pending",
    }
    scenarios = [
        _row(threshold_reached=0, hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None),
        _row(hypothetical_baseline_pnl="-1"),
        _row(hypothetical_baseline_pnl="0"),
        _row(hypothetical_baseline_pnl="5000", hypothetical_baseline_exit_reason="target"),
        _row(hypothetical_baseline_pnl="200", hypothetical_baseline_exit_reason="time_limit"),
        _row(hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None, threshold_reached=1),
    ]
    labels = [_classify_reach(row, position_size=Decimal("5000")) for row in scenarios]
    assert set(labels) <= known_buckets
    assert len(labels) == len(scenarios)  # one label per row, none skipped/duplicated


def test_conversion_ratio_is_none_when_mfe_not_positive():
    row = _row(mfe="0")
    assert _conversion_ratio(row, position_size=Decimal("5000"), entry_price=Decimal("50000")) is None


def test_conversion_ratio_dimensionless_formula():
    row = _row(mfe="500", shadow_realized_pnl="250")  # mfe_pct=0.01, pnl_pct=0.05
    ratio = _conversion_ratio(row, position_size=Decimal("5000"), entry_price=Decimal("50000"))
    assert ratio == Decimal("5")  # 0.05 / 0.01


def test_sample_sizes_counts_match_definitions():
    rows = [
        _row(shadow_id="a", threshold_reached=0, hypothetical_baseline_pnl=None,
             hypothetical_baseline_exit_reason=None, shadow_realized_pnl="0"),
        _row(shadow_id="b", hypothetical_baseline_pnl="-500", shadow_realized_pnl="0"),
        _row(shadow_id="c", hypothetical_baseline_exit_reason="target", shadow_realized_pnl="300"),
        _row(shadow_id="d", shadow_realized_pnl="-50", hypothetical_baseline_pnl="-50"),
    ]
    sizes = _sample_sizes(rows)
    assert sizes["n_closed"] == 4
    assert sizes["n_reached_threshold"] == 3
    assert sizes["n_not_reached"] == 1
    assert sizes["n_baseline_losses_after_threshold"] == 2  # b and d
    assert sizes["n_baseline_target_winners_after_threshold"] == 1  # c
    assert sizes["n_shadow_winners"] == 1  # c
    assert sizes["n_shadow_losses"] == 1  # d


def test_report_note_is_always_present_regardless_of_data(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    report = build_report(repo)
    assert "Pre-registered hypotheses under test" in report["note"]
    assert "never selects a winner" in report["note"]
