"""Original AI bot - 6-month validation with PP1 OFF (and ON for comparison).

Pre-registered in docs/superpowers/reports/2026-10-02-original-bot-6m-validation.md
(section FÖRREGISTRERING) before the first run.

PART R - deterministic reconstruction, 2026-04-01 .. 2026-09-29 (Binance 5m,
P4 cache, 133 BingX-listed coins, real funding):
  discovery every 15 min (evaluations at :05/:20/:35/:50), top-80 universe by
  trailing 24 h quote volume >= 5 M USDT, the bot's own quant screener on 30m
  bars INCLUDING the in-progress bar (as BingX returns it), 60 min cooldown
  unless the score moved >= 0.15, ranking by candidate_score, analyses only
  for free LIVE slots (max 4), at most 10 per run.
  AI steps (opportunity screening, 7 roles / Risk agent, QA) CANNOT be
  replayed historically without leakage -> substitute (documented, not
  optimistic): every analysed candidate is CONFIRMED with an SL/TP pair drawn
  (seeded) from the bot's REAL Risk-agent SL/TP distances; the Gate's own
  rules are applied (SL < price < TP, R:R >= 1, signal age <= 30 min,
  liquidation guard SL < 9 % at 10x). Entry 15 min after the evaluation
  (analysis latency), next 5m open + 0.05 % slippage.
  Exits: exchange SL/TP on 5m bars (stop checked first), 6 h limit, PP1 OFF /
  ON (+1 % -> stop at entry from the next bar), Guardian has no exit authority
  in this config (0 Authority heuristics) -> no Guardian exits. Fees 0.05 % per
  side, 0.10 % slippage on stop exits, 0.03 % on time exits, funding paid on the
  open long at every settlement. 1000 USDT notional, max 4 open, 1 per symbol.

PART A - the REAL AI decisions (bot DB, 2026-09-01 .. 10-01): every quant
  candidate replayed with one identical exit model on BingX 1m (trade_review
  machinery), grouped by what the AI chain did with it: CONFIRMED / NO_TRADE /
  REJECTED / BUDGET_LIMITED (never analysed). Measures the AI's selection
  value without leakage (the decisions were made at the time).

    uv run --with numpy python -m crypto_trading.entry_research.original_bot_6m
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
import statistics as st
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

CACHE = Path("data/entry_research/p4_cache")
BOT_DB = Path("data/crypto_trading.db")
OUT = Path("docs/superpowers/reports/2026-10-02-original-bot-6m-validation.json")
T0 = 1754006400000
STEP = 300_000
START = datetime(2026, 4, 1, tzinfo=UTC)
END = datetime(2026, 9, 29, 18, tzinfo=UTC)  # last full 6 h before the cache ends (09-30 00:00)
PERIODS = {
    "TRAIN": (datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)),
    "VALID": (datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
    "TEST": (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
    "HOLDOUT": (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)),
}
# screener config (crypto_trading/config/pipeline.yaml at the tested commit)
PV_THR, RSI_P, RSI_THR, VZ_THR, FR_THR, LOOKBACK = 2.0, 14, 70.0, 2.5, 0.05, 20
TOP_N, MIN_QV24 = 80, 5_000_000
COOLDOWN_MIN, RESCORE = 60, 0.15
MAX_PER_RUN, MAX_OPEN = 10, 4
ANALYSIS_LATENCY_BARS = 3          # 15 min
MAX_SL_PCT = 9.0                   # liquidation guard at 10x
NOTIONAL = 1000.0
FEE, ENTRY_SLIP, STOP_SLIP, TIME_SLIP = 0.0005, 0.0005, 0.0010, 0.0003
HOLD_BARS = 72                     # 6 h of 5m bars
EVAL_OFFSETS = (1, 4)              # 5m bar offsets inside each 30m bar -> :05/:20 and :35/:50


def ts(i: int) -> datetime:
    return datetime.fromtimestamp((T0 + STEP * i) / 1000, UTC)


def idx(t: datetime) -> int:
    return int((t.timestamp() * 1000 - T0) // STEP)


# ------------------------------------------------------------------ AI SL/TP substitute

def real_brackets() -> list[tuple[float, float]]:
    """(sl %, tp %) of every position the real bot opened (Risk agent output after the Gate)."""
    db = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    out = []
    for fill, sl, tp in db.execute("SELECT simulated_fill_entry, stop_loss, target FROM positions"):
        e, s, t = float(fill), float(sl), float(tp)
        if e > 0 and s < e < t:
            out.append(((1 - s / e) * 100, (t / e - 1) * 100))
    return out


def draw_bracket(brackets: list[tuple[float, float]], key: str) -> tuple[float, float]:
    h = int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)
    return brackets[h % len(brackets)]


# ------------------------------------------------------------------ screener (vectorised per symbol)

@dataclass
class Sym:
    name: str
    c: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    qv: np.ndarray
    cf: np.ndarray
    fr: np.ndarray


def load_symbols() -> list[Sym]:
    names = json.loads((CACHE / "symbols.json").read_text())
    arr = {k: np.load(CACHE / f"{k}.npy", mmap_mode="r") for k in ("c", "h", "l", "qv", "cf", "fr_last")}
    out = []
    for i, n in enumerate(names):
        out.append(Sym(n, np.asarray(arr["c"][i], "f8"), np.asarray(arr["h"][i], "f8"), np.asarray(arr["l"][i], "f8"),
                       np.asarray(arr["qv"][i], "f8"), np.nan_to_num(np.asarray(arr["cf"][i], "f8")),
                       np.asarray(arr["fr_last"][i], "f8")))
    return out


def screen(sym: Sym, evals: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For evaluation index e (data known = bars < e): triggered flag, score, trigger bitmask
    (1 price, 2 rsi, 4 volume, 8 funding). Uses the in-progress 30m bar like the live bot."""
    c, qv = sym.c, sym.qv
    nb = len(c) // 6
    vol30 = np.nansum(np.nan_to_num(qv[: nb * 6]).reshape(nb, 6), axis=1)  # full 30m bar volumes
    trig = np.zeros(len(evals), bool)
    score = np.zeros(len(evals))
    mask = np.zeros(len(evals), int)
    for k, e in enumerate(evals):
        s = (e // 6) * 6                       # start of the current 30m bar
        if s - 6 * (LOOKBACK + 1) < 0 or e <= s:
            continue
        last = c[e - 1]
        full_close = c[s - 1:s - 6 * (RSI_P + 1) - 1:-6][::-1]   # closes of the previous full 30m bars
        if len(full_close) < RSI_P or not np.isfinite(last) or not np.isfinite(full_close).all():
            continue
        prev = full_close[-1]
        pct = abs(last / prev - 1) * 100
        closes = np.append(full_close[-RSI_P:], last)
        d = np.diff(closes)
        g, l_ = np.clip(d, 0, None).mean(), np.clip(-d, 0, None).mean()
        rsi = 50.0 if g == 0 and l_ == 0 else (100.0 if l_ == 0 else 100 - 100 / (1 + g / l_))
        vol_now = np.nansum(qv[s:e])
        hist = vol30[s // 6 - LOOKBACK:s // 6]
        mu, sd = hist.mean(), hist.std()
        z = (0.0 if vol_now == mu else 1000.0) if sd <= 0 else (vol_now - mu) / sd
        fr = abs(sym.fr[e - 1]) * 100 if np.isfinite(sym.fr[e - 1]) else 0.0
        vals = ((pct, PV_THR), (rsi, RSI_THR), (z, VZ_THR), (fr, FR_THR))
        m = sum(bit for bit, (v, t) in zip((1, 2, 4, 8), vals) if v > t)
        if m:
            trig[k] = True
            mask[k] = m
            score[k] = sum(min(max(0.0, (v - t) / t), 1.0) for v, t in vals) / 4
    return trig, score, mask


# ------------------------------------------------------------------ exits

def simulate(sym: Sym, entry_i: int, sl_pct: float, tp_pct: float, pp: bool) -> dict | None:
    if entry_i + 2 >= len(sym.c) or not np.isfinite(sym.c[entry_i - 1]):
        return None
    entry = sym.c[entry_i - 1] * (1 + ENTRY_SLIP)   # open of the entry bar = previous close, + slippage
    sl, tp = entry * (1 - sl_pct / 100), entry * (1 + tp_pct / 100)
    stop, peak = sl, entry
    reason, exit_px, slip, j_end = "TIME", None, TIME_SLIP, None
    for j in range(entry_i, min(entry_i + HOLD_BARS, len(sym.c))):
        o = sym.c[j - 1]
        h, lo, c = sym.h[j], sym.lo[j], sym.c[j]
        if not (np.isfinite(h) and np.isfinite(lo) and np.isfinite(c) and np.isfinite(o)):
            continue
        if lo <= stop:
            exit_px = min(o, stop)
            reason = "SL" if stop <= sl * 1.000001 else "BE"
            slip, j_end = STOP_SLIP, j
            break
        if h >= tp:
            reason, exit_px, slip, j_end = "TP", max(o, tp), 0.0, j
            break
        peak = max(peak, h)
        if pp and (peak / entry - 1) >= 0.01:
            stop = max(stop, entry)              # applies from the next bar
    if exit_px is None:
        j_end = min(entry_i + HOLD_BARS, len(sym.c)) - 1
        while j_end > entry_i and not np.isfinite(sym.c[j_end]):
            j_end -= 1
        exit_px = sym.c[j_end]
    gross = NOTIONAL * (exit_px / entry - 1)
    fees = NOTIONAL * FEE + NOTIONAL * (exit_px / entry) * FEE
    slippage = NOTIONAL * slip + NOTIONAL * ENTRY_SLIP
    funding = NOTIONAL * float(sym.cf[j_end] - sym.cf[entry_i - 1])   # a long PAYS positive funding
    # gross is measured from the slipped entry; add the entry slippage back so `gross` is pre-cost
    gross_pre = gross + NOTIONAL * ENTRY_SLIP
    net = gross - fees - NOTIONAL * slip - funding
    return {"gross": gross_pre, "fees": fees, "slippage": slippage, "funding": funding, "net": net,
            "reason": reason, "exit_i": j_end, "mfe": (peak / entry - 1) * 100}


# ------------------------------------------------------------------ portfolio replay

def reconstruct(pp: bool, brackets, syms, screened) -> tuple[list[dict], dict]:
    evals_all = screened["evals"]
    open_until: dict[str, int] = {}
    last_cand: dict[str, tuple[int, float]] = {}
    trades = []
    funnel = defaultdict(int)
    for k, e in enumerate(evals_all):
        open_until = {s: x for s, x in open_until.items() if x >= e}
        # universe: top-80 by trailing 24 h quote volume, >= 5 M
        qv24 = screened["qv24"][:, k]
        ok = np.isfinite(qv24) & (qv24 >= MIN_QV24)
        order = np.argsort(-np.where(ok, qv24, -1))[:TOP_N]
        cands = []
        for si in order:
            if not ok[si] or not screened["trig"][si, k]:
                continue
            funnel["screened_triggered"] += 1
            name = syms[si].name
            sc = screened["score"][si, k]
            prev = last_cand.get(name)
            if prev and (e - prev[0]) * 5 < COOLDOWN_MIN and abs(sc - prev[1]) < RESCORE:
                funnel["cooldown_skip"] += 1
                continue
            last_cand[name] = (e, sc)
            cands.append((sc, si))
        funnel["candidates"] += len(cands)
        free = MAX_OPEN - len(open_until)
        budget = min(MAX_PER_RUN, max(free, 0))
        ranked = sorted(cands, reverse=True)
        funnel["budget_limited"] += max(0, len(ranked) - budget)
        for sc, si in ranked[:budget]:
            sym = syms[si]
            if sym.name in open_until:
                funnel["symbol_already_open"] += 1
                continue
            funnel["analysed"] += 1
            sl_pct, tp_pct = draw_bracket(brackets, f"{sym.name}|{e}")
            if not (tp_pct >= sl_pct and 0 < sl_pct < MAX_SL_PCT):
                funnel["gate_or_kernel_reject"] += 1
                continue
            entry_i = e + ANALYSIS_LATENCY_BARS
            if len(open_until) >= MAX_OPEN:
                funnel["no_slot_at_entry"] += 1
                continue
            res = simulate(sym, entry_i, sl_pct, tp_pct, pp)
            if res is None:
                continue
            open_until[sym.name] = res["exit_i"]
            funnel["trades"] += 1
            trades.append({"symbol": sym.name, "entry_i": entry_i, "t": ts(entry_i), "t_exit": ts(res["exit_i"]),
                           "score": float(sc), "mask": int(screened["mask"][si, k]), "sl_pct": sl_pct,
                           "tp_pct": tp_pct, **res})
    return trades, dict(funnel)


def run_screen(syms: list[Sym]) -> dict:
    a, b = idx(START), idx(END)
    evals = np.array([i for i in range(a, b) if i % 6 in EVAL_OFFSETS])
    S = len(syms)
    trig = np.zeros((S, len(evals)), bool)
    score = np.zeros((S, len(evals)))
    mask = np.zeros((S, len(evals)), int)
    qv24 = np.zeros((S, len(evals)))
    for si, sym in enumerate(syms):
        t_, s_, m_ = screen(sym, evals)
        trig[si], score[si], mask[si] = t_, s_, m_
        csum = np.concatenate([[0.0], np.cumsum(np.nan_to_num(sym.qv))])
        qv24[si] = csum[evals] - csum[np.maximum(evals - 288, 0)]
        qv24[si][~np.isfinite(sym.c[evals - 1])] = np.nan
    return {"evals": evals, "trig": trig, "score": score, "mask": mask, "qv24": qv24}


# ------------------------------------------------------------------ statistics

def day_bootstrap(trades: list[dict], key: str = "net", n: int = 5000, seed: int = 3) -> dict:
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["t_exit"].date()].append(t)
    days = list(by_day)
    rng = random.Random(seed)
    tot, exp_, wr = [], [], []
    for _ in range(n):
        sample = [x for _ in days for x in by_day[rng.choice(days)]]
        if not sample:
            continue
        vals = [x[key] for x in sample]
        tot.append(sum(vals))
        exp_.append(st.mean(vals))
        wr.append(sum(v > 0 for v in vals) / len(vals))

    def ci(xs):
        xs = sorted(xs)
        return [xs[int(0.025 * len(xs))], xs[int(0.975 * len(xs))]]

    return {"total_ci95": ci(tot), "expectancy_ci95": ci(exp_), "winrate_ci95": ci(wr),
            "p_total_le_0": sum(x <= 0 for x in tot) / len(tot)}


def describe(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    days = defaultdict(float)
    weeks = defaultdict(float)
    months = defaultdict(float)
    for t in trades:
        d = t["t_exit"].date()
        days[d] += t["net"]
        weeks[str(d - timedelta(days=d.weekday()))] += t["net"]
        months[d.strftime("%Y-%m")] += t["net"]
    all_days = [(START + timedelta(days=i)).date() for i in range((END - START).days + 1)]
    dvals = [days.get(d, 0.0) for d in all_days]
    eq, peak, mdd, dd_len, cur = 0.0, 0.0, 0.0, 0, 0
    for v in dvals:
        eq += v
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
        cur = cur + 1 if eq < peak else 0
        dd_len = max(dd_len, cur)
    gross = sum(t["gross"] for t in trades)
    costs = sum(t["fees"] + t["slippage"] + t["funding"] for t in trades)
    pos_d = [v for v in dvals if v > 0]
    neg_d = [v for v in dvals if v < 0]
    return {
        "trades": len(nets), "total_net": sum(nets),
        "return_pct_on_1000_account": 100 * sum(nets) / 1000.0,
        "winners": len(wins), "losers": len(losses), "win_rate": len(wins) / len(nets),
        "avg_win": st.mean(wins) if wins else 0.0, "avg_loss": st.mean(losses) if losses else 0.0,
        "median_trade": st.median(nets), "expectancy": st.mean(nets),
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
        "largest_win": max(nets), "largest_loss": min(nets),
        "max_drawdown_usdt": mdd, "dd_duration_days": dd_len,
        "days": len(all_days), "pos_days": len(pos_d), "pos_day_share": len(pos_d) / len(all_days),
        "avg_pos_day": st.mean(pos_d) if pos_d else 0.0, "avg_neg_day": st.mean(neg_d) if neg_d else 0.0,
        "median_day": st.median(dvals), "best_day": max(dvals), "worst_day": min(dvals),
        "gross": gross, "fees": sum(t["fees"] for t in trades), "slippage": sum(t["slippage"] for t in trades),
        "funding": sum(t["funding"] for t in trades), "costs_total": costs,
        "cost_per_trade": costs / len(nets), "cost_share_of_gross": costs / gross if gross > 0 else None,
        "weeks": dict(sorted(weeks.items())), "months": dict(sorted(months.items())),
        "exits": {r: sum(1 for t in trades if t["reason"] == r) for r in ("TP", "SL", "BE", "TIME")},
    }


def drop_top(trades: list[dict], k: int) -> float:
    nets = sorted((t["net"] for t in trades), reverse=True)
    return sum(nets[k:])


# ------------------------------------------------------------------ part A: real AI decisions

def part_a() -> dict:
    from crypto_trading.entry_research.trade_review import RULES, Paths, _assessments
    from crypto_trading.entry_research.trade_review import simulate as tr_sim

    bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    paths = Paths()
    rows = bot.execute("SELECT candidate_id, instrument, status, created_at FROM candidates ORDER BY created_at").fetchall()
    groups = defaultdict(list)
    for cid, symbol, status, created in rows:
        t_sig = datetime.fromisoformat(created)
        t_entry = (t_sig + timedelta(minutes=15)).replace(second=0, microsecond=0)
        bars = paths.bars(symbol, t_entry, t_entry + timedelta(hours=6, minutes=2))
        if len(bars) < 30:
            groups[status + "_no_path"].append(None)
            continue
        entry = bars[0][1] * (1 + ENTRY_SLIP)
        risk = _assessments(bot, cid).get("risk") or {}
        try:
            sl, tp = float(risk.get("suggested_stop_loss")), float(risk.get("suggested_target"))
            own = sl < entry < tp and (entry - sl) / entry < 0.25
        except (TypeError, ValueError):
            own = False
        if not own:
            sl, tp = entry * (1 - 0.043), entry * (1 + 0.05)
        for rule_name in ("NO_PP", "BASE"):
            out = tr_sim(next(r for r in RULES if r.name == rule_name), entry, sl, tp, bars[1:], None)
            if out is not None:
                groups[f"{status}|{rule_name}"].append(out.pnl)  # entry price already carries the slippage
        groups[f"{status}|own_brackets"].append(own)
    res = {}
    for key, vals in groups.items():
        if "|" in key and not key.endswith("own_brackets"):
            v = [x for x in vals if x is not None]
            res[key] = {"n": len(v), "mean": st.mean(v) if v else 0.0, "win_rate": sum(x > 0 for x in v) / len(v) if v else 0.0,
                        "total_per_trade_1000": sum(v)}
        elif key.endswith("own_brackets"):
            res[key] = sum(vals) / len(vals)
        else:
            res[key] = len(vals)
    return res


# ------------------------------------------------------------------ baselines

def baselines() -> dict:
    """Trend (frozen), BTC/ETH buy-and-hold and cash over the same window (daily engine)."""
    import pandas as pd

    from crypto_trading.entry_research import final_validation as F

    d = F.load()
    sc = F.Scenario()
    a, b = START.strftime("%Y-%m-%d"), (END - timedelta(days=1)).strftime("%Y-%m-%d")
    out = {}
    for name, r in (("TREND", F.run_trend(d, sc)), ("BUY&HOLD BTC/ETH 1x", F.buy_hold(d, sc))):
        x = r.daily[a:b]
        out[name] = {"net_per_1000": float(1000 * ((1 + x).prod() - 1)), "pos_day_share": float((x > 0).mean()),
                     "mdd": float(((1 + x).cumprod() / (1 + x).cumprod().cummax() - 1).min()),
                     "months": {str(k)[:7]: float(1000 * ((1 + v).prod() - 1))
                                for k, v in x.groupby(pd.Grouper(freq="MS"))}}
    out["CASH"] = {"net_per_1000": 0.0, "pos_day_share": 0.0, "mdd": 0.0}
    return out


# ------------------------------------------------------------------ main

def main() -> None:
    syms = load_symbols()
    brackets = real_brackets()
    screened = run_screen(syms)
    result = {"brackets_n": len(brackets),
              "brackets_median": [st.median(x[0] for x in brackets), st.median(x[1] for x in brackets)]}
    regime = {}
    btc = next(s for s in syms if s.name == "BTCUSDT")
    for pp in (False, True):
        label = "PP1_ON" if pp else "PP1_OFF"
        trades, funnel = reconstruct(pp, brackets, syms, screened)
        desc = describe(trades)
        desc["funnel"] = funnel
        desc["bootstrap_net"] = day_bootstrap(trades, "net")
        desc["bootstrap_gross"] = day_bootstrap(trades, "gross")
        desc["periods"] = {p: describe([t for t in trades if a <= t["t"] < b]) for p, (a, b) in PERIODS.items()}
        for p in desc["periods"].values():
            p.pop("weeks", None)
        desc["drop_top"] = {k: drop_top(trades, k) for k in (1, 3, 5, 10)}
        by_sym = defaultdict(float)
        for t in trades:
            by_sym[t["symbol"]] += t["net"]
        top_syms = sorted(by_sym.items(), key=lambda kv: -kv[1])
        desc["symbols_top5"] = top_syms[:5]
        desc["symbols_bottom5"] = top_syms[-5:]
        desc["total_without_top5_symbols"] = sum(v for _, v in top_syms[5:])
        # regime of each trade: BTC 7-day return at entry (known then)
        reg = defaultdict(list)
        for t in trades:
            i = t["entry_i"] - 1
            r7 = btc.c[i] / btc.c[i - 2016] - 1 if i >= 2016 else 0.0
            reg["UP" if r7 > 0.05 else ("DOWN" if r7 < -0.05 else "SIDEWAYS")].append(t["net"])
        desc["regimes"] = {k: {"n": len(v), "total": sum(v), "mean": st.mean(v), "win_rate": sum(x > 0 for x in v) / len(v)}
                           for k, v in reg.items()}
        q = sorted(t["score"] for t in trades)
        cuts = [q[int(len(q) * f)] for f in (0.2, 0.4, 0.6, 0.8)] if q else []
        sq = defaultdict(list)
        for t in trades:
            sq[sum(t["score"] > c for c in cuts)].append(t["net"])
        desc["score_quintiles"] = {str(k): {"n": len(v), "mean": st.mean(v)} for k, v in sorted(sq.items())}
        trig = defaultdict(list)
        for t in trades:
            for bit, name in ((1, "price"), (2, "rsi"), (4, "volume"), (8, "funding")):
                if t["mask"] & bit:
                    trig[name].append(t["net"])
        desc["by_trigger"] = {k: {"n": len(v), "mean": st.mean(v)} for k, v in trig.items()}
        # rolling 30-day windows (for the +393 question)
        days = defaultdict(float)
        for t in trades:
            days[t["t_exit"].date()] += t["net"]
        dl = [(START + timedelta(days=i)).date() for i in range((END - START).days + 1)]
        dv = [days.get(d, 0.0) for d in dl]
        roll = [sum(dv[i:i + 30]) for i in range(len(dv) - 29)]
        desc["rolling30"] = {"min": min(roll), "p05": float(np.percentile(roll, 5)), "median": float(np.median(roll)),
                             "p95": float(np.percentile(roll, 95)), "max": max(roll),
                             "share_ge_393": sum(r >= 393 for r in roll) / len(roll)}
        rng = random.Random(9)
        sims = [sum(rng.choice(dv) for _ in range(17)) for _ in range(20000)]
        desc["random17days"] = {"p50": float(np.median(sims)), "p95": float(np.percentile(sims, 95)),
                                "share_ge_393": sum(s >= 393 for s in sims) / len(sims)}
        result[label] = desc
    result["part_a_real_ai"] = part_a()
    result["baselines"] = baselines()
    OUT.write_text(json.dumps(result, default=str, indent=1))
    print("written", OUT)


if __name__ == "__main__":
    main()
