"""Live evidence classifier - which historical evidence applies to a candidate.

For a candidate (symbol, side) at its DECISION time T it answers, with the
research's own code (event_engine features/conditions, regime_lab buckets)
and only data known at T:

- signal types: every event family whose event fired on the last closed 5m
  bar or up to 55 min before it (the research thinned events to one per
  hour, so an event that old still describes the candidate); none -> the
  candidate is matched with the random BASELINE ("NO_EVENT");
- symbol regimes at T: settled funding bucket, 24 h OI change bucket, ATR
  versus the median of the previous 30 days of hourly samples;
- market regimes at the last full hour <= T: BTC 7-day trend, BTC 24 h vol
  versus its previous 30 days, BTC OI 24 h change, breadth and median
  settled funding across the research universe.

Shadow-only. It never touches the bot database, an order or a position.
"""

from __future__ import annotations

import math
import statistics as st
from collections.abc import Callable
from dataclasses import dataclass, field

from crypto_trading.entry_research import event_engine as ee
from crypto_trading.entry_research import regime_lab as rl
from crypto_trading.evidence_shadow import binance_data as bd

BAR_S = 300
HOUR = 3600
DAY = 86400
EVENT_LOOKBACK_BARS = 12  # the last closed bar + 11 before it (55 min)
HISTORY_S = 31 * DAY + 2 * DAY  # 30-day vol baseline + feature warm-up
REF = "BTCUSDT"


def series_from_bars(bars: list[tuple]) -> ee.Series5 | None:
    if len(bars) < ee.HIST + 2:
        return None
    first = bars[0][0]
    n = (bars[-1][0] - first) // BAR_S + 1
    o, h, lo, c, v = ([None] * n for _ in range(5))
    for t, oo, hh, ll, cc, vv in bars:
        k = (t - first) // BAR_S
        o[k], h[k], lo[k], c[k], v[k] = oo, hh, ll, cc, vv
    return ee.Series5(first, o, h, lo, c, v)


@dataclass
class SymbolData:
    bars: list[tuple]
    funding: list[tuple]
    oi: list[tuple]


Fetch = Callable[[str, int], SymbolData]


def fetch_symbol(symbol: str, cutoff: int, history_s: int = HISTORY_S) -> SymbolData:
    start = cutoff - history_s
    return SymbolData(
        bd.klines_5m(symbol, start, cutoff),
        bd.funding(symbol, cutoff - 10 * DAY, cutoff),
        bd.open_interest(symbol, cutoff - 2 * DAY, cutoff),
    )


def _derivs(symbol: str, d: SymbolData, cutoff: int) -> ee.Derivs:
    """Defence in depth: drop anything stamped after the cutoff even if a
    fetcher returned it."""
    f = [(t, r) for t, r in d.funding if t <= cutoff]
    o = [(t, v) for t, v in d.oi if t <= cutoff]
    return ee.Derivs(
        {symbol: ([t for t, _ in f], [r for _, r in f])},
        {symbol: ([t for t, _ in o], [v for _, v in o])},
    )


@dataclass
class Classification:
    symbol: str
    side: str
    decision_time: int
    last_bar_close: int | None
    signal_types: list[str] = field(default_factory=list)
    regimes: dict[str, str | None] = field(default_factory=dict)
    error: str | None = None


