"""Regime lab - signal type x market/derivatives regime over 14 months (2026-09-30).

    python -m crypto_trading.entry_research.regime_lab

Read-only research on the Binance USD-M public archive (archive_fill.py).
Nothing in the bot imports this module; it can never open, size or veto a
LIVE trade.

Question (user, 2026-09-30): is there a regime (bull / bear / sideways, high /
low volatility, funding, open interest) in which a signal type has POSITIVE
out-of-sample expectancy after costs, at a meaningful trade frequency? A
filter that only loses less by trading less is NOT accepted.

Signals: the 17 point-in-time event families x LONG/SHORT of event_engine
(identical definitions and features), plus a BASELINE (every symbol, both
sides, every 8 h) as the no-skill reference. RS_LEADER (cross-sectional,
needs every symbol at 5m resolution in memory) is left out; it was NOISE in
the September study.

Point-in-time contract (same as event_engine)
- 5m bar k covers [start_k, start_k + 5 min) and is used only at T >= its close.
- Open interest (archive `metrics`, 5m snapshots) only if stamped <= T - 5 min;
  funding only once settled (<= T).
- Market regimes are computed on the hourly grid at hour H from data <= H and
  attached to a signal at T via the last hour H <= T.
- Trailing 30-day medians (volatility regimes) use only samples before T.

Outcome, fixed before any result was seen
- Entry: open of the first 5m bar starting >= T + 5 min.
- PRIMARY geometry G_WIDE: stop 4 x ATR15, target 6 x ATR15, 24 h (close to the
  LIVE Risk Agent's median stop of ~3.8 x ATR15). Sensitivity G_LEGACY: stop 2,
  target 3 x ATR15, 6 h (every earlier study).
- Within a 5m bar the stop is checked first (conservative). A missing bar in
  the window drops the row (no fabricated outcome).
- Costs: 0.10 % fees round trip + 0.15 % stop slippage + the REAL settled
  funding of every settlement inside the holding window. Result in R and in
  USDT at the fixed LIVE size (1000 notional).

Regimes (thresholds fixed a priori)
- mkt_trend    BTC 7-day return: bull >= +4 %, bear <= -4 %, else side
- mkt_vol      BTC 24 h realised vol vs its trailing 30-day median: hi / lo
- breadth      share of symbols with a positive 4 h return: hi (> 0.5) / lo
- mkt_funding  median settled funding across symbols: neg (< 0) / base / hot (> 0.01 %)
- mkt_oi       BTC open interest 24 h change: up (> +5 %) / down (< -5 %) / flat
- sym_funding  the symbol's settled funding: neg / base / hot (same cuts)
- sym_oi       the symbol's open interest 24 h change: up (> +10 %) / down (< -10 %) / flat
- sym_vol      the symbol's 24 h ATR vs its trailing 30-day median: hi / lo

Periods (purged: a row whose outcome window reaches the next period is dropped)
  TRAIN   2025-08-01 .. 2026-01-31   choose
  VALID   2026-02-01 .. 2026-04-30   confirm
  TEST    2026-05-01 .. 2026-07-31   out-of-sample test, touched once
  HOLDOUT 2026-08-01 .. 2026-09-29   completely unseen final check

Pre-registered acceptance (selection uses TRAIN + VALID only)
1. TRAIN: cell n >= 100, mean R > 0, Benjamini-Hochberg q < 0.10 over every
   TRAIN cell (4 h-clustered one-sided p).
2. VALID: the same cell mean R > 0, n >= 30, p < 0.10.
   The selection is written to regime_lab_selection.json (sha256) BEFORE any
   TEST or HOLDOUT statistic is computed.
3. TEST: the union of surviving cells, run through the LIVE portfolio rules
   (fixed 1000 notional, max 4 open, 1 per symbol - no % caps), must have
   mean R > 0 with p < 0.05, net USDT/day > 0 AND >= 3 executed trades/day.
   Positive expectancy is required in absolute terms - "less negative than
   unfiltered" does not pass.
4. HOLDOUT (completely unseen): the same frozen selection must again have
   mean R > 0, net USDT/day > 0 and >= 3 trades/day.
ACCEPTED = 3 and 4 both pass, for the PRIMARY geometry only (G_LEGACY is a
sensitivity check and can never be accepted - no second chance).
Every cell's numbers for every period are published (CSV) for transparency,
but no TEST or HOLDOUT number is used to choose anything.

Known limits: survivorship (universe = symbols listed today), Binance prices
and funding as a proxy for BingX, no AI step in the replay (the AI is not
replayable over 14 months), 5m bars (intrabar order unknown -> stop first).
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import sqlite3
import statistics as st
import sys
from array import array
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research import event_engine as ee
from crypto_trading.entry_research.stats import bh, cluster_bootstrap_ci, p_mean_positive

ARCHIVE_DB = "data/entry_research/archive.db"
OUT = Path("data/entry_research")
REPORT_CSV = Path("docs/superpowers/reports/2026-09-30-regime-lab-cells.csv")
BAR_S = 300
HOUR = 3600
DAY = 86400
LAT_S = 5 * 60
FEE_RT = 0.001
STOP_SLIP = 0.0015
NOTIONAL = 1000.0
EQUITY_REF = 389.0
BASELINE_EVERY_S = 8 * HOUR  # memory: 2 rows per symbol per 8 h is plenty for a reference
THIN_S = 60 * 60
REF = "BTCUSDT"
GEOMETRIES = {"G_WIDE": (4.0, 6.0, 24 * HOUR), "G_LEGACY": (2.0, 3.0, 6 * HOUR)}
PRIMARY = "G_WIDE"
MAX_HORIZON = max(g[2] for g in GEOMETRIES.values())
WORKERS = 4  # ~0.3 GB per symbol scan; the machine has 8 GB and the bot runs beside it


def _ts(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp())


PERIODS = (
    ("TRAIN", _ts(2025, 8, 1), _ts(2026, 2, 1)),
    ("VALID", _ts(2026, 2, 1), _ts(2026, 5, 1)),
    ("TEST", _ts(2026, 5, 1), _ts(2026, 8, 1)),
    ("HOLDOUT", _ts(2026, 8, 1), _ts(2026, 9, 30)),
)
PERIOD_NAMES = tuple(p[0] for p in PERIODS)

FUND_BASE_MAX = 0.0001
DIMS = {
    "mkt_trend": ("bull", "bear", "side"),
    "mkt_vol": ("hi", "lo"),
    "breadth": ("hi", "lo"),
    "mkt_funding": ("neg", "base", "hot"),
    "mkt_oi": ("up", "down", "flat"),
    "sym_funding": ("neg", "base", "hot"),
    "sym_oi": ("up", "down", "flat"),
    "sym_vol": ("hi", "lo"),
}

# scan_symbol() row: (symbol, T, type, side, atr15 %, outcomes, symbol regimes)
# outcomes: tuple in GEOMETRIES order of (r, reason, mfe_r, mae_r, entry_ts, exit_ts) | None
GEO_IDX = {g: i for i, g in enumerate(GEOMETRIES)}
DIM_NAMES = tuple(DIMS)
DIM_IDX = {d: i for i, d in enumerate(DIM_NAMES)}


def period(T: int) -> str | None:
    for i, (name, a, b) in enumerate(PERIODS):
        if a <= T < b:
            nxt = PERIODS[i + 1][1] if i + 1 < len(PERIODS) else None
            if nxt is not None and T + LAT_S + MAX_HORIZON >= nxt:
                return None  # purged: the outcome window reaches the next period
            return name
    return None


def period_days() -> dict[str, float]:
    return {name: (b - a) / DAY for name, a, b in PERIODS}


def fund_bucket(x: float | None) -> str | None:
    if x is None:
        return None
    return "neg" if x < 0 else "hot" if x > FUND_BASE_MAX else "base"


def oi_bucket(x: float | None, cut: float) -> str | None:
    if x is None:
        return None
    return "up" if x > cut else "down" if x < -cut else "flat"


# ------------------------------------------------------------------ data


def load_series(conn: sqlite3.Connection, sym: str) -> ee.Series5 | None:
    rows = conn.execute(
        "SELECT t, o, h, l, c, v FROM klines5m WHERE symbol = ? ORDER BY t", (sym,)
    ).fetchall()
    if len(rows) < ee.HIST + 10:
        return None
    first = rows[0][0] // 1000
    n = (rows[-1][0] // 1000 - first) // BAR_S + 1
    o: list = [None] * n
    h: list = [None] * n
    l: list = [None] * n  # noqa: E741
    c: list = [None] * n
    v: list = [None] * n
    for t, oo, hh, ll, cc, vv in rows:
        k = (t // 1000 - first) // BAR_S
        o[k], h[k], l[k], c[k], v[k] = oo, hh, ll, cc, vv
    return ee.Series5(first, o, h, l, c, v)


def load_derivs(conn: sqlite3.Connection, syms: list[str]) -> ee.Derivs:
    f: dict = defaultdict(lambda: ([], []))
    o: dict = defaultdict(lambda: ([], []))
    ph = ",".join("?" * len(syms))
    for sym, t, r in conn.execute(
        f"SELECT symbol, t, rate FROM funding WHERE symbol IN ({ph}) ORDER BY symbol, t", syms
    ):
        f[sym][0].append(t // 1000)
        f[sym][1].append(r)
    for sym, t, v in conn.execute(
        f"SELECT symbol, t, oi_value FROM metrics WHERE symbol IN ({ph}) AND oi_value IS NOT NULL"
        " ORDER BY symbol, t",
        syms,
    ):
        o[sym][0].append(t // 1000)
        o[sym][1].append(v)
    return ee.Derivs(dict(f), dict(o))


# ------------------------------------------------------------------ outcome


def atr15_pct(s: ee.Series5, T: int) -> float | None:
    """Mean range of the last 16 complete 15-min buckets closed by T (3 x 5m
    bars each), in % of the bucket's close - same definition as edge_lab."""
    end = T - T % 900
    ranges = []
    for j in range(16):
        b0 = end - 900 * (j + 1)
        k0 = (b0 - s.start) // BAR_S
        if k0 < 0:
            break
        idx = [k0, k0 + 1, k0 + 2]
        if idx[-1] >= len(s.c) or any(s.c[i] is None for i in idx):
            continue
        ranges.append((max(s.h[i] for i in idx) - min(s.l[i] for i in idx)) / s.c[idx[-1]])
    return st.mean(ranges) * 100 if len(ranges) >= 12 else None


