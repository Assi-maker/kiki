"""Edge lab (2026-09-29) - event vs state, signal age, interactions, regimes,
negative edge. Read-only research on the cached 1m klines; never LIVE.

Universe (VectorBT-style: every signal is evaluated on every bar, not only
when our screener fired): every symbol-day in data/entry_research/klines.db,
on a 5-minute grid. At grid time T the 30m indicators are computed the way
discovery sees them - the closed 30m bars plus the FORMING 30m bar built
from 1m bars strictly before T. Nothing at or after T is used (enforced by
tests/crypto_trading/entry_research/test_edge_lab.py: lookahead + recursive
stability checks in the spirit of Freqtrade's lookahead-analysis /
recursive-analysis).

STATE = a condition that is true (RSI > 70, volume z > 2.5, 30m move > 2 %,
above the 20-bar high, uptrend). EVENT = the state became true at this grid
point (false 5 min earlier). AGE = minutes since the state became
continuously true.

Outcome (pre-registered, identical to the entry research): entry at the
open of the 1m bar at T (+0) or at T + 23 min (our real pipeline latency);
stop = entry - 2 x ATR15, target = entry + 3 x ATR15, 6 h, 0.10 % fees +
0.15 % stop slippage. R after costs. Plus failure flags: SL, TIME, low MFE
(< 0.5 %), fast decay (MAE reaches -1 ATR15 before MFE reaches +1 ATR15).
"""
from __future__ import annotations

import bisect
import math
import sqlite3
import statistics as st
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

GRID_S = 300
BAR30_S = 1800
N30 = 30                  # 30m bars of history needed (15 h)
LATENCY_S = 23 * 60
HORIZON_S = 6 * 3600
FEE_RT = 0.001
STOP_SLIP = 0.0015
STOP_ATR, TARGET_ATR = 2.0, 3.0
STATE_THRESHOLDS = {"rsi": 70.0, "volz": 2.5, "move30": 2.0}


@dataclass
class Minute:
    ts: list[int]
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741
    c: list[float]
    v: list[float]


