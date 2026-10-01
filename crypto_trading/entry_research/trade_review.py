"""2026-10-01 trading-system review: trades, signals, entry features, exit replay.

Research only. Reads the bot DB read-only and the path cache built by
`trade_review_data.py`; writes JSON results under `data/entry_research/`.
Nothing here is imported by a trading path.

Two populations, both LONG (the bot is LONG-only):

- TRADES: every position the bot opened (PAPER and LIVE). Entry time is the
  LIVE fill time when the position went LIVE, else the gate time (the
  CANDIDATE_TRANSITIONED -> CONFIRMED event; `positions.opened_at` is the
  discovery-cycle start, minutes too early). Entry price is the LIVE fill or
  the PAPER fill. SL/TP are the position's own.
- SIGNALS: every quant candidate (incl. never-analysed BUDGET_LIMITED),
  entered `SIGNAL_LATENCY` after the candidate was created at that minute's
  close, with the Risk Agent's SL/TP when one exists, else the median
  geometry of the bot's own positions. First per symbol per 6 h only.

Exit replay on real BingX 1m candles, all P/L on 1000 USDT notional:
- a stop or lock is set from information up to a bar's CLOSE and only
  applies from the NEXT bar (no intrabar look-ahead);
- inside one bar the stop is checked before the target (conservative);
- a bar opening through a level fills at the open;
- costs: 0.05 % fee per side, 0.10 % slippage on every stop-type exit
  (SL, break-even, lock, trail), 0.03 % on market exits (time / retrace).
"""

from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

BOT_DB = Path("data/crypto_trading.db")
CACHE_DB = Path("data/entry_research/trade_review.db")
NOTIONAL = 1000.0
FEE = 0.0005
STOP_SLIP = 0.0010
MARKET_SLIP = 0.0003
HOLD = timedelta(hours=6)
SIGNAL_LATENCY = timedelta(minutes=10)
MEDIAN_SL_PCT = 4.3
MEDIAN_TP_PCT = 5.0
PERIODS = [
    ("TRAIN", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC)),
    ("VALID", datetime(2026, 9, 15, tzinfo=UTC), datetime(2026, 9, 22, tzinfo=UTC)),
    ("TEST", datetime(2026, 9, 22, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)),
    ("HOLDOUT", datetime(2026, 9, 28, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)),
]


def period_of(moment: datetime) -> str:
    for name, start, end in PERIODS:
        if start <= moment < end:
            return name
    return "OUT"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# ---------------------------------------------------------------- path data