def outcome(
    s: ee.Series5,
    T: int,
    atr_pct: float,
    side: str,
    geometry: tuple[float, float, int],
    funding: tuple[list[int], list[float]],
) -> tuple | None:
    stop_atr, tp_atr, horizon = geometry
    i = (T + LAT_S - s.start + BAR_S - 1) // BAR_S  # first bar starting >= T + 5 min
    if i >= len(s.o) or s.o[i] is None:
        return None
    entry = s.o[i]
    t_entry = s.start + i * BAR_S
    risk = stop_atr * atr_pct / 100 * entry
    sgn = 1 if side == "LONG" else -1
    stop = entry - sgn * risk
    target = entry + sgn * tp_atr * atr_pct / 100 * entry
    fav = adv = entry
    j = i
    last_c = entry
    end = t_entry + horizon
    while j < len(s.c) and s.start + j * BAR_S < end:
        if s.c[j] is None:
            return None  # data gap - no fabricated outcome
        up, down = s.h[j], s.l[j]
        if sgn > 0:
            fav, adv = max(fav, up), min(adv, down)
        else:
            fav, adv = min(fav, down), max(adv, up)
        if (sgn > 0 and down <= stop) or (sgn < 0 and up >= stop):
            px, reason, exit_ts = stop * (1 - sgn * STOP_SLIP), "SL", s.start + (j + 1) * BAR_S
            break
        if (sgn > 0 and up >= target) or (sgn < 0 and down <= target):
            px, reason, exit_ts = target, "TP", s.start + (j + 1) * BAR_S
            break
        last_c = s.c[j]
        j += 1
    else:
        if s.start + j * BAR_S < end:
            return None  # window not covered by data
        px, reason, exit_ts = last_c, "TIME", end
    pnl = sgn * (px - entry) - FEE_RT * entry
    fts, frs = funding
    a = bisect.bisect_right(fts, t_entry)
    b = bisect.bisect_right(fts, exit_ts)
    pnl -= sum(sgn * frs[x] * entry for x in range(a, b))  # longs pay positive funding
    return (
        round(pnl / risk, 5),
        reason,
        round(sgn * (fav - entry) / risk, 4),
        round(sgn * (adv - entry) / risk, 4),
        t_entry,
        exit_ts,
    )


