"""Fas 2A: observation integrity, R multiples, and the experience filter.

The central guarantee: a monitoring gap can never silently produce a
false MFE/MAE, a false exit reason or a false outcome. A trade whose exit
was not seen is UNOBSERVABLE unless exchange history covers it; a trade
whose path has holes keeps its verified outcome but contributes no path
statistics; R is always against the ORIGINAL planned risk, or UNAVAILABLE.
"""

import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.godfather.book import TradeContext
from crypto_trading.godfather.experience import ExperienceConfig, build_experience_memory
from crypto_trading.godfather.experience_builder import build_samples, exclusion_reason
from crypto_trading.godfather.observation import (
    CATCHUP_MAX_MINUTES,
    MonitoringGap,
    classify_observation,
    monitoring_gaps,
)
from crypto_trading.godfather.risk_units import compute_r, initial_stop_loss
from crypto_trading.single_instance import AlreadyRunningError, InstanceLock
from tests.crypto_trading.godfather.intelligence_fixtures import OPENED, make_position, path_point
from tests.crypto_trading.test_market_snapshot import _settings

_T = OPENED


def _at(minutes: float) -> datetime:
    return _T + timedelta(minutes=minutes)


def _run(start_min, end_min):
    return {"started_at": _at(start_min).isoformat(), "completed_at": _at(end_min).isoformat()}


def _dense(position, until, price="101"):
    return [path_point(m, Decimal(price), position=position) for m in range(0, int(until) + 1)]


def _classify(position, points, gaps=(), live=None, pnl=Decimal("10"), r_net=None,
              run_end=None, features=True, r_ok=True):
    return classify_observation(
        position, points, pnl, live, list(gaps), run_end or _T, features, r_ok, r_net
    )


# ---------------------------------------------------------------------
# Monitoring gaps and catch-up coverage
# ---------------------------------------------------------------------


def test_a_short_gap_with_catch_up_is_fully_recovered():
    runs = [_run(0, 1), _run(60, 61)]
    gaps = monitoring_gaps(runs, [{"started_at": _at(60).isoformat()}])
    assert len(gaps) == 1 and gaps[0].unrecovered is None


def test_a_legacy_catch_up_left_the_start_of_a_long_gap_unrecovered():
    base = datetime(2026, 9, 10, tzinfo=UTC)  # before the paged catch-up existed

    def run(a, b):
        return {"started_at": (base + timedelta(minutes=a)).isoformat(),
                "completed_at": (base + timedelta(minutes=b)).isoformat()}

    runs = [run(0, 1), run(1 + CATCHUP_MAX_MINUTES + 500, 1 + CATCHUP_MAX_MINUTES + 501)]
    gaps = monitoring_gaps(runs, [{"started_at": runs[1]["started_at"]}])
    start, end = gaps[0].unrecovered
    assert start == base + timedelta(minutes=1)
    assert (end - start) == timedelta(minutes=500)


def test_a_gap_without_any_catch_up_is_wholly_unrecovered():
    gaps = monitoring_gaps([_run(0, 1), _run(30, 31)], [])
    assert gaps[0].unrecovered == (_at(1), _at(30))


def test_legacy_catch_ups_are_bounded_and_paged_ones_recover_the_whole_gap():
    """Before Fas 2A.1 a catch-up replayed at most 1000 minutes; a paged
    catch-up (from PAGED_CATCHUP_SINCE) replays the whole gap."""
    from crypto_trading.godfather.observation import PAGED_CATCHUP_SINCE
    from crypto_trading.paper_trading import monitoring_catchup

    assert not hasattr(monitoring_catchup, "_MAX_CATCHUP_KLINES")
    start = PAGED_CATCHUP_SINCE + timedelta(hours=1)
    runs = [
        {"started_at": start.isoformat(), "completed_at": start.isoformat()},
        {"started_at": (start + timedelta(hours=48)).isoformat(),
         "completed_at": (start + timedelta(hours=48, minutes=1)).isoformat()},
    ]
    gaps = monitoring_gaps(runs, [{"started_at": runs[1]["started_at"]}])
    assert gaps[0].unrecovered is None
    assert CATCHUP_MAX_MINUTES == 1000


# ---------------------------------------------------------------------
# Exits across gaps
# ---------------------------------------------------------------------


def _unrecovered(start, end):
    return MonitoringGap(start=_at(start), end=_at(end), recovered_from=_at(end))


