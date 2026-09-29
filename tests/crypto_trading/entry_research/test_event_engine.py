"""Integrity tests for the event engine research (2026-09-29): no lookahead,
recursive stability, point-in-time derivatives, TEST-blind member selection,
LIVE-like capacity rules, and isolation from everything that trades."""

import ast
import math
import random
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research import edge_lab as el
from crypto_trading.entry_research import event_engine as ee

ROOT = Path(__file__).resolve().parents[3]
T_START = int(datetime(2026, 9, 5, tzinfo=UTC).timestamp())


def _minutes(n=3 * 1440, seed=3, start=T_START):
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


def _corrupt_from(m, T):
    k = next(i for i, t in enumerate(m.ts) if t >= T)
    bad = len(m.ts) - k
    return el.Minute(
        m.ts,
        m.o[:k] + [9e9] * bad,
        m.h[:k] + [9e9] * bad,
        m.l[:k] + [1e-9] * bad,
        m.c[:k] + [9e9] * bad,
        m.v[:k] + [9e9] * bad,
    )


def _feat(m, T):
    s = ee.bars5(m)
    return ee.features_at(ee.Prepared(s), s.index_closing_at(T))


def test_lookahead_features_at_T_ignore_every_minute_at_or_after_T():
    m = _minutes()
    for T in (T_START + 2 * 86400 + 17 * 300, T_START + 2 * 86400 + 5 * 3600):
        a = _feat(m, T)
        assert a is not None
        assert a == _feat(_corrupt_from(m, T), T)


def test_bar_k_closes_at_T_and_contains_only_minutes_before_T():
    m = _minutes()
    s = ee.bars5(m)
    T = T_START + 86400 + 300 * 7
    k = s.index_closing_at(T)
    assert s.close_time(k) == T
    i_last = m.ts.index(T - 60)
    assert s.c[k] == m.c[i_last]


def test_recursive_stability_features_do_not_depend_on_loaded_history():
    m = _minutes(n=4 * 1440)
    k0 = 1440  # drop the first day (5m aligned)
    short = el.Minute(m.ts[k0:], m.o[k0:], m.h[k0:], m.l[k0:], m.c[k0:], m.v[k0:])
    T = T_START + 3 * 86400 + 11 * 300
    a, b = _feat(m, T), _feat(short, T)
    assert a is not None and b is not None
    for key in a:
        assert math.isclose(a[key], b[key], rel_tol=1e-7, abs_tol=1e-9), key


def test_too_little_history_gives_no_features():
    m = _minutes(n=1000)
    s = ee.bars5(m)
    assert all(ee.features_at(ee.Prepared(s), k) is None for k in range(len(s.c)))


def test_a_breakout_is_an_event_once_not_a_state():
    f_prev = {"c": 100.0, "hh48": 101.0, "ll48": 90.0, "hh288": 110.0, "ll288": 80.0}
    base = {
        "ret48": 0.5,
        "ret288": 1.0,
        "ratio": 1.0,
        "ratio_min24": 1.0,
        "ret6": 0.1,
        "z3": 0.0,
        "cp": 0.0,
        "vz": 0.0,
        "z1": 0.0,
        "sma60": 1.0,
        "sma150": 1.0,
        "o": 100.0,
        "h": 100.0,
        "l": 100.0,
        "z12": 0.0,
    }
    prev = {**base, **f_prev}
    now = {**prev, "c": 102.0, "h": 102.0}
    later = {**now, "c": 103.0, "h": 103.0}
    assert ("BRK_4H", "LONG") in ee.events_at(now, prev)
    assert ("EARLY_BRK", "LONG") in ee.events_at(now, prev)
    assert ("BRK_4H", "LONG") not in ee.events_at(later, now)  # still true, but no longer new
    assert ee.events_at(now, None) == []  # unknown previous point: no event


def test_missing_derivative_data_never_creates_an_event():
    f = {
        "c": 102.0,
        "hh48": 101.0,
        "ll48": 90.0,
        "fund": None,
        "oi1h": None,
        "oi4h": None,
        "z12": 5.0,
    }
    fp = {**f, "c": 100.0}
    names = {n for n, _ in ee.events_at({**f, **_neutral()}, {**fp, **_neutral()})}
    assert not names & {"FUNDING_SQUEEZE", "OI_PRICE", "OI_COVER", "OI_BUILD_BRK"}


def _neutral():
    return {
        "ret48": 0.0,
        "ret288": 0.0,
        "ratio": 1.0,
        "ratio_min24": 1.0,
        "ret6": 0.0,
        "z3": 0.0,
        "cp": 0.0,
        "vz": 0.0,
        "z1": 0.0,
        "sma60": 1.0,
        "sma150": 1.0,
        "o": 1.0,
        "h": 1.0,
        "l": 1.0,
        "hh288": 1e9,
        "ll288": 0.0,
    }


def test_open_interest_is_used_only_one_period_after_its_stamp():
    T = 1_000_000_200
    d = ee.Derivs(oi={"X": ([T - 600, T - 200], [100.0, 999.0])})
    assert d.oi_at("X", T) == 100.0  # the value stamped T-200 is not yet usable
    d2 = ee.Derivs(oi={"X": ([T - 300], [5.0])})
    assert d2.oi_at("X", T) == 5.0


