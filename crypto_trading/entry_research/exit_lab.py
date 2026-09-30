"""Exit / holding-model lab - same entries, only the exit varies (2026-09-30).

    python -m crypto_trading.entry_research.exit_lab

Read-only research on the Binance archive (archive_fill.py). Nothing in the
bot imports this module; it can never open, size, move or close a LIVE trade.

Question (user, 2026-09-30): can a different EXIT (trailing SL, break-even,
ATR trail, MFE lock, holding time, signal/momentum exit) turn the existing
entries into positive expectancy after costs on unseen data, at >= ~3
trades/day? No new entry signal: the entries are exactly the regime lab's
(17 event families x LONG/SHORT, thinned one per symbol/type/side/hour, plus
the random BASELINE every 8 h), same entry time (first 5m open >= T + 5 min),
same entry price and the same initial stop (4 x ATR15 = 1 R).

Exit mechanics (fixed before any result was seen)
- Everything is simulated in R units on the 5m path of the entry, all 22
  variants in one pass, so they are compared on identical entries.
- Within a bar the stop is checked before the target (conservative). A
  trailing / break-even stop is recomputed at a bar's CLOSE from the path up
  to and including that bar and applies from the NEXT bar on (no intrabar
  look-ahead). Signal / momentum exits decide at a bar close and fill at the
  next bar's open.
- Every stop exit (initial, BE or trailing) pays 0.15 % slippage on the stop
  price; every trade pays 0.10 % fees; funding of every settlement inside
  the actual holding window is charged / credited. Result in R after costs.
- Only entries whose full 24 h path is covered by data are used (so every
  variant can be evaluated); max holding 24 h for every variant.

Variants (small fixed grid per family, trailing SL prioritised over TP)
  FIXED               SL 1 R, TP 1.5 R, 24 h (= regime-lab G_WIDE)
  TRAIL_A{a}_D{d}     FIXED + trailing SL at peak - d R once MFE >= a R
  BE{b}_TRAIL         stop to break-even (+fees) at b R, then trail 0.75 R from b + 0.5 R; no TP
  ATR_TRAIL{m}        no TP, stop = peak - m x live ATR15 (recomputed per bar)
  MFE_LOCK{g}         no TP, once MFE >= 1 R stop = peak x (1 - g)
  NOTP_TRAIL{d}       no TP, trailing SL at peak - d R from entry
  TIME{n}             SL 1 R, TP 1.5 R, forced exit after 15m/30m/1h/2h/4h
  SIG_OFF             SL 1 R, no TP, exit when the entry's own event condition is
                      no longer true (from the 3rd bar); BASELINE has none -> n/a
  MOM_OFF             SL 1 R, no TP, exit when the 1 h return turns against the trade
                      (from the 3rd bar)

Protocol - identical to regime_lab (TRAIN -> VALID -> frozen -> TEST -> HOLDOUT)
1. TRAIN: (variant, cell) n >= 100, mean R > 0, BH q < 0.10 over every
   (variant, cell) tested. Cells: every signal type x side, and all types
   pooled ("ANY", one per symbol/side/hour) x side x every regime value.
2. VALID: mean R > 0, n >= 30, p < 0.10.
3. Frozen (sha256) before any TEST/HOLDOUT statistic. An entry matched by
   several survivors uses the survivor with the best VALID mean (decided
   before TEST).
4. TEST (p < 0.05) and HOLDOUT through the LIVE portfolio rules (1000
   notional, max 4, 1 per symbol, no % caps): mean R > 0, net USDT/day > 0,
   >= 3 trades/day, both periods. "Loses less" does not pass.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import pickle
import sqlite3
import statistics as st
import sys
from array import array
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research import event_engine as ee
from crypto_trading.entry_research import regime_lab as rl
from crypto_trading.entry_research.stats import bh, cluster_bootstrap_ci, p_mean_positive

OUT = Path("data/entry_research")
REPORT_CSV = Path("docs/superpowers/reports/2026-09-30-exit-lab-cells.csv")
BAR_S = rl.BAR_S
HOUR = rl.HOUR
STOP_ATR = 4.0  # initial stop = 1 R = 4 x ATR15 (regime-lab G_WIDE)
TP_R = 1.5
HORIZON_BARS = 288  # 24 h
MIN_BARS_SIGNAL = 2  # signal / momentum exits only from the 3rd bar
WORKERS = 2  # ~180 MB per worker; the bot and a browser share the 8 GB

# (name, kind, params). Fixed before any result was seen.
VARIANTS: tuple[tuple[str, str, tuple], ...] = (
    ("FIXED", "fixed", ()),
    ("TRAIL_A0.5_D0.5", "trail_after", (0.5, 0.5)),
    ("TRAIL_A0.5_D1", "trail_after", (0.5, 1.0)),
    ("TRAIL_A1_D0.5", "trail_after", (1.0, 0.5)),
    ("TRAIL_A1_D1", "trail_after", (1.0, 1.0)),
    ("BE0.5_TRAIL", "be_trail", (0.5,)),
    ("BE1_TRAIL", "be_trail", (1.0,)),
    ("ATR_TRAIL2", "atr_trail", (2.0,)),
    ("ATR_TRAIL3", "atr_trail", (3.0,)),
    ("ATR_TRAIL4", "atr_trail", (4.0,)),
    ("MFE_LOCK0.3", "mfe_lock", (0.3,)),
    ("MFE_LOCK0.5", "mfe_lock", (0.5,)),
    ("NOTP_TRAIL0.5", "notp_trail", (0.5,)),
    ("NOTP_TRAIL1", "notp_trail", (1.0,)),
    ("NOTP_TRAIL1.5", "notp_trail", (1.5,)),
    ("TIME15m", "time", (3,)),
    ("TIME30m", "time", (6,)),
    ("TIME1h", "time", (12,)),
    ("TIME2h", "time", (24,)),
    ("TIME4h", "time", (48,)),
    ("SIG_OFF", "sig_off", ()),
    ("MOM_OFF", "mom_off", ()),
)
VNAMES = tuple(v[0] for v in VARIANTS)
NV = len(VARIANTS)
TRAIL_BE_D = 0.75
KIND_NEEDS_ATR = any(v[1] == "atr_trail" for v in VARIANTS)


# ------------------------------------------------------------------ path simulation


def simulate(
    path: list[tuple],
    entry: float,
    risk: float,
    sgn: int,
    atr_entry_pct: float,
    fee_r: float,
    fund_cum: list[float],
    sig: list[bool | None] | None,
    mom: list[bool | None],
    atr_live: list[float | None],
) -> list[tuple[float, int, float] | None]:
    """All variants over one entry's path. path[m] = (hi_R, lo_R, close_R,
    next_open_R) in favourable-direction R units; fund_cum[m] = funding paid
    in R up to the end of bar m. Returns per variant (r_net, bars_held,
    mae_R) or None (variant not applicable). Each variant runs its own loop
    and stops at its exit (speed); semantics are identical for all."""
    hi_l = [x[0] for x in path]
    lo_l = [x[1] for x in path]
    cl_l = [x[2] for x in path]
    nx_l = [x[3] for x in path]
    ctx = (
        hi_l,
        lo_l,
        cl_l,
        nx_l,
        entry,
        risk,
        sgn,
        atr_entry_pct,
        fee_r,
        fund_cum,
        sig,
        mom,
        atr_live,
    )
    return [_one(kind, prm, ctx) for _, kind, prm in VARIANTS]


def _one(kind: str, prm: tuple, ctx: tuple) -> tuple[float, int, float] | None:
    (
        hi_l,
        lo_l,
        cl_l,
        nx_l,
        entry,
        risk,
        sgn,
        atr_entry_pct,
        fee_r,
        fund_cum,
        sig,
        mom,
        atr_live,
    ) = ctx
    if kind == "sig_off" and sig is None:
        return None
    flags = sig if kind == "sig_off" else mom if kind == "mom_off" else None
    has_tp = kind in ("fixed", "trail_after", "time")
    max_bars = prm[0] if kind == "time" else None
    n = len(hi_l)
    stop = -1.0
    peak = 0.0
    low = 0.0
    for m in range(n):
        hi = hi_l[m]
        lo = lo_l[m]
        if lo < low:
            low = lo
        if lo <= stop:  # stop first
            slip = rl.STOP_SLIP * (entry + sgn * stop * risk) / risk
            return (stop - slip - fee_r - fund_cum[m], m + 1, min(low, stop))
        if has_tp and hi >= TP_R:
            return (TP_R - fee_r - fund_cum[m], m + 1, low)
        if max_bars is not None and m + 1 >= max_bars:
            return (cl_l[m] - fee_r - fund_cum[m], m + 1, low)
        if flags is not None and m >= MIN_BARS_SIGNAL and m + 1 < n and flags[m] is False:
            return (nx_l[m] - fee_r - fund_cum[m], m + 1, low)
        # recompute the stop at this bar's close; it applies from the next bar
        if hi > peak:
            peak = hi
        if kind == "trail_after":
            if peak >= prm[0] and peak - prm[1] > stop:
                stop = peak - prm[1]
        elif kind == "be_trail":
            if peak >= prm[0] and fee_r > stop:
                stop = fee_r
            if peak >= prm[0] + 0.5 and peak - TRAIL_BE_D > stop:
                stop = peak - TRAIL_BE_D
        elif kind == "atr_trail":
            a = atr_live[m]
            if a:
                cand = peak - prm[0] * a / (STOP_ATR * atr_entry_pct)
                if cand > stop:
                    stop = cand
        elif kind == "mfe_lock":
            if peak >= 1.0 and peak * (1 - prm[0]) > stop:
                stop = peak * (1 - prm[0])
        elif kind == "notp_trail":
            if peak - prm[0] > stop:
                stop = peak - prm[0]
    return (cl_l[n - 1] - fee_r - fund_cum[n - 1], n, low)


# ------------------------------------------------------------------ per-symbol scan


UNKNOWN = 2  # flag byte: 0 false, 1 true, 2 not known at that bar


def _set_flags(flags: dict[tuple[str, str], bytearray], k: int, f: dict) -> None:
    for name, (lc, sc) in ee.CONDITIONS.items():
        flags[(name, "LONG")][k] = ee._holds(lc, f)
        flags[(name, "SHORT")][k] = ee._holds(sc, f)


def _flag(b: bytearray, j: int, invert: bool = False) -> bool | None:
    v = b[j]
    if v == UNKNOWN:
        return None
    return (not v) if invert else bool(v)


def scan_symbol(sym: str) -> tuple[dict, list[tuple]]:
    """Columnar chunk for one symbol + hourly cross-section points."""
    conn = sqlite3.connect(f"file:{rl.ARCHIVE_DB}?mode=ro", uri=True)
    s = rl.load_series(conn, sym)
    if s is None:
        return {}, []
    d = rl.load_derivs(conn, [sym])
    conn.close()
    fts, frs = d.funding.get(sym, ([], []))
    p = ee.Prepared(s)
    n_bars = len(s.c)
    # pass 1: features at every bar close -> condition flags, momentum, events
    flags = {
        (name, side): bytearray([UNKNOWN]) * n_bars
        for name in ee.CONDITIONS
        for side in ("LONG", "SHORT")
    }
    mom_up = bytearray([UNKNOWN]) * n_bars
    events: list[tuple] = []
    xs: list[tuple] = []
    atr_hist: deque = deque(maxlen=30 * 24)
    last_emit: dict = {}
    prev = None
    for k in range(ee.HIST, n_bars):
        f = ee.features_at(p, k)
        T = s.close_time(k)
        if f is None:
            prev = None
            continue
        f = ee.with_derivs(f, sym, T, d)
        _set_flags(flags, k, f)
        mom_up[k] = f["ret12"] > 0
        hits = [e for e in ee.events_at(f, prev) if T - last_emit.get(e, -(10**12)) >= rl.THIN_S]
        if T % rl.BASELINE_EVERY_S == 0:
            hits += [("BASELINE", "LONG"), ("BASELINE", "SHORT")]
        if hits:
            reg = {
                "sym_funding": rl.fund_bucket(f.get("fund")),
                "sym_oi": rl.oi_bucket(d.oi_change(sym, T, rl.DAY), 0.10),
                "sym_vol": rl._median_before(atr_hist, f["atr_l"]),
            }
            for e in hits:
                last_emit[e] = T
                events.append((k, T, e[0], e[1], reg))
        if T % HOUR == 0:
            atr_hist.append(f["atr_l"])
            xs.append((T, f["ret48"], f.get("fund")))
        prev = f

    atr_cache: dict[int, float | None] = {}

    def atr_at(T: int) -> float | None:
        if T not in atr_cache:
            atr_cache[T] = rl.atr15_pct(s, T)
        return atr_cache[T]

    chunk = new_chunk()
    for k, T, typ, side, reg in events:
        a = atr_at(T)
        if not a:
            continue
        i = k + 2  # first bar starting >= T + 5 min
        if i + HORIZON_BARS >= n_bars or s.o[i] is None:
            continue
        if any(s.c[j] is None for j in range(i, i + HORIZON_BARS + 1)):
            continue  # every variant needs the full 24 h path
        entry = s.o[i]
        sgn = 1 if side == "LONG" else -1
        risk = STOP_ATR * a / 100 * entry
        fee_r = rl.FEE_RT * entry / risk
        path, fund_cum, sig, mom, atr_live = [], [], [], [], []
        t_entry = s.start + i * BAR_S
        fi = bisect.bisect_right(fts, t_entry)
        paid = 0.0
        mfe24 = 0.0
        for m in range(HORIZON_BARS):
            j = i + m
            if sgn > 0:
                hi, lo = (s.h[j] - entry) / risk, (s.l[j] - entry) / risk
            else:
                hi, lo = (entry - s.l[j]) / risk, (entry - s.h[j]) / risk
            cl = sgn * (s.c[j] - entry) / risk
            nxt = sgn * (s.o[j + 1] - entry) / risk if s.o[j + 1] is not None else cl
            path.append((hi, lo, cl, nxt))
            end = s.start + (j + 1) * BAR_S
            while fi < len(fts) and fts[fi] <= end:
                paid += sgn * frs[fi] * entry / risk
                fi += 1
            fund_cum.append(paid)
            mfe24 = max(mfe24, hi)
            if typ != "BASELINE":
                sig.append(_flag(flags[(typ, side)], j))
            mom.append(_flag(mom_up, j, invert=sgn < 0))
            atr_live.append(atr_at(s.close_time(j)) if KIND_NEEDS_ATR else None)
        outs = simulate(
            path,
            entry,
            risk,
            sgn,
            a,
            fee_r,
            fund_cum,
            None if typ == "BASELINE" else sig,
            mom,
            atr_live,
        )
        add_row(chunk, sym, T, typ, side, a, t_entry, mfe24, outs, reg)
    return chunk, xs


# ------------------------------------------------------------------ checkpoints

CHECKPOINT_DIR = OUT / "exit_lab_chunks"
_CODE_FILES = ("regime_lab.py", "event_engine.py", "stats.py")
_SCAN_FUNCS = ("simulate", "_one", "_set_flags", "_flag", "scan_symbol", "new_chunk", "add_row")


def code_hash() -> str:
    """Checkpoints are only reused if every piece of code that PRODUCES them
    is byte-identical: the imported research modules in full, plus this
    module's scan/simulation functions and exit constants. Evaluation-only
    code (run, stats, selection) is deliberately not part of it."""
    import inspect

    h = hashlib.sha256()
    base = Path(__file__).parent
    for name in _CODE_FILES:
        h.update((base / name).read_bytes())
    mod = sys.modules[__name__]
    for fn in _SCAN_FUNCS:
        h.update(inspect.getsource(getattr(mod, fn)).encode())
    consts = (VARIANTS, STOP_ATR, TP_R, HORIZON_BARS, MIN_BARS_SIGNAL, TRAIL_BE_D, UNKNOWN)
    h.update(repr(consts).encode())
    return h.hexdigest()


def _cp_path(sym: str) -> Path:
    return CHECKPOINT_DIR / f"{sym}.pkl"


def load_checkpoint(sym: str, code: str):
    p = _cp_path(sym)
    if not p.exists():
        return None
    with p.open("rb") as fh:
        payload = pickle.load(fh)  # noqa: S301 - our own local research files
    if payload.get("code") != code or payload.get("symbol") != sym:
        return None
    return payload["chunk"], payload["xs"]


def scan_and_checkpoint(sym: str, code: str) -> str:
    """Worker: scan one symbol and write its result atomically to disk, so
    the main process holds nothing during the scan and an interrupted run
    resumes where it stopped."""
    chunk, xs = scan_symbol(sym)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _cp_path(sym).with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump({"symbol": sym, "code": code, "chunk": chunk, "xs": xs}, fh)
    tmp.replace(_cp_path(sym))  # atomic: a file is either complete or absent
    return sym


# ------------------------------------------------------------------ columnar storage


def new_chunk() -> dict:
    return {
        "sym": [],
        "T": array("q"),
        "typ": array("B"),
        "side": array("B"),
        "stop": array("d"),
        "entry": array("q"),
        "mfe24": array("f"),
        "reg": [],
        "r": [array("f") for _ in VARIANTS],
        "hold": [array("H") for _ in VARIANTS],
        "mae": [array("f") for _ in VARIANTS],
    }


def add_row(ch: dict, sym, T, typ, side, atr, t_entry, mfe24, outs, reg) -> None:
    ch["sym"].append(sym)
    ch["T"].append(T)
    ch["typ"].append(rl.TYPE_IDX[typ])
    ch["side"].append(rl.SIDES.index(side))
    ch["stop"].append(atr)
    ch["entry"].append(t_entry)
    ch["mfe24"].append(mfe24)
    ch["reg"].append(tuple(reg.get(dn) for dn in rl.DIM_NAMES))
    for vi, o in enumerate(outs):
        ch["r"][vi].append(o[0] if o else rl.NAN)
        ch["hold"][vi].append(o[1] if o else 0)
        ch["mae"][vi].append(o[2] if o else rl.NAN)


class QArr:
    """R values as int16 thousandths (0.001 R resolution, +-32 R, NaN kept):
    6 bytes per variant per entry instead of 12 - the full universe fits
    beside the running bot on an 8 GB machine."""

    NAN_Q = -32768

    def __init__(self) -> None:
        self.a = array("h")

    def extend(self, xs) -> None:
        q = self.NAN_Q
        self.a.extend(q if x != x else max(-32767, min(32767, round(x * 1000))) for x in xs)

    def __getitem__(self, i: int) -> float:
        v = self.a[i]
        return rl.NAN if v == self.NAN_Q else v / 1000

    def __len__(self) -> int:
        return len(self.a)


class ExitTable:
    def __init__(self) -> None:
        self.syms: list[str] = []
        self.sym_idx: dict[str, int] = {}
        self.sym = array("H")
        self.T = array("q")
        self.typ = array("B")
        self.side = array("B")
        self.stop = array("d")
        self.entry = array("q")
        self.mfe24 = array("f")
        self.r = [QArr() for _ in VARIANTS]
        self.hold = [array("H") for _ in VARIANTS]
        self.mae = [QArr() for _ in VARIANTS]
        self.reg = [array("b") for _ in rl.DIMS]
        self.per = array("b")

    def __len__(self) -> int:
        return len(self.T)

    def extend(self, ch: dict) -> None:
        if not ch:
            return
        for sym in ch["sym"]:
            if sym not in self.sym_idx:
                self.sym_idx[sym] = len(self.syms)
                self.syms.append(sym)
            self.sym.append(self.sym_idx[sym])
        for key in ("T", "typ", "side", "stop", "entry", "mfe24"):
            getattr(self, key).extend(ch[key])
        for vi in range(NV):
            self.r[vi].extend(ch["r"][vi])
            self.hold[vi].extend(ch["hold"][vi])
            self.mae[vi].extend(ch["mae"][vi])
        for reg in ch["reg"]:
            for di, val in enumerate(reg):
                self.reg[di].append(rl.DIMS[rl.DIM_NAMES[di]].index(val) if val else rl.NONE)
        for T in ch["T"]:
            pn = rl.period(T)
            self.per.append(rl.PERIOD_NAMES.index(pn) if pn else rl.NONE)

    def attach_market(self, mkt: dict[int, dict]) -> None:
        rl.Table.attach_market(self, mkt)  # same columns (T, reg)


# ------------------------------------------------------------------ evaluation


def light_stats(tab: ExitTable, idx, vi: int) -> dict:
    r = tab.r[vi]
    rs = [r[i] for i in idx if r[i] == r[i]]
    if not rs:
        return {"n": 0}
    cl = [tab.T[i] // (4 * HOUR) for i in idx if r[i] == r[i]]
    return {"n": len(rs), "mean_r": st.mean(rs), "p_pos": p_mean_positive(rs, cl)}


def portfolio(tab: ExitTable, pairs) -> list[tuple[int, int]]:
    """LIVE rules on (row, variant) pairs: 1000 notional, max 4 open, 1 per
    symbol, no % caps; the exit time is the variant's own."""
    open_: list[tuple[int, int]] = []
    taken = []
    for i, vi in sorted(pairs, key=lambda x: tab.T[x[0]]):
        if tab.r[vi][i] != tab.r[vi][i]:
            continue
        e = tab.entry[i]
        open_ = [x for x in open_ if x[1] > e]
        if len(open_) >= 4 or any(x[0] == tab.sym[i] for x in open_):
            continue
        open_.append((tab.sym[i], e + tab.hold[vi][i] * BAR_S))
        taken.append((i, vi))
    return taken