def test_gap_before_sl_without_exchange_history_is_unobservable():
    position = make_position(exit_price=Decimal("95"), exit_reason="stop_loss",
                             closed_at=_at(300))
    obs = _classify(position, _dense(position, 60), gaps=[_unrecovered(100, 290)])

    assert obs.status == "UNOBSERVABLE"
    assert obs.exit_verification == "UNVERIFIED"
    assert "EXCHANGE_HISTORY_GAP" in obs.reasons


def test_gap_before_tp_with_exchange_history_uses_the_exchange_outcome():
    position = make_position(exit_price=Decimal("110"), exit_reason="target",
                             closed_at=_at(300))
    live = {"exchange_fill_entry": "100.1", "exchange_fill_exit": "109.8",
            "claimed_at": _at(0.2).isoformat()}
    obs = _classify(position, _dense(position, 60), gaps=[_unrecovered(100, 290)], live=live)

    assert obs.status == "PARTIAL"
    assert obs.outcome_source == "EXCHANGE"
    assert obs.exit_verification == "EXCHANGE"
    assert obs.paper_exit_status == "UNVERIFIED"
    assert "MONITORING_GAP_UNRECOVERED" in obs.reasons


def test_gap_through_both_sl_and_tp_is_unobservable_without_exchange_history():
    """Before the gap the trade was near its target, after it the stop had
    filled. Which level came first is unknowable - the booked exit reason
    is not trusted."""
    position = make_position(exit_price=Decimal("94"), exit_reason="stop_loss",
                             closed_at=_at(400))
    points = _dense(position, 60, price="109")
    obs = _classify(position, points, gaps=[_unrecovered(70, 390)])

    assert obs.status == "UNOBSERVABLE"
    assert obs.path_status == "PARTIAL"


def test_a_recovered_gap_is_candle_verified():
    position = make_position(exit_price=Decimal("95"), exit_reason="stop_loss",
                             closed_at=_at(60))
    recovered = MonitoringGap(start=_at(10), end=_at(50), recovered_from=_at(10))
    obs = _classify(position, _dense(position, 60), gaps=[recovered])

    assert obs.exit_verification == "CANDLES_REPLAYED"
    assert obs.paper_exit_status == "VERIFIED"


def test_unknown_paper_outcome_without_exchange_is_unobservable():
    position = make_position(closed_at=_at(60))
    obs = _classify(position, _dense(position, 60), pnl=None)
    assert obs.status == "UNOBSERVABLE" and "UNKNOWN_PNL" in obs.reasons


def test_a_fully_watched_trade_is_complete():
    position = make_position(exit_price=Decimal("110"), exit_reason="target",
                             closed_at=_at(60))
    obs = _classify(position, _dense(position, 60))
    assert obs.status == "COMPLETE" and obs.path_status == "OBSERVED"


# ---------------------------------------------------------------------
# Path: gaps, activation, stop overshoot
# ---------------------------------------------------------------------


def test_a_guardian_hole_makes_the_path_partial_but_keeps_the_outcome():
    position = make_position(exit_price=Decimal("110"), exit_reason="target",
                             closed_at=_at(120))
    points = [p for p in _dense(position, 120) if not 30 < p.minutes_in_trade < 90]
    obs = _classify(position, points)

    assert obs.path_status == "PARTIAL" and "MONITORING_GAP" in obs.reasons
    assert obs.outcome_usable and not obs.path_usable


def test_the_analysis_window_before_activation_is_not_mistaken_for_a_gap():
    """opened_at is the START of the deciding discovery run; the position
    exists from the LIVE claim 25 minutes later and Guardian sees it at
    once. That is a decision delay, not a hole in the path."""
    position = make_position(exit_price=Decimal("110"), exit_reason="target",
                             closed_at=_at(90))
    points = [path_point(m, Decimal("101"), position=position) for m in range(26, 91)]
    obs = _classify(position, points, live={"claimed_at": _at(25).isoformat()})

    assert obs.path_status == "OBSERVED"
    assert "DECISION_TO_ACTIVATION_DELAY" in obs.reasons
    assert obs.activation_source == "LIVE_CLAIM"


def test_a_late_first_observation_after_activation_is_flagged():
    position = make_position(exit_price=Decimal("110"), exit_reason="target",
                             closed_at=_at(90))
    points = [path_point(m, Decimal("101"), position=position) for m in range(45, 91)]
    obs = _classify(position, points, live={"claimed_at": _at(25).isoformat()})

    assert obs.path_status == "PARTIAL"
    assert "LATE_FIRST_OBSERVATION" in obs.reasons


