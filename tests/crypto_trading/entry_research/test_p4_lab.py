"""Integrity tests for the P4 strategy-redesign lab (point-in-time, windows, stats)."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")  # research-only deps, not in the bot's venv
pytest.importorskip("scipy")
pytest.importorskip("pandas")
from scipy.ndimage import maximum_filter1d, minimum_filter1d  # noqa: E402

from crypto_trading.entry_research import p4_data as D  # noqa: E402
from crypto_trading.entry_research import p4_lab as P  # noqa: E402


def test_fwd_extreme_covers_exactly_the_next_hb_bars():
    rng = np.random.default_rng(1)
    A = rng.random((2, D.N)).astype("f4")
    for hb in (1, 3, 12, 288):
        mx = P.fwd_extreme(A, hb, maximum_filter1d)
        mn = P.fwd_extreme(A, hb, minimum_filter1d)
        for j in (0, 5, 1000, D.N - hb - 2):
            assert mx[0, j] == A[0, j + 1 : j + hb + 1].max()
            assert mn[1, j] == A[1, j + 1 : j + hb + 1].min()


def test_fwd_extreme_never_reads_the_entry_bar():
    A = np.zeros((1, D.N), dtype="f4")
    A[0, 100] = 5.0
    mx = P.fwd_extreme(A, 12, maximum_filter1d)
    assert mx[0, 100] == 0.0  # entry bar itself excluded
    assert mx[0, 99] == 5.0


def test_shift_is_backward_looking():
    a = np.arange(10, dtype="f4")[None, :]
    s = P.shift(a, 3)
    assert np.isnan(s[0, :3]).all()
    assert s[0, 5] == 2.0


def test_period_purge_blocks_windows_reaching_next_period():
    b = P.PERIOD_START[1]
    e = np.array([b - P.MAXH - 1, b - P.MAXH, b - 1, b, b + 5])
    per = P.period_of(e)
    assert per.tolist() == [0, -1, -1, 1, 1]
    last = np.array([P.PERIOD_END[3] - 1, P.PERIOD_END[3]])
    assert P.period_of(last).tolist() == [3, -1]
    # the last allowed entry still has its full 24 h window plus slow latency in range
    assert P.PERIOD_END[3] - 1 + P.LAT_SLOW + P.MAXH < D.N


def test_cooldown():
    assert P.cooldown(np.array([0, 3, 10, 11, 25]), 10).tolist() == [0, 10, 25]


def test_cluster_p_direction_and_clusters():
    v = np.r_[np.full(50, 0.01), np.full(50, 0.012)]
    g = np.repeat(np.arange(25), 4)
    m, p, G = P.cluster_p(v + np.tile([0.001, -0.001], 50), g)
    assert G == 25 and m > 0 and p < 0.01
    m2, p2, _ = P.cluster_p(-v, g)
    assert p2 > 0.99


def test_bh_monotone():
    q = P.bh(np.array([0.001, 0.02, 0.5, 0.04]))
    assert q[0] <= q[1] <= q[3] <= q[2]
    assert abs(q[0] - 0.004) < 1e-12


def test_cell_values_side_and_costs():
    ts = P.TradeSet("X", {}, np.array([0, 0], np.int32), np.array([10, 20], np.int32))
    ts.per_h = {
        "1h": {
            P.LAT: {"idx": np.array([0, 1]), "ret": np.array([0.01, -0.02]), "fund": np.array([0.0001, 0.0]), "e": np.array([11, 21]), "up": np.array([0.02, 0.0]), "dn": np.array([-0.01, -0.03])},
        }
    }
    cell = P.Cell("X", {}, "SHORT", [(0, -1)], ("1h",))
    v = P.cell_values(cell, [ts], "1h", P.LAT)
    # short: gross = -ret, a short RECEIVES positive funding
    assert np.allclose(v["gross"], [-0.01, 0.02])
    assert np.allclose(v["net"], [-0.01 - P.COST + 0.0001, 0.02 - P.COST])
    assert np.allclose(v["mfe"], [0.01, 0.03]) and np.allclose(v["mae"], [-0.02, 0.0])


def test_lab_is_not_imported_by_the_bot():
    import pathlib

    root = pathlib.Path(P.__file__).resolve().parents[1]
    for f in root.rglob("*.py"):
        if "entry_research" in f.parts:
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        assert "p4_lab" not in txt and "p4_data" not in txt, f


def test_dvol_hour_is_only_used_after_it_closes():
    from crypto_trading.entry_research import p4_external as X

    t = D.T0 + 3600_000  # hour [01:00, 02:00)
    s = X.hourly_series([[t, 0, 0, 0, 42.0]])
    k_end = (t + 3600_000 - D.T0) // D.STEP - 1  # 5m bar ending 02:00
    assert np.isnan(s[k_end - 1]) and s[k_end] == 42.0 and s[k_end + 50] == 42.0


def test_trailing_z_excludes_current_value():
    from crypto_trading.entry_research import p4_external as X

    x = np.zeros(D.N)
    x[11::12] = np.random.default_rng(0).normal(size=len(x[11::12]))
    x[11 + 12 * 5000] = 50.0
    z = X.trailing_z(x, 720)
    assert z[11 + 12 * 5000] > 20  # the spike itself is not in its own baseline
