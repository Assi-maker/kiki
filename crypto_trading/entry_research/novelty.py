"""Novelty / information-gain check of NEW information sources (2026-09-30).

    python -m crypto_trading.entry_research.novelty

Read-only research; the bot never imports this. Runs on TRAIN + VALID ONLY -
no TEST or HOLDOUT row is loaded - so it can decide which (if any) new
feature is worth an event lab without touching the out-of-sample data.

Question (user): does taker flow or order-book data carry information that
the existing features (price, volume, funding, open interest) do not - and is
it large enough to matter after costs? If it only duplicates what we have,
drop it.

Grid: every symbol at every full hour T (point-in-time, bars closed <= T).
Target: log return from the open of the first bar starting >= T + 5 min
(= LIVE latency) to 1 h and 4 h later, in %.

Existing features (the control set, from event_engine.features_at):
  ret12 ret48 ret288 vz z3 z12 atr_l fund oi1h oi4h

New features, fixed before any result was seen
  taker (from the 5m klines' taker-buy volume, all 135 symbols):
    TI_5m   2 x takerbuy / volume - 1 of the last 5m bar
    TI_1h   the same over the last 12 bars
    dTI     TI_1h(T) - TI_1h(T - 1 h)                    (change)
    accTI   dTI(T) - dTI(T - 1 h)                        (acceleration)
    TIz     (TI_1h - mean) / sd over the previous 7 days of hourly TI_1h
            (relative to normal activity)
  order book (archive bookDepth, 30 most liquid symbols by TRAIN volume,
  every 3rd day; snapshot <= 120 s old at T; DEPTHr baseline needs >= 24
  hourly samples in the previous 7 days):
    IMB02   (bid - ask) / (bid + ask) notional within 0.2 % of mid
    IMB1    the same within 1 %
    dIMB1   IMB1(T) - IMB1(T - 15 min)
    DEPTHr  depth within 1 % / its median over the previous 7 days (hourly)

Measures per feature, per period (TRAIN, VALID)
- IC: rank correlation with the target.
- partial IC: rank correlation after regressing BOTH the feature's rank and
  the target's rank on the ranks of all 10 existing features (OLS) - the
  information the existing features do not already carry. One-sided
  clustered p (4 h blocks) on the product of the standardised residuals.
- redundancy: max |rank correlation| with any existing feature.
- magnitude: mean target of the feature's top decile minus bottom decile
  (decile cuts from TRAIN), in %, versus the round-trip cost of ~0.15 %.

Pre-registered PASS (a feature may go on to a small, pre-defined event lab):
  (a) TRAIN |partial IC| >= 0.02 with p < 0.01 at 1 h or 4 h,
  (b) VALID same sign and |partial IC| >= 0.01 at that horizon,
  (c) redundancy < 0.7,
  (d) |decile spread| >= 0.15 % in TRAIN and VALID, same sign as the IC.
Anything else is dropped.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from array import array
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from crypto_trading.entry_research import event_engine as ee
from crypto_trading.entry_research import regime_lab as rl
from crypto_trading.entry_research.stats import cluster_t, t_sf

OUT = Path("data/entry_research")
EXISTING = ("ret12", "ret48", "ret288", "vz", "z3", "z12", "atr_l", "fund", "oi1h", "oi4h")
TAKER = ("TI_5m", "TI_1h", "dTI", "accTI", "TIz")
BOOK = ("IMB02", "IMB1", "dIMB1", "DEPTHr")
# Evaluation groups (one regression design each, on the rows where every
# member is known). DEPTHr has its own group: its 7-day baseline needs
# history that the other book features do not (fix 2026-09-30: grouped
# together, the every-3rd-day sample left only 1 380 TRAIN rows).
GROUPS = (TAKER, ("IMB1", "dIMB1"), ("DEPTHr",), ("IMB02",))  # IMB02: only from ~2026-01
TARGETS = ("fwd1h", "fwd4h")
PERIODS = ("TRAIN", "VALID")  # TEST / HOLDOUT are never loaded here
COLS = ("T", *EXISTING, *TAKER, *BOOK, *TARGETS)
WORKERS = 2
COST_PCT = 0.15


def _load_taker(conn: sqlite3.Connection, sym: str, s: ee.Series5) -> list:
    tbv: list = [None] * len(s.c)
    for t, x in conn.execute("SELECT t, tbv FROM klines5m WHERE symbol = ? ORDER BY t", (sym,)):
        tbv[(t // 1000 - s.start) // rl.BAR_S] = x
    return tbv


def _load_book(conn: sqlite3.Connection, sym: str) -> dict[int, tuple]:
    return {
        t // 1000: (i02, i1, d1)
        for t, i02, i1, d1 in conn.execute(
            "SELECT t, imb02, imb1, depth1 FROM book5m WHERE symbol = ?", (sym,)
        )
    }


def _ti(s: ee.Series5, tbv: list, a: int, b: int) -> float | None:
    v = tb = 0.0
    for j in range(a, b + 1):
        if s.v[j] is None or tbv[j] is None:
            return None
        v += s.v[j]
        tb += tbv[j]
    return 2 * tb / v - 1 if v > 0 else None


def scan_symbol(sym: str) -> dict[str, array]:
    conn = sqlite3.connect(f"file:{rl.ARCHIVE_DB}?mode=ro", uri=True)
    s = rl.load_series(conn, sym)
    if s is None:
        return {}
    d = rl.load_derivs(conn, [sym])
    tbv = _load_taker(conn, sym, s)
    book = _load_book(conn, sym)
    conn.close()
    p = ee.Prepared(s)
    out = {c: array("d") for c in COLS}
    ti_hist: deque = deque(maxlen=7 * 24)
    depth_hist: deque = deque(maxlen=7 * 24)
    ti_prev: deque = deque(maxlen=3)  # TI_1h at T-2h, T-1h, T
    nan = float("nan")
    end = rl.PERIODS[1][2]  # end of VALID: nothing later is ever read
    for k in range(ee.HIST, len(s.c) - 50):
        T = s.close_time(k)
        if T % rl.HOUR:
            continue
        if T + rl.LAT_S + 4 * rl.HOUR >= end:
            break
        ti1h = _ti(s, tbv, k - 11, k)
        ti_prev.append(ti1h)
        pn = rl.period(T)
        f = ee.features_at(p, k)
        i = k + 2
        ok = pn in PERIODS and f is not None and s.o[i] and s.c[i + 11] and s.c[i + 47]
        if ok:
            f = ee.with_derivs(f, sym, T, d)
            ex = [f.get(c) for c in EXISTING]
            if all(x is not None and math.isfinite(x) for x in ex):
                dti = acc = tiz = nan
                if len(ti_prev) == 3 and None not in ti_prev:
                    a0, a1, a2 = ti_prev
                    dti = a2 - a1
                    acc = (a2 - a1) - (a1 - a0)
                if ti1h is not None and len(ti_hist) >= 72:
                    m = sum(ti_hist) / len(ti_hist)
                    sd = math.sqrt(sum((x - m) ** 2 for x in ti_hist) / len(ti_hist))
                    tiz = (ti1h - m) / sd if sd > 0 else nan
                ti5 = _ti(s, tbv, k, k)
                bk, bk15 = book.get(T), book.get(T - 900)
                imb02 = bk[0] if bk and bk[0] is not None else nan
                imb1 = bk[1] if bk else nan
                dimb = bk[1] - bk15[1] if bk and bk15 else nan
                dep = (
                    bk[2] / sorted(depth_hist)[len(depth_hist) // 2]
                    if bk and len(depth_hist) >= 24  # >= 1 day of samples (every-3rd-day book data)
                    else nan
                )
                row = (
                    T,
                    *ex,
                    ti5 if ti5 is not None else nan,
                    ti1h if ti1h is not None else nan,
                    dti,
                    acc,
                    tiz,
                    imb02,
                    imb1,
                    dimb,
                    dep,
                    100 * math.log(s.c[i + 11] / s.o[i]),
                    100 * math.log(s.c[i + 47] / s.o[i]),
                )
                for c, x in zip(COLS, row, strict=True):
                    out[c].append(x)
        if ti1h is not None:
            ti_hist.append(ti1h)
        bk = book.get(T)
        if bk:
            depth_hist.append(bk[2])
    out["period"] = array("b", (PERIODS.index(rl.period(int(t))) for t in out["T"]))
    return out


# ------------------------------------------------------------------ statistics


def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=xs.__getitem__)
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2
        for q in range(i, j + 1):
            r[order[q]] = avg
        i = j + 1
    return r


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                fct = m[r][c] / m[c][c]
                for q in range(c, n + 1):
                    m[r][q] -= fct * m[c][q]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


class Residualizer:
    """OLS on [1, existing-feature ranks]; X'X is built once per row set."""

    def __init__(self, xs: list[list[float]]) -> None:
        self.cols = [[1.0] * len(xs[0]), *xs]
        k = len(self.cols)
        self.xtx = [
            [sum(a * b for a, b in zip(self.cols[i], self.cols[j], strict=True)) for j in range(k)]
            for i in range(k)
        ]

    def __call__(self, y: list[float]) -> list[float]:
        xty = [sum(a * b for a, b in zip(c, y, strict=True)) for c in self.cols]
        beta = _solve(self.xtx, xty)
        fit = [0.0] * len(y)
        for bcoef, c in zip(beta, self.cols, strict=True):
            for i, x in enumerate(c):
                fit[i] += bcoef * x
        return [yy - ff for yy, ff in zip(y, fit, strict=True)]


def corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    return sab / math.sqrt(saa * sbb) if saa > 0 and sbb > 0 else 0.0


def ic_p(a: list[float], b: list[float], clusters: list[int]) -> tuple[float, float]:
    """Correlation and one-sided clustered p that |corr| > 0 in its sign."""
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a) / n)
    sb = math.sqrt(sum((y - mb) ** 2 for y in b) / n)
    if sa == 0 or sb == 0:
        return 0.0, 1.0
    prod = [(x - ma) / sa * (y - mb) / sb for x, y in zip(a, b, strict=True)]
    t, g = cluster_t(prod, clusters)
    ic = sum(prod) / n
    return ic, (t_sf(abs(t), g - 1) if g > 1 else 1.0)


def evaluate_group(data: dict[str, list], feats: tuple, cuts: dict) -> dict[str, dict]:
    """Stats of a feature group in one period, on the rows where every
    feature of the group is known (one regression design for the group)."""
    idx = [i for i in range(len(data["T"])) if all(data[f][i] == data[f][i] for f in feats)]
    if len(idx) < 500:
        return {f: {"n": len(idx)} for f in feats}
    rex = [ranks([data[c][i] for i in idx]) for c in EXISTING]
    resid = Residualizer(rex)
    cl = [int(data["T"][i]) // (4 * rl.HOUR) for i in idx]
    ys = {tg: [data[tg][i] for i in idx] for tg in TARGETS}
    rys = {tg: ranks(ys[tg]) for tg in TARGETS}
    res_y = {tg: resid(rys[tg]) for tg in TARGETS}
    out = {}
    for feat in feats:
        f = [data[feat][i] for i in idx]
        rf = ranks(f)
        res_f = resid(rf)
        o = {"n": len(idx), "redundancy": round(max(abs(corr(rf, x)) for x in rex), 3)}
        if feat not in cuts:
            srt = sorted(f)
            cuts[feat] = (srt[len(srt) // 10], srt[len(srt) * 9 // 10])  # set on TRAIN only
        lo, hi = cuts[feat]
        for tg in TARGETS:
            y = ys[tg]
            ic, p = ic_p(rf, rys[tg], cl)
            pic, pp = ic_p(res_f, res_y[tg], cl)
            top = [y[j] for j in range(len(y)) if f[j] >= hi]
            bot = [y[j] for j in range(len(y)) if f[j] <= lo]
            spread = (sum(top) / len(top) - sum(bot) / len(bot)) if top and bot else None
            o[tg] = {
                "ic": round(ic, 4),
                "p": round(p, 5),
                "partial_ic": round(pic, 4),
                "partial_p": round(pp, 5),
                "decile_spread_pct": round(spread, 4) if spread is not None else None,
            }
        out[feat] = o
    return out


def verdict(tr: dict, va: dict) -> dict:
    res = {"pass": False, "why": []}
    if tr.get("n", 0) < 500 or va.get("n", 0) < 500:
        res["why"].append("too little data")
        return res
    if tr["redundancy"] >= 0.7:
        res["why"].append(f"redundant ({tr['redundancy']})")
    for tg in TARGETS:
        a, b = tr[tg], va[tg]
        ok = (
            abs(a["partial_ic"]) >= 0.02
            and a["partial_p"] < 0.01
            and b["partial_ic"] * a["partial_ic"] > 0
            and abs(b["partial_ic"]) >= 0.01
            and a["decile_spread_pct"] is not None
            and b["decile_spread_pct"] is not None
            and abs(a["decile_spread_pct"]) >= COST_PCT
            and abs(b["decile_spread_pct"]) >= COST_PCT
            and a["decile_spread_pct"] * a["partial_ic"] > 0
            and b["decile_spread_pct"] * a["partial_ic"] > 0
        )
        if ok and tr["redundancy"] < 0.7:
            res["pass"] = True
            res["horizon"] = tg
    if not res["pass"] and not res["why"]:
        res["why"].append("partial IC / VALID sign / size vs cost not met")
    return res


def run(book_only: bool = False) -> dict:
    conn = sqlite3.connect(f"file:{rl.ARCHIVE_DB}?mode=ro", uri=True)
    sql = (
        "SELECT DISTINCT symbol FROM book5m"
        if book_only
        else "SELECT DISTINCT symbol FROM klines5m"
    )
    symbols = sorted(s for (s,) in conn.execute(sql))
    conn.close()
    data: dict[str, dict[str, list]] = {p: {c: [] for c in COLS} for p in PERIODS}
    with ProcessPoolExecutor(WORKERS) as pool:
        for i, ch in enumerate(pool.map(scan_symbol, symbols, chunksize=1), 1):
            if ch:
                for j, pi in enumerate(ch["period"]):
                    dp = data[PERIODS[pi]]
                    for c in COLS:
                        dp[c].append(ch[c][j])
            if i % 20 == 0:
                print(f"{i}/{len(symbols)} symbols", flush=True)
    result: dict = {"rows": {p: len(data[p]["T"]) for p in PERIODS}, "features": {}}
    cuts: dict = {}
    for group in GROUPS[1:] if book_only else GROUPS:
        tr = evaluate_group(data["TRAIN"], group, cuts)  # decile cuts are set here, on TRAIN
        va = evaluate_group(data["VALID"], group, cuts)
        for feat in group:
            result["features"][feat] = {
                "TRAIN": tr[feat],
                "VALID": va[feat],
                "verdict": verdict(tr[feat], va[feat]),
            }
            print(feat, json.dumps(result["features"][feat]), flush=True)
    name = "novelty_book.json" if book_only else "novelty.json"
    (OUT / name).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


if __name__ == "__main__":
    run(book_only="--book-only" in sys.argv)
    sys.exit(0)