class Paths:
    """1m (per symbol) and 5m (large-cap universe) candles from the cache."""

    def __init__(self, path: Path = CACHE_DB):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._1m: dict[str, tuple[list[int], list[tuple]]] = {}
        self._5m: dict[str, tuple[list[int], list[tuple]]] = {}

    def _load(self, table: str, store: dict, symbol: str):
        if symbol not in store:
            rows = self.db.execute(
                f"SELECT t,o,h,l,c,v FROM {table} WHERE symbol=? ORDER BY t", (symbol,)
            ).fetchall()
            store[symbol] = ([r[0] for r in rows], [r[1:] for r in rows])
        return store[symbol]

    def bars(self, symbol: str, start: datetime, end: datetime, table: str = "k1m") -> list[tuple]:
        """Candles whose OPEN time is in [start, end): (t, o, h, lo, c, v)."""
        ts, rows = self._load(table, self._1m if table == "k1m" else self._5m, symbol)
        i, j = bisect_left(ts, _ms(start)), bisect_left(ts, _ms(end))
        return [(ts[k], *rows[k]) for k in range(i, j)]

    def universe(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT symbol FROM k5m")]


# ---------------------------------------------------------------- features


def _ret(closes: list[float], minutes: int) -> float | None:
    if len(closes) <= minutes or closes[-1 - minutes] <= 0:
        return None
    return (closes[-1] / closes[-1 - minutes] - 1) * 100


def _resample(bars: list[tuple], minutes: int) -> list[tuple]:
    """1m bars -> `minutes` bars aligned on the epoch: (t, o, h, lo, c, v)."""
    out: list[list] = []
    step = minutes * 60_000
    for t, o, h, lo, c, v in bars:
        bucket = t - t % step
        if out and out[-1][0] == bucket:
            row = out[-1]
            row[2], row[3], row[4], row[5] = max(row[2], h), min(row[3], lo), c, row[5] + v
        else:
            out.append([bucket, o, h, lo, c, v])
    return [tuple(r) for r in out]


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1 : -1], closes[-n:], strict=False):
        diff = b - a
        gains += max(diff, 0.0)
        losses += max(-diff, 0.0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def entry_features(paths: Paths, symbol: str, t_entry: datetime, market: Market) -> dict:
    """Everything known at `t_entry` - only candles that CLOSED before it."""
    pre = paths.bars(symbol, t_entry - timedelta(hours=24), t_entry)
    if len(pre) < 600:
        return {"pre_bars": len(pre)}
    closes = [b[4] for b in pre]
    highs = [b[2] for b in pre]
    lows = [b[3] for b in pre]
    vols = [b[5] for b in pre]
    price = closes[-1]
    f: dict[str, float | None] = {"pre_bars": len(pre)}
    for m in (15, 30, 60, 120, 240, 720, 1380):
        f[f"ret_{m}m"] = _ret(closes, m)
    f["accel_30m"] = (
        f["ret_30m"] - ((closes[-31] / closes[-61] - 1) * 100)
        if f["ret_30m"] is not None and len(closes) > 61
        else None
    )
    f["dist_hi_4h"] = (price / max(highs[-240:]) - 1) * 100
    f["dist_hi_24h"] = (price / max(highs) - 1) * 100
    f["dist_lo_24h"] = (price / min(lows) - 1) * 100
    rng = max(highs) - min(lows)
    f["pos_24h"] = (price - min(lows)) / rng if rng > 0 else None
    typical = [(h + lo + c) / 3 for h, lo, c in zip(highs, lows, closes, strict=False)]
    for m, name in ((240, "4h"), (len(pre), "24h")):
        vw = sum(t * v for t, v in zip(typical[-m:], vols[-m:], strict=False))
        vv = sum(vols[-m:])
        f[f"dist_vwap_{name}"] = (price / (vw / vv) - 1) * 100 if vv > 0 else None
    for m in (60, 240):
        f[f"dist_sma_{m}m"] = (price / (sum(closes[-m:]) / m) - 1) * 100
    b15 = _resample(pre, 15)
    trs = [(h - lo) / c * 100 for _, _, h, lo, c, _ in b15 if c > 0]
    f["atr15_pct"] = sum(trs) / len(trs) if trs else None
    recent = trs[-4:]
    f["atr_expansion"] = (
        (sum(recent) / len(recent)) / f["atr15_pct"] if trs and f["atr15_pct"] else None
    )
    b5 = _resample(pre, 5)
    streak = 0
    for _, o, _, _, c, _ in reversed(b5):
        if c > o:
            streak += 1
        else:
            break
    f["green_streak_5m"] = streak
    last = b5[-1] if b5 else None
    if last:
        _, o, h, lo, c, _ = last
        span = h - lo
        f["upper_wick_5m"] = (h - max(o, c)) / span if span > 0 else 0.0
        f["candle_ext_5m"] = (span / c * 100) / f["atr15_pct"] if f["atr15_pct"] else None
    l15 = b15[-1] if b15 else None
    if l15:
        _, o, h, lo, c, _ = l15
        span = h - lo
        f["upper_wick_15m"] = (h - max(o, c)) / span if span > 0 else 0.0
    vol30 = sum(vols[-30:])
    hourly = [sum(vols[k : k + 30]) for k in range(0, len(vols) - 30, 30)]
    if len(hourly) > 5:
        mu = sum(hourly) / len(hourly)
        sd = math.sqrt(sum((x - mu) ** 2 for x in hourly) / len(hourly))
        f["volz_30m"] = (vol30 - mu) / sd if sd > 0 else None
    rets = [(closes[k] / closes[k - 60] - 1) * 100 for k in range(60, len(closes), 15)]
    if len(rets) > 10 and f["ret_60m"] is not None:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / len(rets))
        f["ret_60m_z"] = (f["ret_60m"] - mu) / sd if sd > 0 else None
    f["rsi_30m"] = _rsi([b[4] for b in _resample(pre, 30)])
    f["rsi_60m"] = _rsi([b[4] for b in _resample(pre, 60)])
    f["rsi_5m"] = _rsi([b[4] for b in b5])
    # Follow-through since the move: highest high of the last 60 min vs now.
    f["dist_hi_60m"] = (price / max(highs[-60:]) - 1) * 100
    f.update(market.state(t_entry, symbol, f.get("ret_60m")))
    f["hour_utc"] = t_entry.hour
    return f


