import pytest

np = pytest.importorskip("numpy")

from crypto_trading.entry_research import original_bot_6m as M  # noqa: E402 - after the importorskip


def sym(closes, highs=None, lows=None, qv=None, fr=None):
    c = np.array(closes, dtype="f8")
    return M.Sym("XUSDT", c, np.array(highs if highs is not None else closes, "f8"),
                 np.array(lows if lows is not None else closes, "f8"),
                 np.array(qv if qv is not None else [1.0] * len(closes), "f8"),
                 np.zeros(len(closes)), np.array(fr if fr is not None else [0.0] * len(closes), "f8"))


def test_screener_has_no_lookahead():
    base = [100.0 * (1 + 0.0001 * (i % 3)) for i in range(400)]
    s1 = sym(base)
    future = base[:200] + [x * 1.5 for x in base[200:]]
    s2 = sym(future)
    evals = np.array([199, 200])
    t1, sc1, _ = M.screen(s1, evals)
    t2, sc2, _ = M.screen(s2, evals)
    assert t1[0] == t2[0] and sc1[0] == sc2[0]  # e=199 sees bars < 199 only


def test_screener_price_trigger_uses_in_progress_bar():
    closes = [100.0] * 300
    closes[296] = 103.0  # inside the current 30m bar (bars 294..299)
    s = sym(closes)
    t, sc, mask = M.screen(s, np.array([297]))
    assert t[0] and mask[0] & 1 and sc[0] > 0


def test_funding_trigger_uses_last_settled_rate_only():
    qv = [1.0 + 0.5 * (i % 4) for i in range(300)]          # varied volume: no zero-variance artefact
    fr = [0.0] * 290 + [0.001] * 10                         # 0.10 % > 0.05 % threshold from bar 290
    s = sym([100.0] * 300, qv=qv, fr=fr)
    t, _, mask = M.screen(s, np.array([289, 295]))          # e=289 sees fr[288]=0, e=295 sees fr[294]
    assert not (mask[0] & 8)
    assert t[1] and mask[1] & 8


def test_simulate_stop_before_target_and_costs():
    closes = [100.0] * 20
    highs = [100.0] * 20
    lows = [100.0] * 20
    highs[11], lows[11] = 106.0, 94.0  # both SL and TP inside one bar -> SL
    r = M.simulate(sym(closes, highs, lows), 10, 4.0, 5.0, pp=False)
    assert r["reason"] == "SL"
    assert r["net"] < -40
    assert r["net"] == pytest.approx(r["gross"] - r["fees"] - r["slippage"] - r["funding"])


def test_pp_moves_stop_to_entry_from_next_bar():
    closes = [100.0] * 30
    highs = [100.0] * 30
    lows = [100.0] * 30
    highs[11] = 101.5        # +1 % reached
    lows[12] = 99.0          # next bar dips below entry -> BE exit with PP, nothing without
    on = M.simulate(sym(closes, highs, lows), 10, 4.0, 5.0, pp=True)
    off = M.simulate(sym(closes, highs, lows), 10, 4.0, 5.0, pp=False)
    assert on["reason"] == "BE"
    assert off["reason"] == "TIME"


def test_drop_top():
    trades = [{"net": x} for x in (50, 10, -5, 30)]
    assert M.drop_top(trades, 1) == 35
    assert M.drop_top(trades, 3) == -5