# ------------------------------------------------------------------ per-symbol scan


def _median_before(samples: deque, x: float) -> str | None:
    if len(samples) < 240:  # >= 10 days of hourly samples
        return None
    return "hi" if x > st.median(samples) else "lo"


def scan_symbol(sym: str) -> tuple[list[tuple], list[tuple]]:
    """Events + 4-hourly baseline for one symbol, with outcomes and symbol
    regimes; plus hourly (T, ret48, funding) points for the market regimes."""
    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    s = load_series(conn, sym)
    if s is None:
        return [], []
    d = load_derivs(conn, [sym])
    conn.close()
    fund = d.funding.get(sym, ([], []))
    p = ee.Prepared(s)
    atr_hist: deque = deque(maxlen=30 * 24)
    last_emit: dict[tuple, int] = {}
    rows, xs = [], []
    prev = None
    for k in range(ee.HIST, len(s.c)):
        f = ee.features_at(p, k)
        T = s.close_time(k)
        if f is None:
            prev = None
            continue
        f = ee.with_derivs(f, sym, T, d)
        hits = [e for e in ee.events_at(f, prev) if T - last_emit.get(e, -(10**12)) >= THIN_S]
        if T % BASELINE_EVERY_S == 0:
            hits += [("BASELINE", "LONG"), ("BASELINE", "SHORT")]
        if hits:
            reg = {
                "sym_funding": fund_bucket(f.get("fund")),
                "sym_oi": oi_bucket(d.oi_change(sym, T, DAY), 0.10),
                "sym_vol": _median_before(atr_hist, f["atr_l"]),
            }  # market dims are added in attach_market()
            a = atr15_pct(s, T)
            if a:
                for name, side in hits:
                    last_emit[(name, side)] = T
                    outs = tuple(outcome(s, T, a, side, geo, fund) for geo in GEOMETRIES.values())
                    if outs[GEO_IDX[PRIMARY]] is None:
                        continue
                    rows.append((sym, T, name, side, round(a, 5), outs, reg))
        if T % HOUR == 0:
            atr_hist.append(f["atr_l"])
            xs.append((T, f["ret48"], f.get("fund")))
        prev = f
    return rows, xs