def test_a_stop_gapped_through_is_flagged_as_overshoot_not_hidden():
    position = make_position(exit_price=Decimal("70"), exit_reason="stop_loss",
                             closed_at=_at(60))
    obs = _classify(position, _dense(position, 60), r_net=Decimal("-5.8"))
    assert "STOP_OVERSHOOT" in obs.reasons
    assert obs.outcome_usable


# ---------------------------------------------------------------------
# R multiples
# ---------------------------------------------------------------------


def test_r_is_return_over_planned_risk():
    position = make_position(stop_loss=Decimal("98"), exit_price=Decimal("104"))
    r = compute_r(position, Decimal("98"), None, fee_pct=Decimal("0"))
    assert r.initial_risk_pct == Decimal("0.02")
    assert r.r_gross == Decimal("2")
    assert r.r_net == Decimal("2")


def test_r_net_uses_the_real_net_result():
    position = make_position(stop_loss=Decimal("98"), exit_price=Decimal("104"),
                             size=Decimal("1000"))
    r = compute_r(position, Decimal("98"), Decimal("38"))  # 40 gross - 2 fees
    assert r.r_net == Decimal("1.9")


def test_r_is_independent_of_position_size():
    small = make_position(size=Decimal("500"), exit_price=Decimal("104"), stop_loss=Decimal("98"))
    large = make_position(size=Decimal("2500"), exit_price=Decimal("104"), stop_loss=Decimal("98"))
    assert compute_r(small, Decimal("98"), Decimal("20")).r_net == compute_r(
        large, Decimal("98"), Decimal("100")
    ).r_net


def test_r_for_a_short_measures_risk_above_entry():
    position = make_position(direction="SHORT", stop_loss=Decimal("102"),
                             exit_price=Decimal("96"))
    r = compute_r(position, Decimal("102"), None, fee_pct=Decimal("0"))
    assert r.r_gross == Decimal("2")


def test_an_exchange_outcome_is_measured_against_the_planned_risk_not_the_fill():
    """A late LIVE fill 5% above plan must not shrink the denominator."""
    position = make_position(stop_loss=Decimal("95"), exit_price=None)
    r = compute_r(position, Decimal("95"), None, exit_price=Decimal("115.5"),
                  entry_price=Decimal("105"), fee_pct=Decimal("0"))
    assert r.initial_risk_pct == Decimal("0.05")
    assert r.return_pct == Decimal("0.1")
    assert r.r_net == Decimal("2")


def test_missing_or_invalid_initial_sl_makes_r_unavailable():
    position = make_position(exit_price=Decimal("104"))
    assert compute_r(position, None, Decimal("40")).status == "UNAVAILABLE"
    wrong_side = compute_r(position, Decimal("101"), Decimal("40"))
    assert wrong_side.status == "UNAVAILABLE" and wrong_side.reason == "INVALID_SL_SIDE"
    assert compute_r(position, Decimal("0"), Decimal("40")).status == "UNAVAILABLE"


def test_r_uses_the_original_sl_not_a_later_tightened_one():
    tightened = make_position(stop_loss=Decimal("99.5"))  # after a TIGHTEN_SL
    decisions = [{"decision_type": "TIGHTEN_SL", "intervention_applied": 1,
                  "decided_at": "2026-09-25T09:00:00+00:00", "old_sl": "95"}]
    sl, problem = initial_stop_loss(tightened, decisions)
    assert sl == Decimal("95") and problem is None


def test_a_stop_overwritten_without_record_makes_r_unavailable():
    decisions = [{"decision_type": "TIGHTEN_SL", "intervention_applied": 1,
                  "decided_at": "2026-09-25T09:00:00+00:00", "old_sl": None}]
    sl, problem = initial_stop_loss(make_position(), decisions)
    assert sl is None and problem == "INITIAL_SL_OVERWRITTEN_WITHOUT_RECORD"


def test_an_untouched_position_keeps_its_own_stop_as_the_original():
    sl, problem = initial_stop_loss(make_position(stop_loss=Decimal("95")), [])
    assert sl == Decimal("95") and problem is None


# ---------------------------------------------------------------------
# Experience filter
# ---------------------------------------------------------------------


def _trade(position, points, obs, r_net):
    r = compute_r(position, position.stop_loss, r_net * position.size * Decimal("0.05"))
    return TradeContext(
        position=position, candidate=None, opportunity_screen=None, gate_decision=None,
        observations=[], points=points, pnl=r_net * position.size * Decimal("0.05"),
        regime="btc_ok", features={"k": "a"}, r=r, observation=obs,
    )


