import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from crypto_trading.entry_research import final_validation as F  # noqa: E402 - after the importorskip


def make_data(days=60, price=100.0, funding_day=0.0003, spot_mult=1.0, spike_day=None, spike=0.0):
    idx = pd.date_range("2024-01-01", periods=days, freq="D")
    cols = ["BTCUSDT", "ETHUSDT"]
    pc = pd.DataFrame(price, index=idx, columns=cols)
    ph = pc.copy()
    if spike_day is not None:
        ph.iloc[spike_day] = price * (1 + spike)
    qv = pd.DataFrame(1e9, index=idx, columns=cols)
    sc = pc * spot_mult
    fund = pd.DataFrame(funding_day, index=idx, columns=cols)
    return F.Data(pc, ph, qv, sc, fund)


SPEC = next(s for s in F.CARRY_SPECS if s.name == "CARRY_MAJORS_T0")


def test_flat_prices_positive_funding_earns_funding_minus_entry_costs():
    d = make_data()
    r = F.run_carry(d, F.Scenario(), SPEC)
    capn = 1 + 1 / 2.0
    notional = 1.0 / capn  # whole capital split over 2 slots, 1/capn of each slot is notional
    held_days = (r.util > 0).sum()
    expected_funding = notional * 0.0003 * (held_days - 0)  # paid over the held days
    entry = notional * (F.leg_cost(F.Scenario(), True, True) + F.leg_cost(F.Scenario(), True, False))
    assert r.extra["liquidations"] == 0
    assert r.costs.sum() == pytest.approx(entry)
    assert r.extra["funding"] == pytest.approx(expected_funding, rel=0.05)
    assert r.extra["basis"] == pytest.approx(0.0)


def test_negative_funding_never_enters():
    d = make_data(funding_day=-0.0003)
    r = F.run_carry(d, F.Scenario(), SPEC)
    assert r.trades == 0 and r.daily.abs().sum() == 0


def test_funding_shift_scenario_turns_signal_negative():
    d = make_data(funding_day=0.0001)
    r = F.run_carry(d, F.Scenario(funding_shift_day=-0.0003), SPEC)
    assert r.trades == 0


def test_liquidation_on_perp_spike():
    d = make_data(spike_day=30, spike=0.50)
    r = F.run_carry(d, F.Scenario(), SPEC)
    assert r.extra["liquidations"] == 2  # both BTC and ETH legs
    assert r.daily.iloc[30] < -0.1


def test_mismatched_pair_is_never_traded():
    d = make_data(spot_mult=1.10)
    r = F.run_carry(d, F.Scenario(), SPEC)
    assert r.trades == 0


def test_apply_band_executes_exits_and_skips_small_changes():
    w = pd.DataFrame({"a": [0.5, 0.52, 0.6, 0.0, 0.01]})
    out = F.apply_band(w, 0.05)["a"].tolist()
    assert out == [0.5, 0.5, 0.6, 0.0, 0.0]


def test_metrics_drawdown_duration():
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    daily = pd.Series([0.1, -0.05, -0.05, 0.02, 0.2, 0.0], index=idx)
    z = pd.Series(0.0, index=idx)
    r = F.Result("x", daily, daily, z, z, z, 0, {})
    m = F.metrics(r, "2024-01-01", "2024-01-06")
    assert m["dd_days"] == 3
    assert m["pos_days"] == 3
    assert m["mdd"] == pytest.approx(0.95 * 0.95 - 1)


def test_bh_controls_false_discoveries():
    assert F.bh({"a": 0.001, "b": 0.5, "c": 0.04}, 0.10) == {"a", "c"}