# ------------------------------------------------------------------ market regimes


def add_cross_section(by_T: dict[int, list], xs: list[tuple]) -> None:
    """Aggregate one symbol's hourly (T, ret48, funding) points in place:
    T -> [n, n with ret48 > 0, fundings]. Keeps memory flat."""
    for T, r48, fu in xs:
        agg = by_T.get(T)
        if agg is None:
            agg = by_T[T] = [0, 0, array("d")]
        agg[0] += 1
        agg[1] += r48 > 0
        if fu is not None:
            agg[2].append(fu)


def market_regimes(by_T: dict[int, list], btc_oi: tuple[list[int], list[float]]) -> dict[int, dict]:
    """Hourly market regime from BTC (trend, vol, OI) + cross-section (breadth,
    median funding). Value at hour H uses only data <= H."""
    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    s = load_series(conn, REF)
    conn.close()
    out: dict[int, dict] = {}
    vol_hist: deque = deque(maxlen=30 * 24)
    d = ee.Derivs({}, {REF: btc_oi})
    for k in range(2016, len(s.c)):
        T = s.close_time(k)
        if T % HOUR:
            continue
        c_now, c_7d = s.c[k], s.c[k - 2016]
        lrs = [
            math.log(s.c[j] / s.c[j - 1])
            for j in range(k - 287, k + 1)
            if s.c[j] is not None and s.c[j - 1] is not None
        ]
        reg: dict = {}
        if c_now and c_7d:
            r7 = c_now / c_7d - 1
            reg["mkt_trend"] = "bull" if r7 >= 0.04 else "bear" if r7 <= -0.04 else "side"
        if len(lrs) > 250:
            vol = st.pstdev(lrs)
            reg["mkt_vol"] = _median_before(vol_hist, vol)
            vol_hist.append(vol)
        reg["mkt_oi"] = oi_bucket(d.oi_change(REF, T, DAY), 0.05)
        agg = by_T.get(T)
        if agg is not None and agg[0] >= 20:
            reg["breadth"] = "hi" if agg[1] / agg[0] > 0.5 else "lo"
            reg["mkt_funding"] = fund_bucket(st.median(agg[2])) if len(agg[2]) >= 20 else None
        out[T] = reg
    return out


# ------------------------------------------------------------------ columnar table


TYPES = ("BASELINE", "ANY", *ee.CONDITIONS)
TYPE_IDX = {t: i for i, t in enumerate(TYPES)}
SIDES = ("LONG", "SHORT")
REASONS = ("SL", "TP", "TIME")
NONE = -1
NAN = float("nan")


