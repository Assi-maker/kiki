"""Integrity tests for the 14-month regime lab (research only)."""

import ast
from pathlib import Path

from crypto_trading.entry_research import event_engine as ee
from crypto_trading.entry_research import regime_lab as rl

ROOT = Path(__file__).resolve().parents[3]
START = rl._ts(2026, 3, 2)  # aligned to 15 min


def _series(closes, spread=0.001):
    o = [c for c in closes]
    h = [c * (1 + spread) for c in closes]
    lo = [c * (1 - spread) for c in closes]
    return ee.Series5(START, o, h, lo, list(closes), [1.0] * len(closes))


def _geo(stop=1.0, tp=1.0, horizon=3600):
    return (stop, tp, horizon)


def test_entry_is_the_open_of_the_first_bar_starting_5_min_after_T():
    s = _series([100.0] * 40)
    s.o[12] = 101.0  # bar 12 starts at START + 3600
    T = START + 11 * rl.BAR_S  # bar 10 closes at T; bar 11 starts at T
    out = rl.outcome(s, T, 1.0, "LONG", _geo(horizon=600), ([], []))
    assert out[4] == START + 12 * rl.BAR_S


def test_stop_is_checked_before_target_inside_one_bar_and_costs_are_charged():
    s = _series([100.0] * 40)
    s.h[12], s.l[12] = 110.0, 90.0  # both barriers inside the entry bar
    out = rl.outcome(s, START + 11 * rl.BAR_S, 1.0, "LONG", _geo(), ([], []))
    r, reason = out[0], out[1]
    assert reason == "SL"
    # stop 1 % below 100, filled 0.15 % worse, 0.10 % fees: about -1.25 R
    assert -1.30 < r < -1.20


def test_settled_funding_inside_the_window_is_paid_by_longs_and_earned_by_shorts():
    s = _series([100.0] * 40)
    T = START + 11 * rl.BAR_S
    fund = ([START + 13 * rl.BAR_S], [0.001])  # 0.1 % settles while the trade is open
    long_ = rl.outcome(s, T, 1.0, "LONG", _geo(horizon=1800), fund)
    long0 = rl.outcome(s, T, 1.0, "LONG", _geo(horizon=1800), ([], []))
    short = rl.outcome(s, T, 1.0, "SHORT", _geo(horizon=1800), fund)
    short0 = rl.outcome(s, T, 1.0, "SHORT", _geo(horizon=1800), ([], []))
    assert long_[0] < long0[0]
    assert short[0] > short0[0]


def test_a_gap_or_an_uncovered_window_gives_no_outcome():
    s = _series([100.0] * 40)
    s.c[14] = None
    assert rl.outcome(s, START + 11 * rl.BAR_S, 1.0, "LONG", _geo(horizon=3600), ([], [])) is None
    s2 = _series([100.0] * 16)
    assert rl.outcome(s2, START + 11 * rl.BAR_S, 1.0, "LONG", _geo(horizon=3600), ([], [])) is None


def test_atr15_ignores_every_bar_at_or_after_T():
    closes = [100.0 + (i % 7) for i in range(80)]
    s1, s2 = _series(closes), _series(closes)
    T = START + 60 * rl.BAR_S
    for k in range(60, 80):
        s2.h[k], s2.l[k] = 1e6, 1e-6
    assert rl.atr15_pct(s1, T) == rl.atr15_pct(s2, T)


def test_trailing_median_needs_history_and_uses_only_past_samples():
    from collections import deque

    d = deque([1.0] * 239)
    assert rl._median_before(d, 5.0) is None
    d.append(1.0)
    assert rl._median_before(d, 5.0) == "hi"
    assert rl._median_before(d, 0.5) == "lo"


def test_the_purge_drops_rows_whose_outcome_reaches_the_next_period():
    valid_from = rl._ts(2026, 2, 1)
    assert rl.period(valid_from - 3600) is None
    assert rl.period(valid_from - 2 * rl.DAY) == "TRAIN"
    assert rl.period(valid_from + 60) == "VALID"
    assert rl.period(rl._ts(2026, 9, 29)) == "HOLDOUT"


def _row(sym, T, stop_atr=1.0, r=0.5, typ="BRK_4H", side="LONG", reg=None, hold=3600):
    out = (r, "TP", 1.0, -0.2, T + 300, T + 300 + hold)
    return (sym, T, typ, side, stop_atr, (out, out), reg or {})


def _table(rows):
    tab = rl.Table()
    tab.add(rows)
    return tab


