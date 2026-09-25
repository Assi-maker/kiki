"""Fas 2A.1: reconstruction from exchange history, separated timestamps,
partial_error handling, and the kline archive.

Pinned: a replay never jumps over a missing minute; nothing before the
position existed enters its path; signal/decision/creation/claim/fill and
planned/actual entry never collapse into one another; a monitoring run
that failed for an instrument makes that trade's paper exit unverified
unless exchange candles verify it.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather.book import _overlaps, load_partial_errors
from crypto_trading.godfather.observation import classify_observation
from crypto_trading.godfather.reconstruction import (
    Candle,
    TradeTimeline,
    candle_path,
    replay_exit,
    verify_exit,
)
from crypto_trading.kline_archive import fetch_window, is_covered, position_window
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.godfather.intelligence_fixtures import make_position, path_point

T0 = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _c(minute, low, high, close=None, open_=None):
    close = close if close is not None else (low + high) / 2
    opening = open_ if open_ is not None else close
    return Candle(T0 + timedelta(minutes=minute), Decimal(str(opening)),
                  Decimal(str(high)), Decimal(str(low)), Decimal(str(close)))


def _flat(minutes, low=99.5, high=100.5):
    return [_c(m, low, high) for m in range(minutes)]


# ---------------------------------------------------------------------
# Exit replay and verification
# ---------------------------------------------------------------------


def _replay(candles, deadline_minutes=10_000):
    return replay_exit(
        candles, T0, T0 + timedelta(minutes=len(candles) + 5), Decimal("95"), Decimal("110"),
        T0 + timedelta(minutes=deadline_minutes),
    )


def test_replay_finds_the_stop_first_and_fills_at_the_candle_low():
    candles = _flat(30) + [_c(30, 94, 101)] + [_c(31, 105, 111)]
    replay = _replay(candles)
    assert (replay.reason, replay.exit_time, replay.exit_price) == (
        "stop_loss", T0 + timedelta(minutes=30), Decimal("94")
    )


def test_replay_finds_the_target_and_the_hard_time_limit():
    assert _replay(_flat(20) + [_c(20, 100, 111)]).reason == "target"
    limited = _replay(_flat(60), deadline_minutes=45)
    assert limited.reason == "time_limit"
    assert limited.exit_time == T0 + timedelta(minutes=45)


def test_a_missing_minute_before_any_trigger_makes_the_replay_incomplete():
    candles = _flat(10) + [_c(m, 99, 101) for m in range(15, 20)] + [_c(20, 94, 100)]
    assert _replay(candles).status == "INCOMPLETE_HISTORY"


def test_verification_matches_contradicts_or_cannot_tell():
    stop = _replay(_flat(10) + [_c(10, 94, 100)])
    at = T0 + timedelta(minutes=10)
    assert verify_exit(stop, "stop_loss", at + timedelta(minutes=1)) == "MATCH"
    # booked much later, after an outage: the paper exit is wrong
    assert verify_exit(stop, "stop_loss", at + timedelta(hours=9)) == "MISMATCH"
    assert verify_exit(stop, "target", at) == "MISMATCH"
    incomplete = _replay(_flat(5) + [_c(9, 99, 101)])
    assert verify_exit(incomplete, "stop_loss", at) == "UNVERIFIABLE"


def test_a_guardian_exit_before_any_price_trigger_matches():
    no_trigger = _replay(_flat(30))
    assert no_trigger.status == "NO_PRICE_EXIT"
    assert verify_exit(no_trigger, "guardian_exit", T0 + timedelta(minutes=20)) == "MATCH"


# ---------------------------------------------------------------------
# Restart scenarios: gaps of 2 h / 12 h / 24 h / 48 h
# ---------------------------------------------------------------------


def _gap_case(gap_hours, exit_in_gap):
    """Monitoring was down for `gap_hours`; the paper engine booked the
    exit at restart. Exchange candles cover the whole gap."""
    minutes = int(gap_hours * 60)
    candles = _flat(minutes + 30)
    if exit_in_gap:
        candles[minutes // 2] = _c(minutes // 2, 94, 100)
    replay = _replay(candles)
    booked_at = T0 + timedelta(minutes=minutes)
    return replay, verify_exit(replay, "stop_loss" if exit_in_gap else "guardian_exit", booked_at)


def test_restarts_after_2_12_24_and_48_hours_are_reconstructed():
    for hours in (2, 12, 24, 48):
        replay, verdict = _gap_case(hours, exit_in_gap=True)
        assert replay.reason == "stop_loss"
        assert replay.exit_time == T0 + timedelta(minutes=int(hours * 60) // 2)
        # The paper engine booked it at restart - the candles contradict it.
        assert verdict == "MISMATCH"


def test_no_exit_during_the_gap_is_consistent_with_a_later_exit():
    for hours in (2, 12, 24, 48):
        replay, verdict = _gap_case(hours, exit_in_gap=False)
        assert replay.status == "NO_PRICE_EXIT"
        assert verdict == "MATCH"


def test_without_exchange_history_the_exit_stays_unobservable():
    position = make_position(exit_price=Decimal("95"), exit_reason="stop_loss",
                             closed_at=T0 + timedelta(hours=24), opened_at=T0)
    obs = classify_observation(
        position, [], Decimal("-50"), None, [], None, True, True,
        kline_verdict="UNVERIFIABLE", partial_error_overlap=True,
    )
    assert obs.status == "UNOBSERVABLE"
    assert obs.paper_exit_status == "UNVERIFIED"


def test_a_contradicted_paper_exit_is_corrected_from_klines_not_trusted():
    position = make_position(exit_price=Decimal("110"), exit_reason="target",
                             closed_at=T0 + timedelta(hours=10), opened_at=T0)
    obs = classify_observation(
        position, [], Decimal("50"), None, [], None, True, True,
        kline_verdict="MISMATCH", candle_path_available=True,
    )
    assert obs.outcome_source == "KLINES"
    assert obs.paper_exit_status == "MISMATCH"
    assert "EXIT_CORRECTED_FROM_KLINES" in obs.reasons


# ---------------------------------------------------------------------
# Candle path: from activation only
# ---------------------------------------------------------------------


def test_the_candle_path_ignores_everything_before_the_position_existed():
    # A spike to 120 BEFORE activation must not become MFE.
    candles = [_c(0, 100, 120)] + _flat(60)[1:]
    stats = candle_path(candles, T0 + timedelta(minutes=1), T0 + timedelta(minutes=60),
                        Decimal("100"), Decimal("95"), Decimal("110"))
    assert stats["mfe_pct"] == Decimal("0.5")
    assert stats["source"] == "EXCHANGE_KLINES"


def test_a_hole_in_the_candles_gives_no_path_rather_than_a_partial_one():
    candles = _flat(10) + [_c(m, 99, 101) for m in range(20, 40)]
    assert candle_path(candles, T0, T0 + timedelta(minutes=40), Decimal("100"),
                       Decimal("95"), Decimal("110")) is None


def test_entry_success_inside_one_minute_both_ways_is_unknowable():
    candles = [_c(0, 98.9, 101.2)] + _flat(10)[1:]
    stats = candle_path(candles, T0, T0 + timedelta(minutes=10), Decimal("100"),
                        Decimal("95"), Decimal("110"))
    assert stats["entry_success"] is None


# ---------------------------------------------------------------------
# Timestamps kept apart
# ---------------------------------------------------------------------


def _timeline(**overrides):
    base = dict(
        signal_at=T0 - timedelta(minutes=2), discovery_started_at=T0,
        ai_decision_at=T0 + timedelta(minutes=24), godfather_decision_at=T0 + timedelta(minutes=24),
        paper_opened_at=T0, created_at=T0 + timedelta(minutes=26),
        created_at_source="RECORDED", claim_at=T0 + timedelta(minutes=27), fill_at=None,
        planned_entry=Decimal("100"), actual_entry=Decimal("101.5"),
        actual_entry_source="EXCHANGE_FILL",
    )
    base.update(overrides)
    return TradeTimeline(**base)


def test_every_timestamp_and_price_level_stays_separate():
    timeline = _timeline()
    d = timeline.as_dict()
    distinct = {d["signal_at"], d["discovery_started_at"], d["ai_decision_at"],
                d["paper_opened_at"], d["created_at"], d["claim_at"]}
    assert len(distinct) == 6 - 1  # discovery start == paper opened_at, by construction
    assert d["planned_entry"] != d["actual_entry"]
    assert d["fill_at"] is None  # not recorded - never filled in with another time


def test_activation_is_the_live_claim_else_the_creation():
    assert _timeline().activation_at == T0 + timedelta(minutes=27)
    paper = _timeline(claim_at=None)
    assert paper.activation_at == T0 + timedelta(minutes=26)
    assert paper.decision_to_activation_minutes == 26


def test_drift_during_analysis_is_measured_against_the_planned_entry():
    assert _timeline().drift_during_analysis_pct == Decimal("1.5")


def test_the_database_records_the_real_creation_time_separately(tmp_path):
    from tests.crypto_trading.godfather.test_supervisor_sweep import _seed

    repo = SQLiteRepository(tmp_path / "t.db")
    before = datetime.now(UTC)
    pid = _seed(repo, 0, [100, 101], 101, "time_limit")  # opened_at in 2026-09-01
    created = repo.get_position_created_at(pid)
    assert created is not None and created >= before - timedelta(seconds=5)
    assert created != repo.get_position(pid).opened_at


def test_experience_measures_the_actual_position_not_the_planned_entry():
    from crypto_trading.godfather.book import TradeContext
    from crypto_trading.godfather.experience_builder import trade_profile

    position = make_position(exit_price=Decimal("104"), exit_reason="time_limit",
                             closed_at=T0 + timedelta(minutes=60), opened_at=T0)
    trade = TradeContext(
        position=position, candidate=None, opportunity_screen=None, gate_decision=None,
        observations=[], points=[path_point(m, Decimal("103"), position=position)
                                 for m in range(27, 60)],
        pnl=None, regime="btc_ok", timeline=_timeline(actual_entry=Decimal("102")),
        candle_path={"source": "EXCHANGE_KLINES", "mfe_pct": Decimal("2.0"),
                     "mae_pct": Decimal("-0.5"), "minutes_to_mfe": 5.0,
                     "minutes_to_first_favorable": 1.0, "minutes_to_target": None,
                     "minutes_to_sl": None, "entry_success": True},
    )
    profile = trade_profile(trade, [], [])
    assert profile["path_source"] == "EXCHANGE_KLINES"
    assert profile["timeline"]["actual_entry"] == "102"
    assert profile["timeline"]["planned_entry"] == "100"


# ---------------------------------------------------------------------
# partial_error
# ---------------------------------------------------------------------


def _error_run(repo, run_id, start, errors):
    repo.start_run(run_id, "monitoring", start)
    repo.complete_run(run_id, start + timedelta(seconds=15), "partial_error", errors)


def test_partial_errors_are_attributed_per_symbol_or_globally(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _error_run(repo, "r1", T0, ["ConnectorUnavailableError: tom klines-lista (SOPH-USDT)"])
    _error_run(repo, "r2", T0 + timedelta(hours=1),
               ["ConnectorUnavailableError: BingX otillgänglig: /quote/ticker (ConnectError)"])
    by_symbol, global_windows = load_partial_errors(repo)

    assert set(by_symbol) == {"SOPH-USDT"}
    assert len(global_windows) == 1
    assert _overlaps(by_symbol["SOPH-USDT"], T0 - timedelta(minutes=5), T0 + timedelta(minutes=5))
    assert not _overlaps(by_symbol["SOPH-USDT"], T0 + timedelta(hours=2), T0 + timedelta(hours=3))


def test_a_partial_error_is_harmless_once_candles_verify_the_exit():
    position = make_position(exit_price=Decimal("95"), exit_reason="stop_loss",
                             closed_at=T0 + timedelta(minutes=60), opened_at=T0)
    points = [path_point(m, Decimal("100"), position=position) for m in range(0, 61)]
    obs = classify_observation(
        position, points, Decimal("-50"), None, [], None, True, True,
        kline_verdict="MATCH", candle_path_available=True, partial_error_overlap=True,
        activation_at=position.opened_at,
    )
    assert obs.status == "COMPLETE"
    assert "EXIT_VERIFIED_BY_KLINES" in obs.reasons


def test_a_zero_duration_paper_trade_is_unobservable():
    position = make_position(exit_price=Decimal("110"), exit_reason="target",
                             opened_at=T0, closed_at=T0)
    obs = classify_observation(position, [], Decimal("10"), None, [], None, True, True)
    assert obs.status == "UNOBSERVABLE" and obs.reasons == ["ZERO_DURATION_TRADE"]


# ---------------------------------------------------------------------
# Kline archive
# ---------------------------------------------------------------------


class _PagedConnector:
    def __init__(self, missing=()):
        self.calls = []
        self._missing = set(missing)

    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        self.calls.append((start_time_ms, end_time_ms))
        rows = []
        t = start_time_ms
        while t <= end_time_ms and len(rows) < limit:
            minute = (t - int(T0.timestamp() * 1000)) // 60000
            if minute not in self._missing:
                rows.append({"open": "100", "high": "101", "low": "99", "close": "100",
                             "volume": "1", "time": t})
            t += 60000
        return rows


def test_the_archive_pages_the_whole_window_and_never_asks_beyond_it():
    connector = _PagedConnector()
    end = T0 + timedelta(hours=30)
    rows = fetch_window(connector, "BTC-USDT", T0, end)
    assert len(connector.calls) == 2  # 1800 minutes at 1440 per page
    assert all(e <= int(end.timestamp() * 1000) for _s, e in connector.calls)
    assert rows[0]["open_time"] == T0 and rows[-1]["open_time"] <= end


def test_coverage_requires_both_ends_and_the_window_reaches_the_live_close(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = make_position(opened_at=T0, closed_at=T0 + timedelta(minutes=30))
    live_close = T0 + timedelta(minutes=90)
    start, end = position_window(position, T0 + timedelta(days=1), live_close)
    assert end >= live_close

    only_start = fetch_window(_PagedConnector(), position.instrument, T0,
                              T0 + timedelta(minutes=30))
    repo.save_exchange_klines(position.instrument, only_start, T0)
    assert not is_covered(repo, position, T0 + timedelta(days=1), live_close)
    full = fetch_window(_PagedConnector(), position.instrument, start, end)
    repo.save_exchange_klines(position.instrument, full, T0)
    assert is_covered(repo, position, T0 + timedelta(days=1), live_close)


def test_archived_history_is_never_rewritten(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    rows = fetch_window(_PagedConnector(), "X-USDT", T0, T0 + timedelta(minutes=3))
    assert repo.save_exchange_klines("X-USDT", rows, T0) == len(rows)
    assert repo.save_exchange_klines("X-USDT", rows, T0) == 0
    conn = sqlite3.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM exchange_klines_1m").fetchone()[0] == len(rows)