def classify_symbol(symbol: str, side: str, T: int, data: SymbolData) -> Classification:
    T0 = T - T % BAR_S  # last 5m close at or before the decision time
    bars = [b for b in data.bars if b[0] + BAR_S <= T]  # closed bars only
    out = Classification(symbol, side, T, None)
    s = series_from_bars(bars)
    if s is None:
        out.error = "insufficient_bars"
        return out
    d = _derivs(symbol, data, T)
    p = ee.Prepared(s)
    k0 = s.index_closing_at(T0)
    if k0 < ee.HIST or k0 >= len(s.c):
        out.error = "no_closed_bar_at_decision_time"
        return out
    out.last_bar_close = s.close_time(k0)

    def feats(k: int) -> dict | None:
        f = ee.features_at(p, k)
        return None if f is None else ee.with_derivs(f, symbol, s.close_time(k), d)

    fired: list[str] = []
    for k in range(k0 - EVENT_LOOKBACK_BARS + 1, k0 + 1):
        f, prev = feats(k), feats(k - 1)
        if f is None:
            continue
        for name, sd in ee.events_at(f, prev):
            if sd == side and name not in fired:
                fired.append(name)
    out.signal_types = fired or ["NO_EVENT"]
    f0 = feats(k0)
    if f0 is None:
        out.error = "unclean_history_at_decision_time"
        return out
    samples = []
    for k in range(max(ee.HIST, k0 - 30 * 24 * 12), k0):
        if s.close_time(k) % HOUR == 0:
            fk = ee.features_at(p, k)
            if fk is not None:
                samples.append(fk["atr_l"])
    out.regimes = {
        "sym_funding": rl.fund_bucket(f0.get("fund")),
        "sym_oi": rl.oi_bucket(d.oi_change(symbol, T0, DAY), 0.10),
        "sym_vol": rl._median_before(samples, f0["atr_l"]),
    }
    return out


# ------------------------------------------------------------------ market regimes


def market_regime_at(
    H: int, btc: SymbolData, universe: dict[str, SymbolData]
) -> dict[str, str | None]:
    """Market regimes at full hour H from data <= H only (regime_lab rules)."""
    bars = [b for b in btc.bars if b[0] + BAR_S <= H]
    s = series_from_bars(bars)
    reg: dict[str, str | None] = {}
    if s is not None:
        k = s.index_closing_at(H)
        if 2016 <= k < len(s.c):
            c_now, c_7d = s.c[k], s.c[k - 2016]
            if c_now and c_7d:
                r7 = c_now / c_7d - 1
                reg["mkt_trend"] = "bull" if r7 >= 0.04 else "bear" if r7 <= -0.04 else "side"

            def vol_at(j: int) -> float | None:
                lrs = [
                    math.log(s.c[x] / s.c[x - 1])
                    for x in range(j - 287, j + 1)
                    if s.c[x] is not None and s.c[x - 1] is not None
                ]
                return st.pstdev(lrs) if len(lrs) > 250 else None

            hist = []
            for j in range(max(288, k - 30 * 24 * 12), k):
                if s.close_time(j) % HOUR == 0:
                    v = vol_at(j)
                    if v is not None:
                        hist.append(v)
            v_now = vol_at(k)
            reg["mkt_vol"] = rl._median_before(hist, v_now) if v_now is not None else None
    d = _derivs(REF, btc, H)
    reg["mkt_oi"] = rl.oi_bucket(d.oi_change(REF, H, DAY), 0.05)
    n = n_pos = 0
    funds = []
    for sym, sd in universe.items():
        closes = {b[0] + BAR_S: b[4] for b in sd.bars if b[0] + BAR_S <= H}
        if H in closes and H - 4 * HOUR in closes:
            n += 1
            n_pos += closes[H] / closes[H - 4 * HOUR] - 1 > 0
        fu = _derivs(sym, sd, H).funding_at(sym, H)
        if fu is not None:
            funds.append(fu)
    if n >= 20:
        reg["breadth"] = "hi" if n_pos / n > 0.5 else "lo"
    if len(funds) >= 20:
        reg["mkt_funding"] = rl.fund_bucket(st.median(funds))
    return reg


def fetch_universe_at(symbols: list[str], H: int) -> dict[str, SymbolData]:
    """Just enough per symbol for breadth (4 h return) and settled funding."""
    out = {}
    for sym in symbols:
        try:
            out[sym] = SymbolData(
                bd.klines_5m(sym, H - 5 * HOUR, H), bd.funding(sym, H - 10 * HOUR, H), []
            )
        except Exception:  # noqa: BLE001 - one missing symbol must not stop the hour
            continue
    return out