def full_stats(tab: ExitTable, pairs, days: float, with_ci: bool = False) -> dict:
    pairs = [(i, vi) for i, vi in pairs if tab.r[vi][i] == tab.r[vi][i]]
    if not pairs:
        return {"n": 0}
    rs = [tab.r[vi][i] for i, vi in pairs]
    cl = [tab.T[i] // (4 * HOUR) for i, _ in pairs]
    usdt = [tab.r[vi][i] * STOP_ATR * tab.stop[i] / 100 * rl.NOTIONAL for i, vi in pairs]
    ends = [tab.entry[i] + tab.hold[vi][i] * BAR_S for i, vi in pairs]
    eq = peak = dd = 0.0
    for _, u in sorted(zip(ends, usdt, strict=True)):
        eq += u
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gains = sum(u for u in usdt if u > 0)
    losses = -sum(u for u in usdt if u < 0)
    mfe = st.mean(tab.mfe24[i] for i, _ in pairs)
    live = portfolio(tab, pairs)
    res = {
        "n": len(rs),
        "per_day": round(len(rs) / days, 2),
        "win_rate": round(sum(x > 0 for x in rs) / len(rs), 3),
        "mean_r": round(st.mean(rs), 4),
        "p_pos": round(p_mean_positive(rs, cl), 5) if len(rs) >= 5 else 1.0,
        "net_usdt": round(sum(usdt), 1),
        "net_usdt_day": round(sum(usdt) / days, 2),
        "profit_factor": round(gains / losses, 3) if losses else None,
        "max_dd_usdt": round(dd, 1),
        "mfe24_r": round(mfe, 3),
        "mfe_capture": round(st.mean(rs) / mfe, 3) if mfe > 0 else None,
        "mae_r": round(st.mean(tab.mae[vi][i] for i, vi in pairs), 3),
        "hold_min": round(st.mean(tab.hold[vi][i] for i, vi in pairs) * 5, 1),
        "live_conv": round(len(live) / len(rs), 3),
        "live_per_day": round(len(live) / days, 2),
    }
    if with_ci and len(rs) >= 10:
        res["ci"] = [round(x, 3) for x in cluster_bootstrap_ci(rs, cl)]
    return res


def build_cells(tab: ExitTable) -> dict[tuple, array]:
    """(type, side) for every type (incl. BASELINE, reference only) and
    ('ANY', side, dim, value) for the pooled entries (one per
    symbol/side/hour) - the same pooling as regime_lab."""
    cells: dict[tuple, array] = defaultdict(lambda: array("I"))
    pooled_last: dict[tuple, int] = {}
    base = rl.TYPE_IDX["BASELINE"]
    for i in sorted(range(len(tab)), key=lambda j: tab.T[j]):
        side = rl.SIDES[tab.side[i]]
        cells[(rl.TYPES[tab.typ[i]], side, "ALL", "ALL")].append(i)
        if tab.typ[i] == base:
            continue
        key = (tab.sym[i], tab.side[i])
        if tab.T[i] - pooled_last.get(key, -(10**12)) < rl.THIN_S:
            continue
        pooled_last[key] = tab.T[i]
        cells[("ANY", side, "ALL", "ALL")].append(i)
        for di, dim in enumerate(rl.DIM_NAMES):
            v = tab.reg[di][i]
            if v != rl.NONE:
                cells[("ANY", side, dim, rl.DIMS[dim][v])].append(i)
    return dict(cells)


def in_period(tab: ExitTable, idx, pname: str) -> list[int]:
    pi = rl.PERIOD_NAMES.index(pname)
    return [i for i in idx if tab.per[i] == pi]


def select(tab: ExitTable, cells: dict) -> tuple[list[dict], dict]:
    """Steps 1-2: TRAIN (BH over every variant x cell) then VALID. Reads no
    TEST or HOLDOUT row."""
    keys, stats = [], []
    for ck, idx in cells.items():
        if ck[0] == "BASELINE":
            continue
        tr = in_period(tab, idx, "TRAIN")
        for vi in range(NV):
            s = light_stats(tab, tr, vi)
            if s["n"]:
                keys.append((vi, ck))
                stats.append(s)
    q = bh([s["p_pos"] if s["n"] >= 100 and s["mean_r"] > 0 else 1.0 for s in stats])
    train_pass = [
        k
        for k, s, qq in zip(keys, stats, q, strict=True)
        if s["n"] >= 100 and s["mean_r"] > 0 and qq < 0.10
    ]
    survivors = []
    for vi, ck in train_pass:
        s = light_stats(tab, in_period(tab, cells[ck], "VALID"), vi)
        if s["n"] >= 30 and s["mean_r"] > 0 and s["p_pos"] < 0.10:
            survivors.append(
                {"variant": VNAMES[vi], "cell": "|".join(ck), "valid_mean_r": round(s["mean_r"], 5)}
            )
    return survivors, {"tested": len(keys), "train_pass": len(train_pass)}


def assign(cells: dict, survivors: list[dict]) -> dict[int, int]:
    """Row -> variant: the survivor with the best VALID mean (fixed pre-TEST)."""
    best: dict[int, tuple[float, int]] = {}
    for sv in survivors:
        vi = VNAMES.index(sv["variant"])
        for i in cells[tuple(sv["cell"].split("|"))]:
            if i not in best or sv["valid_mean_r"] > best[i][0]:
                best[i] = (sv["valid_mean_r"], vi)
    return {i: vi for i, (_, vi) in best.items()}


CSV_FIELDS = [
    "variant",
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
    "profit_factor",
    "max_dd_usdt",
    "mfe24_r",
    "mfe_capture",
    "mae_r",
    "hold_min",
    "live_conv",
    "live_per_day",
]


def run() -> dict:
    conn = sqlite3.connect(f"file:{rl.ARCHIVE_DB}?mode=ro", uri=True)
    symbols = [s for (s,) in conn.execute("SELECT DISTINCT symbol FROM klines5m ORDER BY symbol")]
    btc = rl.load_derivs(conn, [rl.REF]).oi.get(rl.REF, ([], []))
    conn.close()
    code = code_hash()
    todo = [sym for sym in symbols if load_checkpoint(sym, code) is None]
    print(
        f"{len(symbols) - len(todo)}/{len(symbols)} symbols already checkpointed "
        f"(code {code[:12]}), {len(todo)} to scan",
        flush=True,
    )
    done = len(symbols) - len(todo)
    with ProcessPoolExecutor(WORKERS) as pool:
        for _ in pool.map(scan_and_checkpoint, todo, [code] * len(todo), chunksize=1):
            done += 1
            print(f"{done}/{len(symbols)} symbols", flush=True)
    # load every checkpoint once, in fixed symbol order (no mixing, no duplicates)
    tab = ExitTable()
    by_T: dict[int, list] = {}
    for sym in symbols:
        cp = load_checkpoint(sym, code)
        if cp is None:
            raise RuntimeError(f"missing checkpoint for {sym}")
        ch, xs = cp
        if ch and ch["sym"] and set(ch["sym"]) != {sym}:  # empty = no entries (e.g. dead contract)
            raise RuntimeError(f"checkpoint {sym} contains other symbols")
        tab.extend(ch)
        rl.add_cross_section(by_T, xs)
    print(f"all {len(symbols)} symbols loaded, {len(tab)} entries", flush=True)
    tab.attach_market(rl.market_regimes(by_T, btc))
    del by_T
    days = rl.period_days()
    cells = build_cells(tab)
    print(f"{len(cells)} cells x {NV} variants", flush=True)

    # Phase 1 - selection on TRAIN + VALID, frozen before TEST / HOLDOUT
    survivors, sel = select(tab, cells)
    frozen = json.dumps({"survivors": survivors, **sel}, sort_keys=True)
    digest = hashlib.sha256(frozen.encode()).hexdigest()
    (OUT / "exit_lab_selection.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "frozen_at": datetime.now(UTC).isoformat(),
                "selection": json.loads(frozen),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"selection frozen sha256={digest}: {frozen[:3000]}", flush=True)

    # Phase 2/3 - TEST and HOLDOUT of the frozen selection (LIVE portfolio)
    result: dict = {
        "entries": len(tab),
        "symbols": len(symbols),
        "days": days,
        "selection": json.loads(frozen),
        "sha256": digest,
        "accepted": False,
    }
    if survivors:
        rows = assign(cells, survivors)
        result["combined"] = {}
        for pname in rl.PERIOD_NAMES:
            pairs = [
                (i, vi) for i, vi in rows.items() if tab.per[i] == rl.PERIOD_NAMES.index(pname)
            ]
            taken = portfolio(tab, pairs)
            result["combined"][pname] = {
                "signals": full_stats(tab, pairs, days[pname]),
                "live_portfolio": full_stats(tab, taken, days[pname], with_ci=True),
            }
        c = result["combined"]
        result["test_pass"] = rl.passes(c["TEST"]["live_portfolio"], need_p=True)
        result["holdout_pass"] = rl.passes(c["HOLDOUT"]["live_portfolio"], need_p=False)
        result["accepted"] = result["test_pass"] and result["holdout_pass"]
    print("accepted:", result["accepted"], flush=True)

    # Phase 4 - descriptive table (after the freeze; never fed back)
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for ck, idx in cells.items():
            per = {pn: in_period(tab, idx, pn) for pn in rl.PERIOD_NAMES}
            for vi in range(NV):
                for pn in rl.PERIOD_NAMES:
                    s = full_stats(tab, [(i, vi) for i in per[pn]], days[pn])
                    if s["n"]:
                        w.writerow(
                            {
                                "variant": VNAMES[vi],
                                "type": ck[0],
                                "side": ck[1],
                                "dim": ck[2],
                                "value": ck[3],
                                "period": pn,
                                **s,
                            }
                        )
                        if ck[0] in ("ANY", "BASELINE") and ck[2] == "ALL":
                            summary.setdefault("|".join((VNAMES[vi], *ck)), {})[pn] = s
    result["summary"] = summary
    (OUT / "exit_lab.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


if __name__ == "__main__":
    run()
    sys.exit(0)
