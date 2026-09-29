"""Data-integrity tests for the edge lab (2026-09-29), in the spirit of
Freqtrade's lookahead-analysis and recursive-analysis: a backtest that can
see the future is not a strategy."""
import random
from datetime import UTC, datetime, timedelta

import pytest

from crypto_trading.entry_research import edge_lab as el

T_START = int(datetime(2026, 9, 5, tzinfo=UTC).timestamp())


def _minutes(n=3 * 1440, seed=1, start=T_START):
    rng = random.Random(seed)
    ts, o, h, l, c, v = [], [], [], [], [], []  # noqa: E741
    p = 100.0
    for i in range(n):
        op = p
        p = max(1.0, p * (1 + rng.gauss(0, 0.002)))
        ts.append(start + 60 * i)
        o.append(op)
        h.append(max(op, p) * (1 + abs(rng.gauss(0, 0.001))))
        l.append(min(op, p) * (1 - abs(rng.gauss(0, 0.001))))
        c.append(p)
        v.append(abs(rng.gauss(100, 30)))
    return el.Minute(ts, o, h, l, c, v)


def _corrupt_future(m: el.Minute, T: int) -> el.Minute:
    """Every value at or after T replaced by nonsense."""
    k = next(i for i, t in enumerate(m.ts) if t >= T)
    return el.Minute(m.ts, m.o[:k] + [9e9] * (len(m.o) - k), m.h[:k] + [9e9] * (len(m.h) - k),
                     m.l[:k] + [1e-9] * (len(m.l) - k), m.c[:k] + [9e9] * (len(m.c) - k),
                     m.v[:k] + [9e9] * (len(m.v) - k))


def test_lookahead_features_are_blind_to_everything_at_or_after_T():
    m = _minutes()
    for T in (T_START + 1440 * 60 + 17 * 300, T_START + 2 * 1440 * 60 + 5 * 300):
        a = el.features_from_bars(el.bars30_at(m, T))
        b = el.features_from_bars(el.bars30_at(_corrupt_future(m, T), T))
        assert a == b
        assert el.atr15_pct(m, T) == el.atr15_pct(_corrupt_future(m, T), T)


def test_recursive_stability_features_do_not_depend_on_how_much_history_is_loaded():
    long = _minutes(n=4 * 1440)
    k = 2 * 1440                                  # drop the first two days
    short = el.Minute(long.ts[k:], long.o[k:], long.h[k:], long.l[k:], long.c[k:], long.v[k:])
    T = long.ts[-1] - 7 * 3600 - (long.ts[-1] % 300)
    assert el.features_from_bars(el.bars30_at(long, T)) == el.features_from_bars(el.bars30_at(short, T))


def test_the_forming_30m_bar_uses_only_minutes_strictly_before_T():
    m = _minutes()
    T = T_START + 1440 * 60 + 10 * 60             # 10 minutes into a 30m bar
    bars = el.bars30_at(m, T)
    i_start = m.ts.index(T - 10 * 60)
    assert bars[-1][3] == m.c[i_start + 9]        # close of the minute ending at T
    assert len(bars) == el.N30


def test_signal_timestamp_integrity_entry_is_never_before_T():
    m = _minutes()
    T = T_START + 1440 * 60 + 300
    a = el.atr15_pct(m, T)
    fast, slow = el.outcome(m, T, 0, a), el.outcome(m, T, el.LATENCY_S, a)
    assert fast is not None and slow is not None
    # the fast entry is the open of the minute starting exactly at T
    assert el.outcome(_corrupt_future(m, T + 1), T, 0, a) is not None


def test_no_fabricated_outcome_across_a_data_gap():
    m = _minutes()
    T = T_START + 1440 * 60
    k = m.ts.index(T + 3600)
    gap = el.Minute(m.ts[:k] + [t + 3600 for t in m.ts[k:]], m.o, m.h, m.l, m.c, m.v)
    assert el.outcome(gap, T, 0, el.atr15_pct(m, T)) is None


def test_purge_drops_every_row_whose_label_window_reaches_the_next_period():
    vf, tf = datetime(2026, 9, 13, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC)
    rows = [{"T": int((vf - timedelta(hours=h)).timestamp())} for h in (1, 6, 7, 30)]
    parts = el.split(rows, vf, tf)
    kept = sorted(round((vf.timestamp() - r["T"]) / 3600) for r in parts["TRAIN"])
    assert kept == [7, 30]                         # 6 h + 23 min reach -> 1 h and 6 h purged


def test_events_need_a_known_previous_false_state():
    m = _minutes()
    rows = el.build_universe({"X-USDT": m}, baseline_every=1)
    assert rows, "the synthetic series must produce rows"
    by_t = {r["T"]: r for r in rows}
    for r in rows:
        for name in ("rsi", "volz", "move", "breakout", "rangeexp"):
            if r["feat"][f"E_{name}"]:
                assert r["feat"][f"S_{name}"] is True
                prev = by_t.get(r["T"] - el.GRID_S)
                if prev is not None:
                    assert prev["feat"][f"S_{name}"] is False


@pytest.mark.parametrize("latency", [0, el.LATENCY_S])
def test_outcome_r_matches_the_pre_registered_bracket(latency):
    m = _minutes()
    T = T_START + 1440 * 60 + 600
    a = el.atr15_pct(m, T)
    out = el.outcome(m, T, latency, a)
    assert out["reason"] in ("SL", "TP", "TIME")
    if out["reason"] == "SL":
        assert out["r"] == pytest.approx((-(1 + el.STOP_SLIP * (1 - 2 * a / 100) / (2 * a / 100))
                                          - el.FEE_RT / (2 * a / 100)), rel=1e-6)


def test_the_fast_precomputed_path_is_identical_to_the_reference():
    m = _minutes(n=3 * 1440, seed=7)
    pre = el.precompute30(m)
    for T in range(T_START + 16 * 3600, m.ts[-1], 7 * 300):
        assert el.bars30_at(m, T, pre) == el.bars30_at(m, T)


def test_the_fast_path_is_also_blind_to_the_future():
    m = _minutes()
    T = T_START + 1440 * 60 + 17 * 300
    bad = _corrupt_future(m, T)
    assert el.bars30_at(m, T, el.precompute30(m)) == el.bars30_at(bad, T, el.precompute30(bad))


def test_per_family_age_is_zero_exactly_on_the_event():
    rows = el.build_universe({"X-USDT": _minutes(n=4 * 1440, seed=3)}, baseline_every=1)
    for r in rows:
        for fam in ("rsi", "volz", "move", "breakout", "rangeexp"):
            age = r["feat"][f"age_{fam}"]
            if r["feat"][f"E_{fam}"]:
                assert age == 0
            if not r["feat"][f"S_{fam}"]:
                assert age is None