class Market:
    """BTC/ETH regime and breadth from the 5m large-cap universe."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.symbols = paths.universe()
        self.series = {
            s: paths.bars(
                s, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC), "k5m"
            )
            for s in self.symbols
        }
        self.index = {s: [b[0] for b in rows] for s, rows in self.series.items()}

    def _close_before(self, symbol: str, t: datetime) -> float | None:
        ts = self.index[symbol]
        # a 5m bar opening at T closes at T+5m: usable only if T+5m <= t
        i = bisect_left(ts, _ms(t) - 300_000 + 1) - 1
        return self.series[symbol][i][4] if i >= 0 else None

    def _ret(self, symbol: str, t: datetime, minutes: int) -> float | None:
        a = self._close_before(symbol, t - timedelta(minutes=minutes))
        b = self._close_before(symbol, t)
        return (b / a - 1) * 100 if a and b else None

    def state(self, t: datetime, symbol: str, coin_ret_60m: float | None) -> dict:
        out: dict[str, float | None] = {}
        for s, name in (("BTC-USDT", "btc"), ("ETH-USDT", "eth")):
            for m in (60, 240):
                out[f"{name}_ret_{m}m"] = self._ret(s, t, m) if s in self.index else None
        r1 = [x for x in (self._ret(s, t, 60) for s in self.symbols) if x is not None]
        r4 = [x for x in (self._ret(s, t, 240) for s in self.symbols) if x is not None]
        out["breadth_60m"] = sum(x > 0 for x in r1) / len(r1) if r1 else None
        out["breadth_240m"] = sum(x > 0 for x in r4) / len(r4) if r4 else None
        med = sorted(r1)[len(r1) // 2] if r1 else None
        out["mkt_ret_60m"] = med
        out["coin_vs_mkt_60m"] = (
            (coin_ret_60m - med) if coin_ret_60m is not None and med is not None else None
        )
        return out


# ---------------------------------------------------------------- exit rules


@dataclass(frozen=True)
class Rule:
    """A deterministic exit model. All levels are in % of entry price.

    pp_at/pp_lock: one-shot profit protection (current LIVE: +1 % -> 0).
    locks: (mfe_at, lock) pairs - once MFE >= mfe_at the stop is raised to
        entry + lock (a step ladder; A/B/J families).
    trail_frac/trail_at: once MFE >= trail_at, stop = entry + frac * MFE (D).
    trail_dist/trail_dist_at: once MFE >= at, stop = peak - dist % (C/F);
        dist may be given in units of ATR15 (trail_atr) or of SL distance
        (trail_sl_frac) - resolved per trade.
    near_tp/near_tp_frac: once MFE >= near_tp * TP distance, lock frac * MFE (H).
    retrace/retrace_at: close-based exit at next open once the CLOSE has given
        back `retrace` of the peak profit, after MFE >= retrace_at (E).
    time_lock_after_h/time_lock_frac: after that many hours, lock frac * MFE
        if MFE >= 1 % (G).
    tp_scale: target distance multiplier (K). hold_h: max hold.
    """

    name: str
    family: str
    pp_at: float | None = 1.0
    pp_lock: float = 0.0
    locks: tuple[tuple[float, float], ...] = ()
    trail_frac: float | None = None
    trail_at: float = 0.0
    trail_atr: float | None = None
    trail_sl_frac: float | None = None
    trail_dist_at: float = 0.0
    trail_dist_at_tp_frac: float | None = None
    near_tp: float | None = None
    near_tp_frac: float = 0.5
    retrace: float | None = None
    retrace_at: float = 0.0
    time_lock_after_h: float | None = None
    time_lock_frac: float = 0.5
    tp_scale: float = 1.0
    hold_h: float = 6.0


def _step(mfe: float, start: float, gap: float) -> tuple[tuple[float, float], ...]:
    return tuple((float(k), float(k) - gap) for k in range(int(start), 21) if k - gap >= 0)


RULES: list[Rule] = [
    Rule("BASE", "baseline"),
    Rule("NO_PP", "baseline", pp_at=None),
    Rule("BE_FEE", "fee-covering BE", pp_lock=0.2),
    Rule("A_2_1", "A fixed lock", locks=((2.0, 1.0),)),
    Rule("A_3_1.5", "A fixed lock", locks=((3.0, 1.5),)),
    Rule("A_3_2", "A fixed lock", locks=((3.0, 2.0),)),
    Rule("A_4_3", "A fixed lock", locks=((4.0, 3.0),)),
    Rule("B_STEP_gap1", "B stepped lock", locks=_step(0, 2, 1.0)),
    Rule("B_STEP_gap1.5", "B stepped lock", locks=_step(0, 2, 1.5)),
    Rule("C_ATR3_at1", "C ATR trail", trail_atr=3.0, trail_dist_at=1.0),
    Rule("C_ATR3_at2", "C ATR trail", trail_atr=3.0, trail_dist_at=2.0),
    Rule("C_ATR5_at2", "C ATR trail", trail_atr=5.0, trail_dist_at=2.0),
    Rule("D_MFE0.5_at2", "D MFE trail", trail_frac=0.5, trail_at=2.0),
    Rule("D_MFE0.5_at3", "D MFE trail", trail_frac=0.5, trail_at=3.0),
    Rule("D_MFE0.7_at2", "D MFE trail", trail_frac=0.7, trail_at=2.0),
    Rule("D_MFE0.7_at3", "D MFE trail", trail_frac=0.7, trail_at=3.0),
    Rule("E_RETR0.4_at2", "E peak retrace", retrace=0.4, retrace_at=2.0),
    Rule("E_RETR0.6_at2", "E peak retrace", retrace=0.6, retrace_at=2.0),
    Rule("E_RETR0.5_at3", "E peak retrace", retrace=0.5, retrace_at=3.0),
    Rule("F_VOL0.3", "F vol-adaptive trail", trail_sl_frac=0.3, trail_dist_at_tp_frac=0.5),
    Rule("F_VOL0.5", "F vol-adaptive trail", trail_sl_frac=0.5, trail_dist_at_tp_frac=0.5),
    Rule("G_TIME2h", "G time-aware", time_lock_after_h=2.0, time_lock_frac=0.5),
    Rule("G_HOLD3h", "G time-aware", hold_h=3.0),
    Rule("H_NEARTP0.7", "H near-target lock", near_tp=0.7, near_tp_frac=0.6),
    Rule("H_NEARTP0.8", "H near-target lock", near_tp=0.8, near_tp_frac=0.7),
    Rule("J_2_1", "J PP + late lock", locks=((2.0, 1.0),), pp_lock=0.2),
    Rule("J_3_1.5", "J PP + late lock", locks=((3.0, 1.5),), pp_lock=0.2),
    Rule("J_4_2", "J PP + late lock", locks=((4.0, 2.0),), pp_lock=0.2),
    Rule("K_TP0.8", "K closer target", tp_scale=0.8),
    Rule("K_TP0.9", "K closer target", tp_scale=0.9),
]


@dataclass
class Outcome:
    pnl: float  # USDT on NOTIONAL, after all costs
    gross_pct: float
    reason: str
    minutes: float
    mfe_pct: float
    mae_pct: float
    exit_price: float


def simulate(
    rule: Rule,
    entry: float,
    sl: float,
    tp: float,
    bars: list[tuple],
    atr15_pct: float | None = None,
    events: tuple[tuple[int, str, float], ...] = (),
) -> Outcome | None:
    """Replay one LONG trade on 1m bars starting with the first bar AFTER the
    entry minute. Returns None when there is no path.

    events: discretionary decisions (decided_at_ms, kind, value) - "EXIT"
    closes at the open of the first bar starting at/after the decision,
    "STOP" raises the stop to `value` from that bar on (never lowered, never
    above that bar's open). A decision never sees the bar it acts in."""
    if not bars or entry <= 0:
        return None
    sl_pct = (sl / entry - 1) * 100
    tp_dist = (tp / entry - 1) * 100 * rule.tp_scale
    tp_px = entry * (1 + tp_dist / 100)
    stop = sl
    peak = entry
    trough = entry
    pending_market = False
    t0 = bars[0][0]
    limit_ms = rule.hold_h * 3_600_000
    reason, exit_px, slip, end_t = "TIME", bars[-1][4], MARKET_SLIP, bars[-1][0]
    pending = sorted(events)
    for t, o, h, lo, c, _v in bars:
        while pending and pending[0][0] <= t:
            _, kind, value = pending.pop(0)
            if kind == "EXIT":
                pending_market = True
            elif kind == "STOP" and value > stop:
                stop = min(value, o * 0.9999)
        if t - t0 >= limit_ms:
            reason, exit_px, slip, end_t = "TIME", o, MARKET_SLIP, t
            break
        if pending_market:
            reason = "DISCRETIONARY_EXIT" if events else "RETRACE"
            exit_px, slip, end_t = o, MARKET_SLIP, t
            break
        if lo <= stop:
            exit_px = min(o, stop)
            reason = "SL" if stop <= sl else ("BE" if stop <= entry * 1.0025 else "LOCK")
            slip, end_t = STOP_SLIP, t
            trough = min(trough, lo)
            break
        if h >= tp_px:
            reason, exit_px, slip, end_t = "TP", max(o, tp_px), 0.0, t
            peak = max(peak, h)
            break
        peak = max(peak, h)
        trough = min(trough, lo)
        mfe = (peak / entry - 1) * 100
        new = stop
        if rule.pp_at is not None and mfe >= rule.pp_at:
            new = max(new, entry * (1 + rule.pp_lock / 100))
        for at, lock in rule.locks:
            if mfe >= at:
                new = max(new, entry * (1 + lock / 100))
        if rule.trail_frac is not None and mfe >= rule.trail_at:
            new = max(new, entry * (1 + rule.trail_frac * mfe / 100))
        dist = None
        if rule.trail_atr is not None and atr15_pct:
            dist = rule.trail_atr * atr15_pct
        if rule.trail_sl_frac is not None:
            dist = rule.trail_sl_frac * abs(sl_pct)
        if dist is not None:
            at = rule.trail_dist_at
            if rule.trail_dist_at_tp_frac is not None:
                at = rule.trail_dist_at_tp_frac * tp_dist
            if mfe >= at:
                new = max(new, peak * (1 - dist / 100))
        if rule.near_tp is not None and mfe >= rule.near_tp * tp_dist:
            new = max(new, entry * (1 + rule.near_tp_frac * mfe / 100))
        if (
            rule.time_lock_after_h is not None
            and t - t0 >= rule.time_lock_after_h * 3_600_000
            and mfe >= 1.0
        ):
            new = max(new, entry * (1 + rule.time_lock_frac * mfe / 100))
        stop = min(max(stop, new), c * 0.9999) if new > stop else stop
        if rule.retrace is not None and mfe >= rule.retrace_at:
            if (c / entry - 1) * 100 <= (1 - rule.retrace) * mfe:
                pending_market = True
    gross = exit_px / entry - 1
    pnl = NOTIONAL * gross - NOTIONAL * FEE - NOTIONAL * (1 + gross) * FEE - NOTIONAL * slip
    minutes = (end_t - t0) / 60_000
    return Outcome(
        pnl,
        gross * 100,
        reason,
        minutes,
        (peak / entry - 1) * 100,
        (trough / entry - 1) * 100,
        exit_px,
    )


# ---------------------------------------------------------------- populations


def _gate_times(bot: sqlite3.Connection) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for cid, at in bot.execute(
        "SELECT aggregate_id, occurred_at FROM events WHERE event_type='CANDIDATE_TRANSITIONED'"
        ' AND payload LIKE \'%"to": "CONFIRMED"%\''
    ):
        out[cid] = datetime.fromisoformat(at)
    return out


def _assessments(bot: sqlite3.Connection, cid: str) -> dict[str, dict]:
    out = {}
    for name, payload in bot.execute(
        "SELECT field_name, payload FROM assessments WHERE candidate_id=?", (cid,)
    ):
        try:
            out[name] = json.loads(payload)
        except ValueError:
            continue
    return out


def _forecast_edge(assess: dict) -> float | None:
    probs = (assess.get("forecast") or {}).get("scenario_probabilities") or {}
    bull = sum(v for k, v in probs.items() if "bull" in k)
    bear = sum(v for k, v in probs.items() if "bear" in k)
    return bull - bear if probs else None


def _evidence(record: str) -> dict:
    ev = json.loads(record)
    sec = ev.get("secondary_timeframe_evidence") or {}
    return {
        "triggers": ev.get("trigger_reasons") or [],
        "ev_chg_30m": (ev.get("price_volatility_evidence") or {}).get("value"),
        "ev_rsi_30m": (ev.get("momentum_breakout_evidence") or {}).get("value"),
        "ev_volz_30m": (ev.get("volume_evidence") or {}).get("value"),
        "ev_funding": (ev.get("funding_oi_evidence") or {}).get("value"),
        "ev_rsi_1h": (sec.get("momentum_breakout_evidence") or {}).get("value"),
        "ev_chg_1h": (sec.get("price_volatility_evidence") or {}).get("value"),
        "candidate_score": ev.get("candidate_score"),
    }


def load_trades(bot: sqlite3.Connection, manual_live: dict[str, dict]) -> list[dict]:
    gate = _gate_times(bot)
    live = {
        r[0]: r
        for r in bot.execute(
            "SELECT position_id, phase, exchange_fill_entry, entry_filled_at, exchange_fill_exit,"
            " exit_classification, exchange_realized_pnl_usdt, realized_fees_usdt, exit_filled_at,"
            " notional_usdt FROM live_executions WHERE phase IN ('ACTIVE','CLOSED')"
        )
    }
    out = []
    for (
        pid,
        cid,
        symbol,
        status,
        fill,
        sl,
        tp,
        _size,
        opened,
        exit_px,
        reason,
        _fees,
        closed,
    ) in bot.execute(
        "SELECT position_id, candidate_id, instrument, status, simulated_fill_entry, stop_loss,"
        " target, size, opened_at, simulated_fill_exit, exit_reason, fees, closed_at"
        " FROM positions"
    ):
        row = {
            "position_id": pid,
            "candidate_id": cid,
            "symbol": symbol,
            "sl": float(sl),
            "tp": float(tp),
            "paper_status": status,
            "paper_exit_reason": reason,
        }
        lv = live.get(pid)
        filled_at = (lv[3] if lv else None) or manual_live.get(pid, {}).get("entry_at")
        if lv and lv[2] and filled_at:
            row.update(is_live=True, entry=float(lv[2]), t_entry=datetime.fromisoformat(filled_at))
            scale = NOTIONAL / float(lv[9] or NOTIONAL)  # early LIVE ran 100 notional
            row["live_notional"] = float(lv[9] or NOTIONAL)
            if lv[1] == "CLOSED" and lv[6] is not None:
                row["actual_pnl"] = (float(lv[6]) - float(lv[7] or 0)) * scale
                row["actual_pnl_usdt_real"] = float(lv[6]) - float(lv[7] or 0)
                # every bot order carries a deterministic "lv..." client id; a
                # MARKET close without one (EXTERNAL_CLOSE) is a manual close -
                # verified against the exchange order history 2026-10-01
                row["actual_exit_class"] = "MANUAL_CLOSE" if lv[5] == "EXTERNAL_CLOSE" else lv[5]
                row["actual_exit_at"] = lv[8]
            elif pid in manual_live:
                m = manual_live[pid]
                row["actual_pnl"] = (m["pnl"] - m["fees"]) * scale
                row["actual_pnl_usdt_real"] = m["pnl"] - m["fees"]
                row["actual_exit_class"] = m["exit_class"]
                row["actual_exit_at"] = m["exit_at"]
        else:
            t_gate = gate.get(cid) or datetime.fromisoformat(opened)
            row.update(is_live=False, entry=float(fill), t_entry=t_gate)
            if status == "CLOSED" and exit_px is not None:
                gross = float(exit_px) / float(fill) - 1
                row["actual_pnl"] = NOTIONAL * gross - NOTIONAL * 2 * FEE
                row["actual_exit_class"] = f"PAPER_{reason}"
                row["actual_exit_at"] = closed
        ev_row = bot.execute(
            "SELECT evidence_record, created_at FROM candidates WHERE candidate_id=?", (cid,)
        ).fetchone()
        if ev_row:
            row.update(_evidence(ev_row[0]))
            row["t_signal"] = datetime.fromisoformat(ev_row[1])
        assess = _assessments(bot, cid)
        row["forecast_edge"] = _forecast_edge(assess)
        out.append(row)
    return out


def load_signals(bot: sqlite3.Connection, paths: Paths) -> list[dict]:
    rows = bot.execute(
        "SELECT candidate_id, instrument, status, evidence_record, created_at FROM candidates"
        " ORDER BY created_at"
    ).fetchall()
    last_taken: dict[str, datetime] = {}
    out = []
    for cid, symbol, status, record, created in rows:
        t_sig = datetime.fromisoformat(created)
        prev = last_taken.get(symbol)
        if prev is not None and t_sig - prev < timedelta(hours=6):
            continue
        last_taken[symbol] = t_sig
        t_entry = t_sig + SIGNAL_LATENCY
        t_entry = t_entry.replace(second=0, microsecond=0)
        bar = paths.bars(symbol, t_entry - timedelta(minutes=1), t_entry)
        if not bar:
            continue
        entry = bar[-1][4]
        assess = _assessments(bot, cid)
        risk = assess.get("risk") or {}
        try:
            sl = float(risk.get("suggested_stop_loss"))
            tp = float(risk.get("suggested_target"))
            own = 0.005 < (entry - sl) / entry < 0.25 and 0.005 < (tp - entry) / entry < 0.4
        except (TypeError, ValueError):
            own = False
        if not own:
            sl = entry * (1 - MEDIAN_SL_PCT / 100)
            tp = entry * (1 + MEDIAN_TP_PCT / 100)
        row = {
            "candidate_id": cid,
            "symbol": symbol,
            "status": status,
            "t_signal": t_sig,
            "t_entry": t_entry,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "own_brackets": own,
            "forecast_edge": _forecast_edge(assess),
        }
        row.update(_evidence(record))
        out.append(row)
    return out
