from datetime import UTC, datetime, timedelta

import pytest

from crypto_trading.entry_research import stats as s


def test_t_sf_matches_known_quantiles():
    assert s.t_sf(0.0, 10) == pytest.approx(0.5)
    assert s.t_sf(2.228, 10) == pytest.approx(0.025, abs=5e-4)   # t(0.975, 10)
    assert s.t_sf(-1.96, 1e6) == pytest.approx(0.975, abs=1e-3)


def test_clustering_widens_uncertainty_for_duplicated_observations():
    vals = [1.0, -0.2, 0.8, 0.1, 0.5, -0.4, 0.9, 0.3]
    distinct = [f"c{i}" for i in range(len(vals))]
    p_indep = s.p_mean_positive(vals, distinct)
    # same 8 numbers, but each copied 5x inside its own cluster: n grows, info does not
    dup_vals = [v for v in vals for _ in range(5)]
    dup_cl = [c for c in distinct for _ in range(5)]
    p_dup = s.p_mean_positive(dup_vals, dup_cl)
    assert p_dup == pytest.approx(p_indep, rel=0.35)


def test_bh_is_monotone_and_bounded():
    q = s.bh([0.01, 0.04, 0.03, 0.5])
    assert q[0] == pytest.approx(0.04)
    assert all(0 <= x <= 1 for x in q)
    assert q[3] == pytest.approx(0.5)


def test_drawdown_profit_factor_and_top_trade_dependence():
    assert s.max_drawdown_r([1, -1, -1, 2, -3]) == -3
    assert s.profit_factor([2, -1, -1]) == 1.0
    carried = [-0.2] * 19 + [10.0]
    assert s.mean_without_top(carried) == pytest.approx(-0.2)


def test_summarize_reports_expectancy_not_just_win_rate():
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    recs = [{"r": r, "t0": t0 + timedelta(hours=5 * i), "mfe_pct": 1, "mae_pct": -1,
             "minutes_to_mfe": 10, "risk_pct": 2} for i, r in enumerate([0.1] * 9 + [-3.0])]
    out = s.summarize(recs)
    assert out["win_rate"] == 0.9 and out["mean_r"] < 0
    assert out["cost_r"] == pytest.approx(0.05)
