"""P3 (2026-10-01): does NEW information add edge to the bot's own signal?

Research only. Nothing here is imported by a trading path.

BASELINE = the bot's quant trigger, rebuilt point-in-time on the Binance 5m
archive (135 symbols, 2025-08-01 -> 2026-09-30): on every CLOSED 30m bar,
  |close/prev close - 1| > 2 %  or  RSI14(30m, simple) > 70  or
  volume z vs the previous 20 bars > 2.5  or  |funding| > 0.05 %,
with the bot's 60 min per-symbol cooldown (screening/quant_screener.py,
config/pipeline.yaml). Entry at the open of the first 5m bar >= T + 5 min.

Stored per event (all from data closed at T - no look-ahead):
- controls = what today's signal already sees: 30m/1h/4h/24h return, RSI,
  volume z, funding, 1h OI change, ATR15, BTC 4h return (regime);
- ORDER FLOW (P3-B): taker imbalance over 5/15/30/60 min, its change and
  acceleration, persistence, abnormal net aggressive flow (z vs 24 h), price
  response per unit of flow, flow/price divergence;
- LIQUIDATION PROXY (P3-A): Binance has no liquidation history, so only a
  proxy is possible - abrupt OI drops (5/15 min, z vs 24 h) split by the price
  direction that accompanied them (long vs short liquidations). Built from OI
  and price, i.e. from data the baseline already has: it can show whether a
  forced-deleveraging STATE matters, not that a new source does;
- outcomes: gross LONG return at 5/15/30/60/120/240 min, and the bot's own
  bracket (SL 4.3 % / TP 5 % / 6 h, stop first, 0.10 % stop slippage,
  funding) for LONG and SHORT, with MFE/MAE.

    python -m crypto_trading.entry_research.p3_lab scan
"""

from __future__ import annotations

import bisect
import math
import sqlite3
import statistics as st
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ARCHIVE = "data/entry_research/archive.db"
EVENTS_DB = Path("data/entry_research/p3_events.db")
BAR = 300
LAT = 300
HORIZONS = (5, 15, 30, 60, 120, 240)
SL_PCT, TP_PCT, HOLD_S = 4.3, 5.0, 6 * 3600
FEE = 0.0005
TAKER_SLIP = 0.0003
STOP_SLIP = 0.0010
COOLDOWN_S = 3600
WORKERS = 4

COLUMNS = (
    "symbol",
    "T",
    "trig",
    "dir",
    # controls
    "ret30",
    "ret60",
    "ret240",
    "ret1440",
    "rsi30",
    "volz30",
    "funding",
    "oi60",
    "atr15",
    "btc240",
    # order flow
    "ti5",
    "ti15",
    "ti30",
    "ti60",
    "dti15",
    "acc_ti",
    "persist",
    "flowz",
    "impact",
    "diverge",
    # liquidation proxy
    "doi5",
    "doi15",
    "oiz15",
    "liq_long",
    "liq_short",
    "doi5_acc",
    # outcomes
    *(f"r{h}" for h in HORIZONS),
    "bl_ret",
    "bl_reason",
    "bl_mfe",
    "bl_mae",
    "bs_ret",
    "bs_reason",
    "bs_mfe",
    "bs_mae",
)