class Table:
    """Columnar rows (~100 bytes each instead of ~770 as tuples): the full
    14-month universe fits in memory next to the running bot."""

    def __init__(self) -> None:
        self.syms: list[str] = []
        self.sym_idx: dict[str, int] = {}
        self.sym = array("H")
        self.T = array("q")
        self.typ = array("B")
        self.side = array("B")
        self.stop = array("d")  # ATR15 % (the stop is GEOMETRIES[g][0] x this)
        self.entry = array("q")
        self.r = [array("d") for _ in GEOMETRIES]  # NaN = no outcome for this geometry
        self.reason = [array("b") for _ in GEOMETRIES]
        self.mfe = [array("d") for _ in GEOMETRIES]
        self.mae = [array("d") for _ in GEOMETRIES]
        self.exit = [array("q") for _ in GEOMETRIES]
        self.reg = [array("b") for _ in DIMS]  # index into DIMS[dim] or NONE
        self.per = array("b")  # index into PERIOD_NAMES or NONE (purged / outside)

    def __len__(self) -> int:
        return len(self.T)

    def add(self, rows: list[tuple]) -> None:
        """rows as produced by scan_symbol(); the regime may be a dict (symbol
        dims only, market dims added later) or a full tuple in DIMS order."""
        for sym, T, typ, side, stop, outs, reg in rows:
            if sym not in self.sym_idx:
                self.sym_idx[sym] = len(self.syms)
                self.syms.append(sym)
            self.sym.append(self.sym_idx[sym])
            self.T.append(T)
            self.typ.append(TYPE_IDX[typ])
            self.side.append(SIDES.index(side))
            self.stop.append(stop)
            first = next(o for o in outs if o is not None)
            self.entry.append(first[4])
            for gi, o in enumerate(outs):
                self.r[gi].append(o[0] if o else NAN)
                self.reason[gi].append(REASONS.index(o[1]) if o else NONE)
                self.mfe[gi].append(o[2] if o else NAN)
                self.mae[gi].append(o[3] if o else NAN)
                self.exit[gi].append(o[5] if o else 0)
            if isinstance(reg, dict):
                reg = tuple(reg.get(d) for d in DIM_NAMES)
            for di, val in enumerate(reg):
                self.reg[di].append(DIMS[DIM_NAMES[di]].index(val) if val is not None else NONE)
            p = period(T)
            self.per.append(PERIOD_NAMES.index(p) if p else NONE)

    def attach_market(self, mkt: dict[int, dict]) -> None:
        market_dims = [d for d in DIM_NAMES if not d.startswith("sym_")]
        for i in range(len(self)):
            m = mkt.get(self.T[i] - self.T[i] % HOUR)
            if not m:
                continue
            for d in market_dims:
                val = m.get(d)
                if val is not None:
                    self.reg[DIM_IDX[d]][i] = DIMS[d].index(val)


# ------------------------------------------------------------------ evaluation

GEO_STOP = tuple(g[0] for g in GEOMETRIES.values())


def pnl_usdt(tab: Table, i: int, gi: int) -> float:
    return tab.r[gi][i] * GEO_STOP[gi] * tab.stop[i] / 100 * NOTIONAL


def portfolio(tab: Table, idx, geometry: str, max_open: int = 4) -> list[int]:
    """LIVE rules: fixed 1000 notional, max `max_open` open, 1 per symbol, no
    % caps (user decision 2026-09-30). First come first served in time."""
    gi = GEO_IDX[geometry]
    open_: list[tuple[int, int]] = []  # (symbol, exit_ts)
    taken = []
    for i in sorted(idx, key=lambda j: tab.T[j]):
        if tab.r[gi][i] != tab.r[gi][i]:  # NaN: no outcome
            continue
        entry = tab.entry[i]
        open_ = [x for x in open_ if x[1] > entry]
        if len(open_) >= max_open or any(x[0] == tab.sym[i] for x in open_):
            continue
        open_.append((tab.sym[i], tab.exit[gi][i]))
        taken.append(i)
    return taken