def load_minutes(db_path: str) -> dict[str, Minute]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out: dict[str, Minute] = {}
    for sym, in conn.execute("SELECT DISTINCT symbol FROM klines"):
        rows = conn.execute("SELECT t, o, h, l, c, v FROM klines WHERE symbol = ? ORDER BY t", (sym,)).fetchall()
        out[sym] = Minute([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                          [r[3] for r in rows], [r[4] for r in rows], [r[5] or 0.0 for r in rows])
    return out


# ------------------------------------------------------------------ 30m view at T

def precompute30(m: Minute) -> dict[int, tuple]:
    """Complete (>= 27 of 30 minutes) closed 30m buckets: start -> (o,h,l,c,v).
    A bucket only ever contains its own minutes, so reading it at T when
    start + 30 min <= T uses nothing at or after T."""
    out: dict[int, list] = {}
    for i, t in enumerate(m.ts):
        out.setdefault(t - t % BAR30_S, []).append(i)
    return {b: (m.o[idx[0]], max(m.h[i] for i in idx), min(m.l[i] for i in idx), m.c[idx[-1]],
                sum(m.v[i] for i in idx)) for b, idx in out.items() if len(idx) >= 27}


def bars30_at(m: Minute, T: int, pre: dict[int, tuple] | None = None) -> list[tuple] | None:
    """[(o,h,l,c,v)] - the last N30-1 CLOSED 30m bars + the forming bar, all
    built only from 1m bars with ts < T. The forming bar's volume is scaled
    to a full-bar pace. None if history is incomplete."""
    end = bisect.bisect_left(m.ts, T)                       # strictly before T
    cur_start = T - T % BAR30_S
    first_needed = cur_start - (N30 - 1) * BAR30_S
    if pre is not None:
        closed = [pre.get(first_needed + k * BAR30_S) for k in range(N30 - 1)]
        if any(b is None for b in closed):
            return None
        lo = bisect.bisect_left(m.ts, cur_start)
        idx = range(lo, end)
        if len(idx):
            frac = len(idx) / 30
            closed.append((m.o[lo], max(m.h[i] for i in idx), min(m.l[i] for i in idx), m.c[end - 1],
                           sum(m.v[i] for i in idx) / frac))
        return closed
    start = bisect.bisect_left(m.ts, first_needed)
    if start >= end or m.ts[start] > first_needed + 120:
        return None
    buckets: dict[int, list] = {}
    for i in range(start, end):
        b = m.ts[i] - m.ts[i] % BAR30_S
        buckets.setdefault(b, []).append(i)
    out = []
    for k in range(N30 - 1):
        b = first_needed + k * BAR30_S
        idx = buckets.get(b)
        if not idx or len(idx) < 27:
            return None
        out.append((m.o[idx[0]], max(m.h[i] for i in idx), min(m.l[i] for i in idx), m.c[idx[-1]],
                    sum(m.v[i] for i in idx)))
    idx = buckets.get(cur_start)
    if idx:
        frac = len(idx) / 30
        out.append((m.o[idx[0]], max(m.h[i] for i in idx), min(m.l[i] for i in idx), m.c[idx[-1]],
                    sum(m.v[i] for i in idx) / frac))
    return out


def features_from_bars(bars: list[tuple]) -> dict:
    """Pure function of the 30m view (closed + forming). No state carried
    between calls - so it is recursively stable by construction."""
    o, h, l, c, v = zip(*bars, strict=True)  # noqa: E741
    diffs = [c[i] - c[i - 1] for i in range(len(c) - 14, len(c))]
    gains, losses = sum(d for d in diffs if d > 0), -sum(d for d in diffs if d < 0)
    rsi = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    prev_v = v[-21:-1]
    sd = st.pstdev(prev_v)
    volz = (v[-1] - st.mean(prev_v)) / sd if sd > 0 else 0.0
    atr14 = st.mean((h[i] - l[i]) / c[i] for i in range(len(c) - 15, len(c) - 1)) * 100
    move30 = (c[-1] / o[-1] - 1) * 100
    ret_prev = (c[-2] / c[-3] - 1) * 100
    ret_30m = (c[-1] / c[-2] - 1) * 100
    return {
        "rsi": rsi, "volz": volz, "move30": move30, "ret_2h": (c[-1] / c[-5] - 1) * 100,
        "ret_4h": (c[-1] / c[-9] - 1) * 100, "accel": ret_30m - ret_prev, "atr30_pct": atr14,
        "range_exp": ((h[-1] - l[-1]) / c[-1] * 100) / atr14 if atr14 > 0 else 0.0,
        "breakout": c[-1] > max(h[-21:-1]), "trend_up": c[-1] > st.mean(c[-N30:]),
        "pos_20": (c[-1] - min(l[-21:-1])) / (max(h[-21:-1]) - min(l[-21:-1]) or 1),
    }


def states(f: dict) -> dict[str, bool]:
    return {"S_rsi": f["rsi"] > STATE_THRESHOLDS["rsi"], "S_volz": f["volz"] > STATE_THRESHOLDS["volz"],
            "S_move": f["move30"] > STATE_THRESHOLDS["move30"], "S_breakout": bool(f["breakout"]),
            "S_rangeexp": f["range_exp"] > 2.0}


def atr15_pct(m: Minute, T: int) -> float | None:
    end = bisect.bisect_left(m.ts, T)
    start = bisect.bisect_left(m.ts, T - 16 * 900 - (T % 900))
    buckets = defaultdict(list)
    for i in range(start, end):
        buckets[m.ts[i] - m.ts[i] % 900].append(i)
    ranges = [(max(m.h[i] for i in idx) - min(m.l[i] for i in idx)) / m.c[idx[-1]]
              for b, idx in sorted(buckets.items()) if len(idx) >= 12 and b + 900 <= T][-16:]
    return st.mean(ranges) * 100 if len(ranges) >= 12 else None


def outcome(m: Minute, T: int, latency_s: int, atr_pct: float, side: str = "LONG") -> dict | None:
    """Triple barrier after costs. SHORT mirrors LONG exactly: stop 2 x ATR15
    ABOVE entry (filled with slippage above), target 3 x ATR15 below; within a
    bar the stop is still checked first (conservative for both sides)."""
    i = bisect.bisect_left(m.ts, T + latency_s)
    if i >= len(m.ts) or m.ts[i] > T + latency_s + 120:
        return None
    entry, t_entry = m.o[i], m.ts[i]
    risk = STOP_ATR * atr_pct / 100 * entry
    sgn = 1 if side == "LONG" else -1
    stop, target = entry - sgn * risk, entry + sgn * TARGET_ATR * atr_pct / 100 * entry
    fav = adv = entry            # most favourable / most adverse price so far
    mfe_hit_at = mae_hit_at = None
    one_atr = atr_pct / 100 * entry
    j = i
    last_c = entry
    while j < len(m.ts) and m.ts[j] < t_entry + HORIZON_S:
        if m.ts[j] - (m.ts[j - 1] if j > i else t_entry) > 600:
            return None                                  # data gap - no fabricated outcome
        up, down = m.h[j], m.l[j]
        if sgn > 0:
            fav, adv = max(fav, up), min(adv, down)
        else:
            fav, adv = min(fav, down), max(adv, up)
        if mae_hit_at is None and sgn * (adv - entry) <= -one_atr:
            mae_hit_at = j
        if mfe_hit_at is None and sgn * (fav - entry) >= one_atr:
            mfe_hit_at = j
        if (sgn > 0 and down <= stop) or (sgn < 0 and up >= stop):
            px, reason = stop * (1 - sgn * STOP_SLIP), "SL"
            break
        if (sgn > 0 and up >= target) or (sgn < 0 and down <= target):
            px, reason = target, "TP"
            break
        last_c = m.c[j]
        j += 1
    else:
        if j == i or m.ts[j - 1] < t_entry + HORIZON_S - 900:
            return None                                  # window not covered by data
        px, reason = last_c, "TIME"
    r = (sgn * (px - entry) - FEE_RT * entry) / risk
    mfe = sgn * (fav / entry - 1) * 100
    return {"r": r, "reason": reason, "mfe_pct": mfe, "mae_pct": sgn * (adv / entry - 1) * 100,
            "low_mfe": mfe < 0.5,
            "fast_decay": mae_hit_at is not None and (mfe_hit_at is None or mae_hit_at < mfe_hit_at)}


# ------------------------------------------------------------------ universe build

def build_universe(minutes: dict[str, Minute], baseline_every: int = 12) -> list[dict]:
    """One row per (symbol, grid T) where any state is true, plus every
    `baseline_every`-th grid point regardless (unconditional baseline)."""
    rows = []
    for sym, m in minutes.items():
        if not m.ts:
            continue
        t0 = m.ts[0] - m.ts[0] % GRID_S + GRID_S
        pre = precompute30(m)
        prev_states: dict[str, bool] = {}
        since: dict[str, int] = {}
        k = 0
        for T in range(t0, m.ts[-1] - HORIZON_S, GRID_S):
            bars = bars30_at(m, T, pre)
            if bars is None:
                prev_states, since = {}, {}
                continue
            f = features_from_bars(bars)
            s = states(f)
            ages = {}
            for name, on in s.items():
                if on and not prev_states.get(name):
                    since[name] = T
                if not on:
                    since.pop(name, None)
                ages[name] = (T - since[name]) / 60 if on and name in since else None
            # an event needs a KNOWN previous point where the state was false
            events = {("E" + n[1:]): bool(on and (n in prev_states) and not prev_states[n])
                      for n, on in s.items()}
            prev_states = s
            k += 1
            if not any(s.values()) and k % baseline_every:
                continue
            a15 = atr15_pct(m, T)
            if not a15:
                continue
            fast = outcome(m, T, 0, a15)
            slow = outcome(m, T, LATENCY_S, a15)
            if fast is None or slow is None:
                continue
            short_fast = outcome(m, T, 0, a15, "SHORT")
            short_slow = outcome(m, T, LATENCY_S, a15, "SHORT")
            active_ages = [a for a in ages.values() if a is not None]
            rows.append({"symbol": sym, "T": T, "feat": {**f, **s, **events, "atr15_pct": a15,
                         "signal_age_min": min(active_ages) if active_ages else None,
                         **{f"age_{n[2:]}": a for n, a in ages.items()}},
                         "is_signal": any(s.values()), "out_fast": fast, "out_slow": slow,
                         "short_fast": short_fast, "short_slow": short_slow})
    return rows


def add_regimes(rows: list[dict], minutes: dict[str, Minute]) -> None:
    """Cross-sectional, point-in-time regimes at each T: BTC trend/vol,
    breadth (share of symbols up over 4 h), market vol (median ATR30),
    dispersion of 4 h returns (low = correlated market)."""
    by_T = defaultdict(list)
    for r in rows:
        by_T[r["T"]].append(r)
    btc = minutes.get("BTC-USDT")
    btc_pre = precompute30(btc) if btc else None
    for T, rs in by_T.items():
        rets = [r["feat"]["ret_4h"] for r in rs]
        vols = [r["feat"]["atr30_pct"] for r in rs]
        breadth = sum(x > 0 for x in rets) / len(rets) if len(rets) >= 5 else None
        disp = st.pstdev(rets) if len(rets) >= 5 else None
        mvol = st.median(vols) if len(vols) >= 5 else None
        b = bars30_at(btc, T, btc_pre) if btc else None
        bf = features_from_bars(b) if b else None
        for r in rs:
            r["feat"].update({"breadth_4h": breadth, "dispersion_4h": disp, "market_vol": mvol,
                              "btc_ret_4h": bf["ret_4h"] if bf else None,
                              "btc_atr30": bf["atr30_pct"] if bf else None,
                              "btc_trend_up": bf["trend_up"] if bf else None})


def utc(T: int) -> datetime:
    return datetime.fromtimestamp(T, UTC)


def split(rows: list[dict], valid_from: datetime, test_from: datetime) -> dict[str, list[dict]]:
    """Time split with PURGE: a row whose outcome window (T + latency + 6 h)
    reaches into the next period is dropped (no label leakage across the
    boundary - Lopez de Prado's purging/embargo)."""
    reach = timedelta(seconds=LATENCY_S + HORIZON_S)
    out = {"TRAIN": [], "VALID": [], "TEST": []}
    for r in rows:
        t = utc(r["T"])
        if t < valid_from:
            if t + reach < valid_from:
                out["TRAIN"].append(r)
        elif t < test_from:
            if t + reach < test_from:
                out["VALID"].append(r)
        else:
            out["TEST"].append(r)
    return out


def thin(rows: list[dict], minutes_apart: int = 60) -> list[dict]:
    """First row per symbol per `minutes_apart` window - overlapping 6 h
    outcomes of the same symbol are otherwise near-duplicates."""
    last: dict[str, int] = {}
    out = []
    for r in sorted(rows, key=lambda x: x["T"]):
        if r["T"] - last.get(r["symbol"], -10**12) >= minutes_apart * 60:
            out.append(r)
            last[r["symbol"]] = r["T"]
    return out


def as_pattern_rows(rows: list[dict], key: str = "out_slow") -> list[dict]:
    """Adapter to the entry_research.patterns protocol."""
    return [{"t0": utc(r["T"]), "feat": r["feat"], "symbol": r["symbol"],
             "outcomes": {"primary": {**r[key], "minutes_to_mfe": 0, "risk_pct": STOP_ATR * r["feat"]["atr15_pct"]}}}
            for r in rows]


def finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))