def test_funding_is_used_only_once_settled():
    T = 1_000_000_000
    d = ee.Derivs(funding={"X": ([T - 3600, T + 1], [0.0001, -0.01])})
    assert d.funding_at("X", T) == 0.0001


def test_kernel_feasibility_matches_the_fixed_size_worst_case():
    assert ee.kernel_feasible(0.5, 0.05)  # (1.0 + 0.7) % of 1000 = 17 <= 19.45
    assert not ee.kernel_feasible(0.7, 0.05)  # 21 > 19.45
    assert ee.kernel_feasible(0.7, 0.10)  # 21 <= 38.9


def _s(mean, n=50, up=None, down=None):
    return {
        "n": n,
        "mean": mean,
        "regimes": {
            "btc_up": {"n": 25, "mean": mean if up is None else up},
            "btc_down": {"n": 25, "mean": mean if down is None else down},
        },
    }


def _ev(test_mean):
    return {
        "BASELINE:LONG": {"TRAIN": _s(-0.1), "VALID": _s(-0.1), "TEST": _s(-0.1)},
        "BRK_4H:LONG": {"TRAIN": _s(0.2), "VALID": _s(0.1), "TEST": _s(test_mean)},
        "ACCEL:LONG": {"TRAIN": _s(-0.5), "VALID": _s(-0.4), "TEST": _s(test_mean)},
        "CHANGE_POINT:LONG": {
            "TRAIN": _s(0.3, up=0.6, down=-0.2),
            "VALID": _s(0.2, up=0.5, down=-0.3),
            "TEST": _s(test_mean),
        },
    }


def test_members_vetoes_and_no_trade_regimes_are_chosen_without_test():
    a, b = ee.choose_members(_ev(-5.0)), ee.choose_members(_ev(+5.0))
    assert a == b
    members, vetoes, no_trade = a
    assert set(members) == {"BRK_4H:LONG", "CHANGE_POINT:LONG"}
    assert vetoes == {"ACCEL:LONG"}
    assert no_trade["CHANGE_POINT:LONG"] == {"down"}


def _row(sym, T, typ="BRK_4H", side="LONG", btc="up", atr=0.4, exit_after=3600):
    return {
        "symbol": sym,
        "T": T,
        "type": typ,
        "side": side,
        "period": "TEST",
        "reg": {"btc": btc},
        "atr15": atr,
        "entry_ts": T + 300,
        "exit_ts": T + 300 + exit_after,
        "r": 1.0,
        "reason": "TP",
    }


def test_combined_stream_one_per_symbol_best_quality_veto_and_no_trade_regime():
    members = {"BRK_4H:LONG": 0.1, "CHANGE_POINT:LONG": 0.3}
    rows = [
        _row("A", 0),
        _row("A", 0, "CHANGE_POINT"),  # same T: better-documented wins
        _row("A", 1800),  # < 60 min after the last pick
        _row("B", 0, "ACCEL"),
        _row("B", 600),  # vetoed by ACCEL 10 min earlier
        _row("C", 0, "CHANGE_POINT", btc="down"),
    ]  # member's no-trade regime
    out = ee.combined_stream(rows, members, {"ACCEL:LONG"}, {"CHANGE_POINT:LONG": {"down"}}, "TEST")
    assert [(r["symbol"], r["type"]) for r in out] == [("A", "CHANGE_POINT")]


def test_capacity_sim_respects_max_open_one_per_symbol_and_the_group_cap():
    stream = [{**_row(s, 0), "quality": 0.1} for s in "ABCDEF"] + [
        {**_row("A", 600), "quality": 0.1}
    ]
    taken = ee.capacity_sim(stream, group_cap=1.0, total_cap=1.0)
    assert len(taken) == 4  # max 4 open
    small = ee.capacity_sim(stream, group_cap=0.05)  # 17 USDT each vs 19.45 cap
    assert len(small) == 1
    wide = [{**_row("Z", 0, atr=2.0), "quality": 0.1}]  # 47 USDT worst case
    assert ee.capacity_sim(wide, group_cap=0.10) == []  # never reduced - rejected


def test_the_purge_drops_rows_whose_outcome_reaches_the_next_period():
    valid_from = int(datetime(2026, 9, 13, tzinfo=UTC).timestamp())
    assert ee.period(valid_from - 3600) is None
    assert ee.period(valid_from - 8 * 3600) == "TRAIN"
    assert ee.period(valid_from + 60) == "VALID"


def test_no_bot_module_imports_the_research_engine():
    offenders = []
    for p in (ROOT / "crypto_trading").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith("crypto_trading/entry_research/"):
            continue
        text = p.read_text(encoding="utf-8")
        if "event_engine" in text or "derivs_data" in text:
            offenders.append(rel)
    assert offenders == []


def test_the_engine_imports_nothing_that_trades():
    forbidden = (
        "connectors",
        "execution",
        "safety_kernel",
        "orchestrator",
        "paper_trading",
        "guardian",
        "godfather",
        "agents",
        "storage",
        "run",
        "discovery_loop",
    )
    for mod in ("event_engine.py", "derivs_data.py"):
        tree = ast.parse((ROOT / "crypto_trading/entry_research" / mod).read_text(encoding="utf-8"))
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        for name in names:
            if name.startswith("crypto_trading"):
                assert name.split(".")[1] == "entry_research", (mod, name)
            assert not any(name.split(".")[-1] == f for f in forbidden), (mod, name)