def _series(conn: sqlite3.Connection, sym: str):
    rows = conn.execute(
        "SELECT t, o, h, l, c, v, tbv FROM klines5m WHERE symbol = ? ORDER BY t", (sym,)
    ).fetchall()
    if len(rows) < 2000:
        return None
    first = rows[0][0] // 1000
    n = (rows[-1][0] // 1000 - first) // BAR + 1
    cols = [[None] * n for _ in range(6)]
    for t, *vals in rows:
        k = (t // 1000 - first) // BAR
        for j, x in enumerate(vals):
            cols[j][k] = x
    return first, cols


def _oi(conn: sqlite3.Connection, sym: str, first: int, n: int) -> list:
    out: list = [None] * n
    for t, v in conn.execute(
        "SELECT t, oi_value FROM metrics WHERE symbol = ? AND oi_value IS NOT NULL", (sym,)
    ):
        k = (t // 1000 - first) // BAR
        if 0 <= k < n:
            out[k] = v
    return out


def _funding(conn: sqlite3.Connection, sym: str) -> tuple[list[int], list[float]]:
    rows = conn.execute(
        "SELECT t, rate FROM funding WHERE symbol = ? ORDER BY t", (sym,)
    ).fetchall()
    return [t // 1000 for t, _ in rows], [r for _, r in rows]


def _rsi(closes: list[float]) -> float | None:
    if len(closes) < 15:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-15:-1], closes[-14:], strict=True):
        d = b - a
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def _z(x: float, hist: list[float]) -> float | None:
    if len(hist) < 10:
        return None
    m = sum(hist) / len(hist)
    var = sum((h - m) ** 2 for h in hist) / len(hist)
    return (x - m) / math.sqrt(var) if var > 0 else None


def bracket(o, h, lo, c, i: int, side: int, fts, frs, start: int) -> tuple | None:
    """Bot geometry on 5m bars from entry bar i; stop first inside a bar."""
    entry = o[i]
    if entry is None:
        return None
    stop = entry * (1 - side * SL_PCT / 100)
    target = entry * (1 + side * TP_PCT / 100)
    fav = adv = entry
    end_k = i + HOLD_S // BAR
    j = i
    px = reason = None
    while j < min(end_k, len(c)):
        if c[j] is None:
            return None
        up, dn = h[j], lo[j]
        fav = max(fav, up) if side > 0 else min(fav, dn)
        adv = min(adv, dn) if side > 0 else max(adv, up)
        if (side > 0 and dn <= stop) or (side < 0 and up >= stop):
            px, reason = stop * (1 - side * STOP_SLIP), "SL"
            break
        if (side > 0 and up >= target) or (side < 0 and dn <= target):
            px, reason = target, "TP"
            break
        j += 1
    if px is None:
        if end_k > len(c) or c[end_k - 1] is None:
            return None
        px, reason, j = c[end_k - 1] * (1 - side * TAKER_SLIP), "TIME", end_k - 1
    gross = side * (px / entry - 1)
    cost = FEE * 2 + TAKER_SLIP  # entry fee + slip, exit fee (exit slip inside px)
    t0, t1 = start + i * BAR, start + (j + 1) * BAR
    a, b = bisect.bisect_right(fts, t0), bisect.bisect_right(fts, t1)
    fund = sum(side * frs[x] for x in range(a, b))  # longs pay positive funding
    return (gross - cost - fund, reason, side * (fav / entry - 1), side * (adv / entry - 1))


def scan_symbol(sym: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True)
    s = _series(conn, sym)
    btc = _series(conn, "BTCUSDT") if sym != "BTCUSDT" else s
    if s is None or btc is None:
        return []
    start, (o, h, lo, c, v, tbv) = s
    n = len(c)
    oi = _oi(conn, sym, start, n)
    fts, frs = _funding(conn, sym)
    bstart, bcols = btc
    bc = bcols[3]
    c30: list[float] = []
    v30: list[float] = []
    nf30: list[float] = []  # net aggressive quote flow per 30m
    imp_hist: list[float] = []
    doi15_hist: list[float] = []
    last_event = -(10**12)
    out = []

    def ret(k: int, bars: int) -> float | None:
        if k - bars < 0 or c[k] is None or not c[k - bars]:
            return None
        return c[k] / c[k - bars] - 1

    def ti(k: int, bars: int) -> float | None:
        if k - bars + 1 < 0:
            return None
        vs = v[k - bars + 1 : k + 1]
        bs = tbv[k - bars + 1 : k + 1]
        if any(x is None for x in vs) or any(x is None for x in bs):
            return None
        tot = sum(vs)
        return (2 * sum(bs) - tot) / tot if tot > 0 else None

    def doi(k: int, bars: int) -> float | None:
        if k - bars < 0 or oi[k] is None or oi[k - bars] is None or oi[k - bars] <= 0:
            return None
        return oi[k] / oi[k - bars] - 1

    for k in range(6, n - 2):
        T = start + (k + 1) * BAR  # close of bar k
        if T % 900 == 0 and k >= 3:
            d15 = doi(k, 3)
            if d15 is not None:
                doi15_hist.append(d15)
                if len(doi15_hist) > 96:
                    doi15_hist.pop(0)
        if T % 1800:
            continue
        window = range(k - 5, k + 1)
        if any(c[x] is None or v[x] is None for x in window):
            c30.clear()
            v30.clear()
            nf30.clear()
            continue
        close30, vol30 = c[k], sum(v[x] for x in window)
        net = sum((2 * (tbv[x] or 0) - v[x]) * c[x] for x in window)
        prev_c30, prev_v30 = c30[-1] if c30 else None, list(v30[-20:])
        prev_nf = list(nf30[-48:])
        prev_v48 = list(v30[-48:])
        c30.append(close30)
        v30.append(vol30)
        nf30.append(net)
        if len(c30) > 400:
            del c30[:-200], v30[:-200], nf30[:-200]
        ti30 = ti(k, 6)
        r30 = close30 / prev_c30 - 1 if prev_c30 else None
        mean_v48 = sum(prev_v48) / len(prev_v48) if prev_v48 else 0.0
        relv = vol30 / mean_v48 if mean_v48 > 0 else None
        imp = (
            r30 / (ti30 * relv)
            if r30 is not None and ti30 and relv and abs(ti30 * relv) > 0.02
            else None
        )
        if imp is not None:
            imp_hist.append(imp)
            if len(imp_hist) > 480:
                imp_hist.pop(0)
        if r30 is None or len(prev_v30) < 20 or len(c30) < 16:
            continue
        pct = abs(r30) * 100
        rsi = _rsi(c30)
        volz = _z(vol30, prev_v30) or 0.0
        fi = bisect.bisect_right(fts, T) - 1
        fund = frs[fi] if fi >= 0 else None
        trig = (
            (pct > 2.0) << 0
            | ((rsi or 0) > 70) << 1
            | (volz > 2.5) << 2
            | (fund is not None and abs(fund) * 100 > 0.05) << 3
        )
        if not trig or T - last_event < COOLDOWN_S:
            continue
        i = k + 2  # first bar starting >= T + 5 min
        if i + HOLD_S // BAR >= n or o[i] is None:
            continue
        last_event = T
        entry = o[i]
        fwd = []
        for hz in HORIZONS:
            e = i + hz // 5 - 1
            fwd.append(c[e] / entry - 1 if e < n and c[e] is not None else None)
        bl = bracket(o, h, lo, c, i, +1, fts, frs, start)
        bs = bracket(o, h, lo, c, i, -1, fts, frs, start)
        if bl is None or bs is None:
            continue
        bk = (T - bstart) // BAR - 1
        btc240 = (
            (bc[bk] / bc[bk - 48] - 1) if 48 <= bk < len(bc) and bc[bk] and bc[bk - 48] else None
        )
        trs = []
        for j in range(16):
            a = k - 3 * (j + 1) + 1
            if a < 0 or any(c[x] is None for x in range(a, a + 3)):
                continue
            trs.append(
                (max(h[x] for x in range(a, a + 3)) - min(lo[x] for x in range(a, a + 3)))
                / c[a + 2]
            )
        atr = st.mean(trs) * 100 if len(trs) >= 12 else None
        ti15 = ti(k, 3)
        ti15p = ti(k - 3, 3)
        ti15pp = ti(k - 6, 3)
        dti = ti15 - ti15p if ti15 is not None and ti15p is not None else None
        acc = (dti - (ti15p - ti15pp)) if dti is not None and ti15pp is not None else None
        sgn = 1 if r30 > 0 else -1
        persist = 0
        for x in range(k, max(k - 12, -1), -1):
            if v[x] and tbv[x] is not None and (2 * tbv[x] - v[x]) * sgn > 0:
                persist += 1
            else:
                break
        flowz = _z(net, prev_nf)
        impact = (
            imp / st.median(imp_hist)
            if imp is not None and len(imp_hist) > 50 and st.median(imp_hist)
            else None
        )
        diverge = -sgn * ti30 if ti30 is not None else None
        d5, d15 = doi(k, 1), doi(k, 3)
        d5p = doi(k - 1, 1)
        r15 = ret(k, 3)
        oiz = _z(d15, doi15_hist[:-1]) if d15 is not None else None
        liq_long = max(0.0, -d15) if d15 is not None and r15 is not None and r15 < 0 else 0.0
        liq_short = max(0.0, -d15) if d15 is not None and r15 is not None and r15 > 0 else 0.0
        out.append(
            (
                sym,
                T,
                trig,
                sgn,
                r30,
                ret(k, 12),
                ret(k, 48),
                ret(k, 288),
                rsi,
                volz,
                fund,
                doi(k, 12),
                atr,
                btc240,
                ti(k, 1),
                ti15,
                ti30,
                ti(k, 12),
                dti,
                acc,
                persist,
                flowz,
                impact,
                diverge,
                d5,
                d15,
                oiz,
                liq_long if d15 is not None else None,
                liq_short if d15 is not None else None,
                (d5 - d5p) if d5 is not None and d5p is not None else None,
                *fwd,
                *bl,
                *bs,
            )
        )
    return out


def scan_symbol_safe(sym: str) -> tuple[str, list[tuple], str | None]:
    try:
        return sym, scan_symbol(sym), None
    except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the scan
        return sym, [], f"{type(exc).__name__}: {exc}"


def scan() -> None:
    conn = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True)
    symbols = [s for (s,) in conn.execute("SELECT DISTINCT symbol FROM klines5m ORDER BY symbol")]
    EVENTS_DB.unlink(missing_ok=True)
    db = sqlite3.connect(EVENTS_DB)
    db.execute(f"CREATE TABLE events ({', '.join(COLUMNS)})")
    with ProcessPoolExecutor(WORKERS) as pool:
        for i, (sym, rows, err) in enumerate(pool.map(scan_symbol_safe, symbols)):
            db.executemany(f"INSERT INTO events VALUES ({','.join('?' * len(COLUMNS))})", rows)
            db.commit()
            print(
                f"{i + 1}/{len(symbols)} {sym} {len(rows)}" + (f" ERROR {err}" if err else ""),
                flush=True,
            )
    print("done", flush=True)


if __name__ == "__main__":
    {"scan": scan}[sys.argv[1]]()
