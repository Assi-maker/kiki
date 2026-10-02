import math
import random

import pytest

from crypto_trading.trend.signal import (
    InsufficientHistoryError,
    TrendConfig,
    asset_signal,
    realised_vol,
    target_weights,
)


def cfg(**kw) -> TrendConfig:
    base = dict(mode="PAPER", assets=["A", "B"], lookbacks_days=[20, 50], vol_target_annual=0.4,
                vol_window_days=30, max_scale=2.0, rebalance_band=0.05, leverage=3,
                catastrophe_stop_pct=0.2, capital_cap_usdt=None, paper_start_equity=1000.0,
                fee_rate=0.0005, slippage_rate=0.0003)
    base.update(kw)
    return TrendConfig(**base)


def walk(n: int, drift: float, seed: int, vol: float = 0.03) -> list[float]:
    rng = random.Random(seed)
    out = [100.0]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + drift + rng.gauss(0, vol)))
    return out


def test_signal_is_mean_of_signs_clipped_at_zero():
    up = [float(i) for i in range(1, 80)]
    assert asset_signal(up, [20, 50]) == 1.0
    down = list(reversed(up))
    assert asset_signal(down, [20, 50]) == 0.0
    mixed = [100.0] * 30 + [50.0] * 29 + [60.0] * 21  # 20d up, 50d down
    assert asset_signal(mixed, [20, 50]) == 0.0  # (+1 - 1) / 2 = 0


def test_signal_half_when_one_lookback_up_one_flat():
    closes = [100.0] * 81
    closes[-1] = 101.0   # 20-day change: 101 vs 100 -> +1
    closes[-51] = 101.0  # 50-day change exactly zero -> sign 0
    assert asset_signal(closes, [20, 50]) == 0.5


def test_insufficient_history_raises():
    with pytest.raises(InsufficientHistoryError):
        asset_signal([1.0] * 50, [50])
    with pytest.raises(InsufficientHistoryError):
        realised_vol([1.0] * 20, 30)


def test_weights_bounded_by_max_scale_and_asset_count():
    c = cfg()
    flat = [100.0 * (1.0001 ** i) for i in range(120)]  # almost zero volatility, uptrend
    w = target_weights({"A": flat, "B": flat}, c)
    assert w["A"] == pytest.approx(c.max_scale / 2)
    assert sum(w.values()) <= c.max_scale + 1e-12


def test_vol_targeting_scales_inverse_to_volatility():
    c = cfg(assets=["A"])
    calm = walk(120, 0.004, 1, vol=0.01)
    wild = walk(120, 0.004, 1, vol=0.06)
    vc, vw = realised_vol(calm, 30), realised_vol(wild, 30)
    assert vc < vw
    w_calm = target_weights({"A": calm}, c)["A"]
    w_wild = target_weights({"A": wild}, c)["A"]
    if asset_signal(calm, c.lookbacks_days) == asset_signal(wild, c.lookbacks_days) > 0:
        assert w_wild < w_calm


def test_parity_with_research_implementation():
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    from crypto_trading.entry_research import p5_lab
    from crypto_trading.entry_research.p5_robust import trend_weights

    c = cfg(assets=["AUSDT", "BUSDT"])
    a, b = walk(400, 0.001, 3), walk(400, -0.0005, 4, vol=0.05)
    idx = pd.date_range("2024-01-01", periods=400, freq="D")
    close = pd.DataFrame({"AUSDT": a, "BUSDT": b}, index=idx)
    ret = close.pct_change(fill_method=None)
    ref = trend_weights(close, ret, ["AUSDT", "BUSDT"], [20, 50], "LF")
    assert p5_lab.vol_scale is not None
    for d in range(120, 400, 7):
        live = target_weights({"AUSDT": a[: d + 1], "BUSDT": b[: d + 1]}, c)
        for s in ("AUSDT", "BUSDT"):
            expected = ref[s].iloc[d]
            assert not math.isnan(expected)
            assert live[s] == pytest.approx(float(np.float64(expected)), abs=1e-9), (d, s)
