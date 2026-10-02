"""The user's original bot (latest version, GODFATHER era) over the last 30 days,
replayed exactly as LIVE would have traded it - WITH and WITHOUT PP1.

Window: 2026-09-02 00:00 .. 2026-10-02 00:00 UTC (30 days).

Entries = the bot's OWN decisions: every position it opened (PAPER or LIVE)
in the window, with its own entry time / price, stop-loss and target
(trade_review.load_trades: LIVE fill when the position went LIVE, else the
gate time and the PAPER fill). Nothing about the entries is invented.

LIVE rules applied to every entry, as the bot was configured:
  * 1000 USDT notional per position (100 margin x 10), LONG only
  * at most 4 open positions, at most 1 per symbol, first come first served
    (an entry while 4 are open, or while the symbol is open, is skipped)
  * exchange SL / TP from the decision, 6 h time limit
  * PP1 ON  = +1.0 % unrealized -> stop moved to the entry price (break-even)
    PP1 OFF = no stop move
Exit replay: trade_review.simulate on real BingX 1m candles - stop checked
before target inside a bar, a stop set at a close applies from the next bar,
0.05 % fee per side, 0.10 % slippage on stop exits, 0.03 % on time exits.

Coverage: the bot was NOT running on every day (crashes, PC sleep, stops).
Days without decisions are reported; a 30-day ESTIMATE scales the per-active-
day result and is labelled as an estimate, never mixed with the replay.

    python -m crypto_trading.entry_research.last30_bot
"""

from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from crypto_trading.entry_research.trade_review import BOT_DB, RULES, Paths, load_trades, simulate
from crypto_trading.entry_research.trade_review_run import ceil_minute

START = datetime(2026, 9, 2, tzinfo=UTC)
END = datetime(2026, 10, 2, tzinfo=UTC)
MAX_OPEN = 4


def replay(rows: list[dict], rule_name: str) -> list[dict]:
    rule = next(r for r in RULES if r.name == rule_name)
    open_until: dict[str, datetime] = {}
    taken = []
    for row in sorted(rows, key=lambda r: r["t_entry"]):
        t = row["t_entry"]
        open_until = {s: e for s, e in open_until.items() if e > t}
        if len(open_until) >= MAX_OPEN or row["symbol"] in open_until:
            continue
        out = simulate(rule, row["entry"], row["sl"], row["tp"], row["bars"], None)
        if out is None:
            continue
        exit_t = t + timedelta(minutes=out.minutes + 1)
        open_until[row["symbol"]] = exit_t
        taken.append({"symbol": row["symbol"], "t": t, "exit_t": exit_t, "pnl": out.pnl, "reason": out.reason,
                      "mfe": out.mfe_pct, "is_live": row["is_live"]})
    return taken


def summary(trades: list[dict], label: str, active_days: int) -> None:
    pnls = [x["pnl"] for x in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_day = defaultdict(float)
    by_week = defaultdict(float)
    for x in trades:
        by_day[x["exit_t"].date()] += x["pnl"]
        monday = x["exit_t"].date() - timedelta(days=x["exit_t"].weekday())
        by_week[monday] += x["pnl"]
    total = sum(pnls)
    print(f"\n=== {label} ===")
    print(f"trades {len(pnls)}  total {total:+.1f} USDT  per trade {total / len(pnls):+.2f}")
    print(f"winning trades {len(wins)} ({100 * len(wins) / len(pnls):.0f} %)  sum of wins {sum(wins):+.1f}  "
          f"avg win {st.mean(wins) if wins else 0:+.2f}")
    print(f"losing trades  {len(losses)} ({100 * len(losses) / len(pnls):.0f} %)  sum of losses {sum(losses):+.1f}  "
          f"avg loss {st.mean(losses) if losses else 0:+.2f}")
    print(f"profit factor {sum(wins) / -sum(losses):.2f}  largest win {max(pnls):+.1f}  largest loss {min(pnls):+.1f}")
    days = sorted(by_day)
    print(f"days with closed trades {len(days)}: positive {sum(by_day[d] > 0 for d in days)}  "
          f"negative {sum(by_day[d] <= 0 for d in days)}  best day {max(by_day.values()):+.1f}  worst day {min(by_day.values()):+.1f}")
    reasons = defaultdict(lambda: [0, 0.0])
    for x in trades:
        reasons[x["reason"]][0] += 1
        reasons[x["reason"]][1] += x["pnl"]
    print("exits: " + "  ".join(f"{k} {v[0]} ({v[1]:+.1f})" for k, v in sorted(reasons.items())))
    bal, low = 1000.0, 1000.0
    for x in sorted(trades, key=lambda x: x["exit_t"]):
        bal += x["pnl"]
        low = min(low, bal)
    print(f"balance from 1000: end {bal:.1f}, lowest {low:.1f}")
    print("week (Mon)    net")
    for w in sorted(by_week):
        print(f"  {w}  {by_week[w]:+8.1f}")
    print(f"ESTIMATE if it had run all 30 days (per active day x 30): {total / active_days * 30:+.1f} USDT "
          f"(active days {active_days})")


def main() -> None:
    bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    paths = Paths()
    rows = [r for r in load_trades(bot, {}) if START <= r["t_entry"] < END]
    with_path = []
    for r in rows:
        start = ceil_minute(r["t_entry"])
        bars = paths.bars(r["symbol"], start, start + timedelta(hours=6, minutes=2))
        if len(bars) >= 30:
            r["bars"] = bars
            with_path.append(r)
    active = sorted({r["t_entry"].date() for r in rows})
    all_days = [(START + timedelta(days=i)).date() for i in range(30)]
    print(f"bot decisions in window: {len(rows)} (with 1m price path: {len(with_path)}), "
          f"LIVE among them: {sum(r['is_live'] for r in rows)}")
    print(f"days the bot made decisions: {len(active)} of 30; days without: "
          + ", ".join(d.strftime('%d/%m') for d in all_days if d not in active))
    for rule, label in (("BASE", "WITH PP1 (+1 % -> break-even)"), ("NO_PP", "WITHOUT PP1")):
        summary(replay(with_path, rule), label, len(active))
    real = [r for r in rows if r["is_live"] and "actual_pnl_usdt_real" in r]
    print(f"\nfor reference - what REALLY happened on LIVE in the window: {len(real)} closed LIVE trades, "
          f"{sum(r['actual_pnl_usdt_real'] for r in real):+.1f} USDT (caps, manual closes and outages included)")


if __name__ == "__main__":
    main()
