"""P1 (2026-10-01): maker (post-only limit) entry vs today's taker entry.

Research only - nothing here can place an order. Uses the same populations,
1m BingX paths and exit replay as trade_review.py (BASE exit rules: the
position's own SL/TP, PP +1 % -> break-even, 6 h limit).

Decision price P0 = close of the last full minute before the entry time.

TAKER (today): fill at P0 x (1 + TAKER_SLIP) at the decision minute; taker
  fee both sides. TAKER_SLIP = 0.03 %, the measured median of 106 LIVE fills
  against the previous 1m close (2026-10-01).
MAKER(offset, wait): post-only BUY limit at L = P0 x (1 - offset), alive for
  `wait` minutes. Filled only if a later 1m bar trades THROUGH the limit
  (low <= L x (1 - 1 bp)) - queue position is unknown, so merely touching L
  is not counted. Maker fee on the entry, taker fee on the exit (stops / TP /
  time are market orders). The SL/TP stay at their absolute prices. Not
  filled = no trade (a missed opportunity, valued at the taker result).
MAKER_FALLBACK(offset, wait): as MAKER, but an unfilled order is cancelled
  and the trade is taken at market at the end of `wait` (100 % retention).

Fees: taker 0.05 % (measured 0.049 %). The account's maker rate is not
verified (every historical order was a taker); BingX's standard perpetual
maker rate 0.02 % is the base case, 0.04 % a pessimistic sensitivity.

    python -m crypto_trading.entry_research.maker_entry_sim
"""

from __future__ import annotations

import json
import sqlite3
import statistics as st
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from crypto_trading.entry_research.trade_review import (
    BOT_DB,
    FEE,
    NOTIONAL,
    RULES,
    Market,
    Paths,
    load_signals,
    load_trades,
    simulate,
)
from crypto_trading.entry_research.trade_review_run import (
    MANUAL_LIVE,
    attach_paths,
    ceil_minute,
    cluster_bootstrap,
)

OUT = Path("data/entry_research/maker_entry_sim.json")
BASE = next(r for r in RULES if r.name == "BASE")
TAKER_SLIP = 0.0003
TRADE_THROUGH = 0.0001
OFFSETS = (0.0, 0.0002, 0.0005, 0.0010, 0.0020)
WAITS = (1, 3, 5, 15)
MAKER_FEES = (0.0002, 0.0004)
PERIODS = ("TRAIN", "VALID", "TEST", "HOLDOUT")


@dataclass(frozen=True)
class Fill:
    filled: bool
    price: float | None
    minutes: float | None  # from order placement to fill


def maker_fill(bars: list[tuple], limit: float, wait_minutes: int) -> Fill:
    """First bar (opening within `wait_minutes` of placement) whose low trades
    through `limit`. A bar opening below the limit fills at the LIMIT (a
    resting buy fills at its own price, never better)."""
    if not bars:
        return Fill(False, None, None)
    t0 = bars[0][0]
    for t, _o, _h, low, _c, _v in bars:
        if t - t0 >= wait_minutes * 60_000:
            break
        if low <= limit * (1 - TRADE_THROUGH):
            return Fill(True, limit, (t - t0) / 60_000 + 1)
    return Fill(False, None, None)


def _entry_fee_adjust(maker_fee: float) -> float:
    """simulate() charges FEE on the entry; a maker entry pays maker_fee."""
    return NOTIONAL * (FEE - maker_fee)


def evaluate_row(row: dict, decision_price: float, offset: float, wait: int,
                 maker_fee: float) -> dict:
    bars = row["bars"]
    taker_entry = decision_price * (1 + TAKER_SLIP)
    taker = simulate(BASE, taker_entry, row["sl"], row["tp"], bars)
    limit = decision_price * (1 - offset)
    fill = maker_fill(bars, limit, wait)
    out = {"taker": taker.pnl if taker else None, "filled": fill.filled,
           "fill_minutes": fill.minutes}
    if fill.filled:
        # the position exists from the bar AFTER the fill bar
        start = int(fill.minutes)
        fill_bar = bars[start - 1]
        after = bars[start:]
        if fill_bar[3] <= row["sl"]:
            # the fill minute itself traded through the stop: stopped out at once
            # (stop first, same convention as simulate()).
            after = [(fill_bar[0], row["sl"], row["sl"], row["sl"], row["sl"], 0.0)]
        maker = simulate(BASE, fill.price, row["sl"], row["tp"], after) if after else None
        if maker is not None and limit <= row["sl"]:
            maker = None  # a limit at/below the stop is never a valid entry
        out["maker"] = maker.pnl + _entry_fee_adjust(maker_fee) if maker else None
        out["fallback"] = out["maker"]
    else:
        out["maker"] = None
        wait_bars = bars[wait:]
        if wait_bars:
            late_entry = bars[wait - 1][4] * (1 + TAKER_SLIP)
            late = simulate(BASE, late_entry, row["sl"], row["tp"], wait_bars)
            out["fallback"] = late.pnl if late and row["sl"] < late_entry < row["tp"] else None
        else:
            out["fallback"] = None
    return out


