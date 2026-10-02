"""Target weights for the P5 trend strategy - a pure function of daily closes.

Must stay identical to the researched rule
(`entry_research/p5_robust.trend_weights`, ENS_BTC+ETH_L20/50_LF):

    sig_a   = mean over L of sign(close[d] / close[d-L] - 1), clipped at 0
    vol_a   = std(daily returns d-29..d, ddof=1) * sqrt(365)   (>= 20 returns)
    w_a     = sig_a * min(vol_target / vol_a, max_scale) / n_assets

w_a is the fraction of strategy equity held LONG in asset a over the next
day. `tests/crypto_trading/test_trend_signal.py` checks parity against the
pandas research implementation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import BaseModel, Field


class TrendConfig(BaseModel):
    mode: str = Field(pattern="^(PAPER|LIVE)$")
    assets: list[str]
    lookbacks_days: list[int]
    vol_target_annual: float = Field(gt=0, le=1.0)
    vol_window_days: int = Field(ge=20)
    max_scale: float = Field(gt=0, le=3.0)
    rebalance_band: float = Field(ge=0, lt=0.5)
    leverage: int = Field(ge=1, le=5)
    catastrophe_stop_pct: float = Field(gt=0.02, lt=0.30)
    capital_cap_usdt: float | None = None
    paper_start_equity: float = Field(gt=0)
    fee_rate: float = Field(ge=0, lt=0.01)
    slippage_rate: float = Field(ge=0, lt=0.01)


class InsufficientHistoryError(ValueError):
    pass


def asset_signal(closes: Sequence[float], lookbacks: Sequence[int]) -> float:
    last = closes[-1]
    total = 0.0
    for lb in lookbacks:
        if len(closes) <= lb:
            raise InsufficientHistoryError(f"need > {lb} closes, have {len(closes)}")
        change = last / closes[-1 - lb] - 1
        total += (change > 0) - (change < 0)
    return max(total / len(lookbacks), 0.0)


def realised_vol(closes: Sequence[float], window: int) -> float:
    rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - window), len(closes))]
    if len(rets) < 20:
        raise InsufficientHistoryError(f"need >= 20 returns for volatility, have {len(rets)}")
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365)


def target_weights(closes_by_asset: dict[str, Sequence[float]], cfg: TrendConfig) -> dict[str, float]:
    n = len(cfg.assets)
    out = {}
    for asset in cfg.assets:
        closes = closes_by_asset[asset]
        sig = asset_signal(closes, cfg.lookbacks_days)
        vol = realised_vol(closes, cfg.vol_window_days)
        scale = cfg.max_scale if vol == 0 else min(cfg.vol_target_annual / vol, cfg.max_scale)
        out[asset] = sig * scale / n
    return out