def cell_stats(tab: Table, idx, days: float, geometry: str, with_ci: bool = False) -> dict:
    gi = GEO_IDX[geometry]
    r, mfe, mae, ex = tab.r[gi], tab.mfe[gi], tab.mae[gi], tab.exit[gi]
    ids = [i for i in idx if r[i] == r[i]]
    if not ids:
        return {"n": 0}
    rs = [r[i] for i in ids]
    cl = [tab.T[i] // (4 * HOUR) for i in ids]  # 4 h UTC blocks, = stats.block_of
    usdt = [pnl_usdt(tab, i, gi) for i in ids]
    eq = peak = dd = 0.0
    for _, u in sorted(zip([ex[i] for i in ids], usdt, strict=True)):
        eq += u
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    live = portfolio(tab, ids, geometry)
    res = {
        "n": len(rs),
        "per_day": round(len(rs) / days, 2),
        "win_rate": round(sum(x > 0 for x in rs) / len(rs), 3),
        "mean_r": round(st.mean(rs), 4),
        "p_pos": round(p_mean_positive(rs, cl), 5) if len(rs) >= 5 else 1.0,
        "net_usdt": round(sum(usdt), 1),
        "net_usdt_day": round(sum(usdt) / days, 2),
        "mfe_r": round(st.mean(mfe[i] for i in ids), 3),
        "mae_r": round(st.mean(mae[i] for i in ids), 3),
        "max_dd_usdt": round(dd, 1),
        "live_conv": round(len(live) / len(rs), 3),
        "live_per_day": round(len(live) / days, 2),
    }
    if with_ci and len(rs) >= 10:
        res["ci"] = [round(x, 3) for x in cluster_bootstrap_ci(rs, cl)]
    return res


def build_cells(tab: Table) -> dict[tuple, array]:
    """(type, side, dim, value) -> row indices. Type 'ANY' pools every event
    type, thinned to one per symbol/side/hour so a move is not counted once
    per event family."""
    cells: dict[tuple, array] = defaultdict(lambda: array("I"))
    pooled_last: dict[tuple, int] = {}
    base = TYPE_IDX["BASELINE"]
    for i in sorted(range(len(tab)), key=lambda j: tab.T[j]):
        side = SIDES[tab.side[i]]
        types = [TYPES[tab.typ[i]]]
        if tab.typ[i] != base:
            key = (tab.sym[i], tab.side[i])
            if tab.T[i] - pooled_last.get(key, -(10**12)) >= THIN_S:
                pooled_last[key] = tab.T[i]
                types.append("ANY")
        for typ in types:
            cells[(typ, side, "ALL", "ALL")].append(i)
            for di, dim in enumerate(DIM_NAMES):
                v = tab.reg[di][i]
                if v != NONE:
                    cells[(typ, side, dim, DIMS[dim][v])].append(i)
    return dict(cells)


def in_period(tab: Table, idx, pname: str) -> list[int]:
    pi = PERIOD_NAMES.index(pname)
    return [i for i in idx if tab.per[i] == pi]


def select(tab: Table, cells: dict, days: dict, geometry: str) -> tuple[list[tuple], dict]:
    """Steps 1-2 of the protocol: TRAIN (BH over every cell) then VALID.
    Reads no TEST or HOLDOUT row."""
    keys, stats_tr = [], []
    for key, idx in cells.items():
        if key[0] == "BASELINE":
            continue
        s = cell_stats(tab, in_period(tab, idx, "TRAIN"), days["TRAIN"], geometry)
        if s["n"]:
            keys.append(key)
            stats_tr.append(s)
    q = bh([s["p_pos"] if s["n"] >= 100 and s["mean_r"] > 0 else 1.0 for s in stats_tr])
    train_pass = [
        k
        for k, s, qq in zip(keys, stats_tr, q, strict=True)
        if s["n"] >= 100 and s["mean_r"] > 0 and qq < 0.10
    ]
    survivors = []
    for key in train_pass:
        s = cell_stats(tab, in_period(tab, cells[key], "VALID"), days["VALID"], geometry)
        if s["n"] >= 30 and s["mean_r"] > 0 and s["p_pos"] < 0.10:
            survivors.append(key)
    return survivors, {"train_cells": len(keys), "train_pass": train_pass}


def combined(cells: dict, survivors: list[tuple]) -> list[int]:
    return sorted({i for k in survivors for i in cells[k]})


# ------------------------------------------------------------------ main


def passes(s: dict, need_p: bool) -> bool:
    """Pre-registered acceptance of the LIVE-portfolio stream in one period:
    positive expectancy after all costs in absolute terms, positive net USDT
    and >= 3 executed trades/day (TEST also needs p < 0.05)."""
    return bool(
        s.get("n")
        and s["mean_r"] > 0
        and s["net_usdt_day"] > 0
        and s["per_day"] >= 3
        and (not need_p or s["p_pos"] < 0.05)
    )


CSV_FIELDS = [
    "geometry",
    "type",
    "side",
    "dim",
    "value",
    "period",
    "n",
    "per_day",
    "win_rate",
    "mean_r",
    "p_pos",
    "net_usdt",
    "net_usdt_day",
    "mfe_r",
    "mae_r",
    "max_dd_usdt",
    "live_conv",
    "live_per_day",
]


def run() -> dict:
    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    symbols = [s for (s,) in conn.execute("SELECT DISTINCT symbol FROM klines5m ORDER BY symbol")]
    btc = load_derivs(conn, [REF]).oi.get(REF, ([], []))
    conn.close()
    tab = Table()
    by_T: dict[int, list] = {}
    with ProcessPoolExecutor(WORKERS) as pool:
        for i, (r, x) in enumerate(pool.map(scan_symbol, symbols, chunksize=1), 1):
            tab.add(r)
            add_cross_section(by_T, x)
            print(f"{i}/{len(symbols)} symbols, {len(tab)} rows", flush=True)
    tab.attach_market(market_regimes(by_T, btc))
    del by_T
    days = period_days()
    cells = build_cells(tab)
    print(f"{len(cells)} cells", flush=True)
    result: dict = {"symbols": len(symbols), "rows": len(tab), "days": days, "geometries": {}}

    # Phase 1 - choose on TRAIN, confirm on VALID. Frozen to disk (with its
    # hash) BEFORE a single TEST or HOLDOUT statistic is computed.
    selection: dict = {}
    for g in GEOMETRIES:
        survivors, sel = select(tab, cells, days, g)
        selection[g] = {
            "train_cells": sel["train_cells"],
            "train_pass": ["|".join(k) for k in sel["train_pass"]],
            "survivors": ["|".join(k) for k in survivors],
        }
    frozen = json.dumps(selection, sort_keys=True)
    digest = hashlib.sha256(frozen.encode()).hexdigest()
    (OUT / "regime_lab_selection.json").write_text(
        json.dumps(
            {"sha256": digest, "frozen_at": datetime.now(UTC).isoformat(), "selection": selection},
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"selection frozen sha256={digest}: " + frozen[:3000], flush=True)

    # Phase 2 (TEST) and phase 3 (HOLDOUT) of the frozen selection, run
    # through the LIVE portfolio rules. Only the PRIMARY geometry can be
    # accepted; G_LEGACY is a sensitivity check.
    for g in GEOMETRIES:
        summary: dict = {"selection": selection[g], "combined": {}, "accepted": False}
        keys = [tuple(k.split("|")) for k in selection[g]["survivors"]]
        if keys:
            idx = combined(cells, keys)
            for pname in PERIOD_NAMES:
                ids = in_period(tab, idx, pname)
                taken = portfolio(tab, ids, g)
                summary["combined"][pname] = {
                    "signals": cell_stats(tab, ids, days[pname], g),
                    "live_portfolio": cell_stats(tab, taken, days[pname], g, with_ci=True),
                }

            summary["test_pass"] = passes(
                summary["combined"]["TEST"]["live_portfolio"], need_p=True
            )
            summary["holdout_pass"] = passes(
                summary["combined"]["HOLDOUT"]["live_portfolio"], need_p=False
            )
            summary["accepted"] = g == PRIMARY and summary["test_pass"] and summary["holdout_pass"]
        result["geometries"][g] = summary
        print(g, "accepted:", summary["accepted"], flush=True)

    # Phase 4 - descriptive table of every cell in every period (reporting
    # only; computed after the selection was frozen and never fed back).
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for g in GEOMETRIES:
            summary_cells: dict = {}
            for key, idx in cells.items():
                for pname in PERIOD_NAMES:
                    s = cell_stats(tab, in_period(tab, idx, pname), days[pname], g)
                    if s["n"]:
                        w.writerow(
                            {
                                "geometry": g,
                                "type": key[0],
                                "side": key[1],
                                "dim": key[2],
                                "value": key[3],
                                "period": pname,
                                **s,
                            }
                        )
                        summary_cells.setdefault("|".join(key), {})[pname] = s
            result["geometries"][g]["cells"] = summary_cells
    (OUT / "regime_lab.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


if __name__ == "__main__":
    run()
    sys.exit(0)