def test_unobservable_trades_are_never_experience_and_path_holes_drop_path_stats():
    good = make_position("good", exit_price=Decimal("110"), exit_reason="target",
                         closed_at=_at(60))
    holed = make_position("holed", exit_price=Decimal("110"), exit_reason="target",
                          closed_at=_at(120))
    blind = make_position("blind", exit_price=Decimal("95"), exit_reason="stop_loss",
                          closed_at=_at(300))
    holed_points = [p for p in _dense(holed, 120) if not 30 < p.minutes_in_trade < 90]
    trades = [
        _trade(good, _dense(good, 60), _classify(good, _dense(good, 60)), Decimal("2")),
        _trade(holed, holed_points, _classify(holed, holed_points), Decimal("2")),
        _trade(blind, _dense(blind, 60),
               _classify(blind, _dense(blind, 60), gaps=[_unrecovered(100, 290)]),
               Decimal("-1")),
    ]
    assert exclusion_reason(trades[2]) == "UNOBSERVABLE_OUTCOME"

    samples = {s.position_id: s for s in build_samples(trades, {}, {}, _settings())}
    assert set(samples) == {"good", "holed"}
    assert samples["good"].mfe_pct is not None
    assert samples["holed"].mfe_pct is None
    assert samples["holed"].profile["has_path"] is False
    assert samples["holed"].r == Decimal("2")


def test_experience_memory_classifies_in_r_when_r_is_available():
    good = make_position("x", exit_price=Decimal("110"), exit_reason="target", closed_at=_at(60))
    trades = []
    for i in range(12):
        position = good.model_copy(update={
            "position_id": f"x{i}", "closed_at": _at(60 + i),
            "size": Decimal("500") if i % 2 else Decimal("2500"),
        })
        trades.append(_trade(position, _dense(position, 60),
                             _classify(position, _dense(position, 60)), Decimal("1")))
    samples = build_samples(trades, {}, {}, _settings())
    pattern = build_experience_memory(samples, _at(999), "r", ExperienceConfig(min_support=8))[0]

    assert pattern.detail["outcome_metric"] == "R"
    assert Decimal(pattern.detail["outcome"]["expectancy"]) == Decimal("1")


# ---------------------------------------------------------------------
# One process per database
# ---------------------------------------------------------------------


def test_a_second_instance_cannot_take_the_lock(tmp_path):
    first = InstanceLock(tmp_path / "db.instance.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            InstanceLock(tmp_path / "db.instance.lock").acquire()
    finally:
        first.release()
    again = InstanceLock(tmp_path / "db.instance.lock")
    again.acquire()
    again.release()


def test_a_crashed_holder_never_leaves_a_stale_lock(tmp_path):
    """The OS releases the lock when the holding process dies, however it
    dies - here the child exits without ever calling release()."""
    lock = tmp_path / "db.instance.lock"
    script = textwrap.dedent(f"""
        import os
        from pathlib import Path
        from crypto_trading.single_instance import InstanceLock
        InstanceLock(Path(r"{lock}")).acquire()
        os._exit(3)
    """)
    result = subprocess.run([sys.executable, "-c", script], cwd=str(
        __import__("pathlib").Path(__file__).resolve().parents[3]
    ), capture_output=True, timeout=60)
    assert result.returncode == 3
    survivor = InstanceLock(lock)
    survivor.acquire()
    survivor.release()


def test_guardian_observes_a_new_position_on_the_very_next_tick(tmp_path):
    """No minimum age before the first observation: a position that exists
    at the tick is observed at that tick."""
    from crypto_trading.guardian.tick import run_guardian_tick_body
    from crypto_trading.storage.repository import SQLiteRepository
    from tests.crypto_trading.guardian.test_tick import (
        _NOW,
        _FakeRunner,
        _seed_candidate_and_position,
        _StubConnector,
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo, opened_at=_NOW - timedelta(seconds=5))
    observations = run_guardian_tick_body(
        repo, _StubConnector(), _FakeRunner(), _settings(), "run-1", _NOW
    )
    assert [o.position_id for o in observations] == ["pos-1"]
    assert datetime.fromisoformat(
        repo.find_latest_guardian_observation("pos-1")["observed_at"]
    ).astimezone(UTC) - (_NOW - timedelta(seconds=5)) < timedelta(minutes=1)