def test_live_portfolio_max_4_open_one_per_symbol_and_no_percent_cap():
    T = START
    rows = [_row(f"S{i}", T + i, stop_atr=50.0) for i in range(6)]  # huge stops: no % cap
    rows.append(_row("S0", T + 10))  # same symbol while S0 is open
    rows.append(_row("S5", T + 5000))  # after the first four have closed
    tab = _table(rows)
    taken = rl.portfolio(tab, range(len(tab)), rl.PRIMARY)
    assert [tab.syms[tab.sym[i]] for i in taken] == ["S0", "S1", "S2", "S3", "S5"]
    assert tab.T[taken[-1]] == T + 5000


def test_rows_without_an_outcome_for_a_geometry_are_skipped_not_invented():
    out = (0.5, "TP", 1.0, -0.2, START + 300, START + 3900)
    tab = _table([("S0", START, "BRK_4H", "LONG", 1.0, (out, None), {})])
    assert rl.cell_stats(tab, [0], 1.0, "G_LEGACY") == {"n": 0}
    assert rl.cell_stats(tab, [0], 1.0, "G_WIDE")["n"] == 1


def test_market_regimes_are_attached_from_the_last_hour_at_or_before_T():
    T = START + 3600 + 1500  # 25 min after the hour
    tab = _table([_row("S0", T, reg={"sym_oi": "up"})])
    tab.attach_market({START + 3600: {"mkt_trend": "bull"}, START + 7200: {"mkt_trend": "bear"}})
    assert tab.reg[rl.DIM_IDX["mkt_trend"]][0] == rl.DIMS["mkt_trend"].index("bull")
    assert tab.reg[rl.DIM_IDX["sym_oi"]][0] == rl.DIMS["sym_oi"].index("up")


def test_selection_never_looks_at_test_or_holdout():
    days = rl.period_days()
    tr0, va0, te0 = rl._ts(2025, 9, 1), rl._ts(2026, 2, 10), rl._ts(2026, 5, 10)
    good = [_row(f"S{i % 50}", tr0 + i * 7200, r=0.8 if i % 3 else -0.4) for i in range(300)]
    good += [_row(f"S{i % 50}", va0 + i * 7200, r=0.8 if i % 3 else -0.4) for i in range(60)]
    good += [_row(f"S{i % 50}", te0 + i * 7200, r=-1.0) for i in range(100)]  # terrible TEST
    bad = [_row(f"S{i % 50}", tr0 + i * 7200, r=-0.8, typ="ACCEL") for i in range(300)]
    bad += [_row(f"S{i % 50}", te0 + i * 7200, r=5.0, typ="ACCEL") for i in range(100)]
    tab = _table(good + bad)
    survivors, _ = rl.select(tab, rl.build_cells(tab), days, rl.PRIMARY)
    assert ("BRK_4H", "LONG", "ALL", "ALL") in survivors
    assert not any(k[0] == "ACCEL" for k in survivors)  # great TEST numbers never count


def test_no_bot_module_imports_the_regime_lab_or_the_archive_fill():
    offenders = []
    for p in (ROOT / "crypto_trading").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        # evidence_shadow: separate research-side process reusing the research
        # feature code on purpose (never imported by the bot - tested).
        if rel.startswith(("crypto_trading/entry_research/", "crypto_trading/evidence_shadow/")):
            continue
        text = p.read_text(encoding="utf-8")
        if "regime_lab" in text or "archive_fill" in text:
            offenders.append(rel)
    assert offenders == []


def test_the_lab_imports_nothing_that_trades():
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
    for mod in ("regime_lab.py", "archive_fill.py"):
        tree = ast.parse((ROOT / "crypto_trading/entry_research" / mod).read_text(encoding="utf-8"))
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        for name in names:
            if name.startswith("crypto_trading"):
                assert name.split(".")[1] == "entry_research", (mod, name)
            assert not any(name.split(".")[-1] == f for f in forbidden), (mod, name)


def test_acceptance_needs_positive_expectancy_money_and_frequency_not_just_less_loss():
    ok = {"n": 400, "mean_r": 0.05, "net_usdt_day": 2.0, "per_day": 4.0, "p_pos": 0.01}
    assert rl.passes(ok, need_p=True)
    assert not rl.passes({**ok, "mean_r": -0.01}, need_p=False)  # "loses less" is not enough
    assert not rl.passes({**ok, "per_day": 2.9}, need_p=False)  # too few trades
    assert not rl.passes({**ok, "net_usdt_day": -0.1}, need_p=False)
    assert not rl.passes({**ok, "p_pos": 0.2}, need_p=True)