def decision_price(paths: Paths, row: dict) -> float | None:
    start = ceil_minute(row["t_entry"])
    prev = paths.bars(row["symbol"], start - timedelta(minutes=2), start)
    return prev[-1][4] if prev else None


def summarize(results: list[dict], rows: list[dict]) -> dict:
    n = len(results)
    filled = [r for r in results if r["filled"]]
    missed = [r for r in results if not r["filled"]]
    taker = [r["taker"] for r in results]
    maker_tot = sum(r["maker"] or 0.0 for r in filled)
    fallback = [r["fallback"] if r["fallback"] is not None else r["taker"] for r in results]
    out = {
        "n": n,
        "fill_rate": len(filled) / n if n else None,
        "median_fill_minutes": st.median(r["fill_minutes"] for r in filled) if filled else None,
        "taker_total": sum(taker), "taker_mean": st.mean(taker) if taker else None,
        "maker_only_total": maker_tot,
        "maker_only_mean_per_signal": maker_tot / n if n else None,
        "maker_only_mean_per_trade": maker_tot / len(filled) if filled else None,
        "missed": len(missed),
        "missed_taker_total": sum(r["taker"] for r in missed),
        "missed_taker_mean": st.mean(r["taker"] for r in missed) if missed else None,
        "filled_taker_mean": st.mean(r["taker"] for r in filled) if filled else None,
        "filled_maker_mean": st.mean(r["maker"] for r in filled if r["maker"] is not None)
        if filled else None,
        "fallback_total": sum(fallback),
        "fallback_mean": st.mean(fallback) if fallback else None,
    }
    # adverse selection: do the trades we get filled on do worse than the ones
    # we miss, measured on the SAME taker outcome?
    if out["filled_taker_mean"] is not None and out["missed_taker_mean"] is not None:
        out["adverse_selection"] = out["filled_taker_mean"] - out["missed_taker_mean"]
    diffs_maker = [((r["maker"] or 0.0) if r["filled"] else 0.0) - r["taker"] for r in results]
    diffs_fb = [f - r["taker"] for f, r in zip(fallback, results, strict=True)]
    out["diff_maker_vs_taker_per_signal"] = st.mean(diffs_maker) if diffs_maker else None
    out["diff_fallback_vs_taker_per_signal"] = st.mean(diffs_fb) if diffs_fb else None
    out["ci90_maker"] = cluster_bootstrap([(row["day"], d) for row, d in zip(rows, diffs_maker,
                                                                             strict=True)])
    out["ci90_fallback"] = cluster_bootstrap([(row["day"], d) for row, d in zip(rows, diffs_fb,
                                                                                strict=True)])
    return out


def run_population(rows: list[dict], paths: Paths) -> dict:
    usable = []
    for row in rows:
        p0 = decision_price(paths, row)
        if p0 and row["sl"] < p0 < row["tp"]:
            usable.append((row, p0))
    table = {}
    for maker_fee in MAKER_FEES:
        for offset in OFFSETS:
            for wait in WAITS:
                key = f"fee{maker_fee * 100:.2f}_off{offset * 100:.2f}_wait{wait}"
                per_period = {}
                for period in (*PERIODS, "ALL"):
                    sel = [(r, p) for r, p in usable if period == "ALL" or r["period"] == period]
                    results = [evaluate_row(r, p, offset, wait, maker_fee) for r, p in sel]
                    per_period[period] = summarize(results, [r for r, _ in sel])
                table[key] = per_period
    return {"n_usable": len(usable), "table": table}


def main() -> None:
    bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    paths = Paths()
    market = Market(paths)
    trades = attach_paths(load_trades(bot, MANUAL_LIVE), paths, market)
    signals = attach_paths(load_signals(bot, paths), paths, market)
    result = {"assumptions": {"taker_fee": FEE, "taker_entry_slip": TAKER_SLIP,
                              "trade_through": TRADE_THROUGH, "maker_fees": MAKER_FEES},
              "TRADES": run_population(trades, paths),
              "SIGNALS": run_population(signals, paths)}
    OUT.write_text(json.dumps(result, default=str))
    print("trades", result["TRADES"]["n_usable"], "signals", result["SIGNALS"]["n_usable"])


if __name__ == "__main__":
    main()
