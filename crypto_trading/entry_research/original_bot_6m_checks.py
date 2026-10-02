"""Supplementary checks for original_bot_6m (descriptive, change nothing in the bot).

1. AI selection with ONE geometry: CONFIRMED vs NO_TRADE vs BUDGET_LIMITED, all
   with the median SL/TP (4.3 % / 5.0 %), PP1 off, BingX 1m; day-clustered CI
   of the mean per group.
2. Resolution / venue: the real bot's September entries replayed (PP1 off) on
   BingX 1m (trade_review) vs on Binance 5m (original_bot_6m.simulate).

    uv run --with numpy python -m crypto_trading.entry_research.original_bot_6m_checks
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta

from crypto_trading.entry_research import original_bot_6m as M
from crypto_trading.entry_research.trade_review import RULES, Paths, load_trades
from crypto_trading.entry_research.trade_review import simulate as tr_sim
from crypto_trading.entry_research.trade_review_run import ceil_minute

NO_PP = next(r for r in RULES if r.name == "NO_PP")


def day_ci(vals_by_day: dict, n: int = 4000, seed: int = 5) -> list[float]:
    days = list(vals_by_day)
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        s = [x for _ in days for x in vals_by_day[rng.choice(days)]]
        if s:
            means.append(st.mean(s))
    means.sort()
    return [means[int(0.025 * len(means))], means[int(0.975 * len(means))]]


def check_ai_selection() -> dict:
    bot = sqlite3.connect(f"file:{M.BOT_DB}?mode=ro", uri=True)
    paths = Paths()
    groups = defaultdict(lambda: defaultdict(list))
    for cid, symbol, status, created in bot.execute(
            "SELECT candidate_id, instrument, status, created_at FROM candidates ORDER BY created_at"):
        t_entry = (datetime.fromisoformat(created) + timedelta(minutes=15)).replace(second=0, microsecond=0)
        bars = paths.bars(symbol, t_entry, t_entry + timedelta(hours=6, minutes=2))
        if len(bars) < 30:
            continue
        entry = bars[0][1] * (1 + M.ENTRY_SLIP)
        out = tr_sim(NO_PP, entry, entry * (1 - 0.043), entry * (1 + 0.05), bars[1:], None)
        if out is not None:
            groups[status][t_entry.date()].append(out.pnl)
    res = {}
    for status, by_day in groups.items():
        vals = [x for v in by_day.values() for x in v]
        res[status] = {"n": len(vals), "mean": st.mean(vals), "win_rate": sum(x > 0 for x in vals) / len(vals),
                       "mean_ci95_day_clustered": day_ci(by_day)}
    return res


def check_resolution() -> dict:
    bot = sqlite3.connect(f"file:{M.BOT_DB}?mode=ro", uri=True)
    paths = Paths()
    syms = {s.name: s for s in M.load_symbols()}
    pairs = []
    for r in load_trades(bot, {}):
        name = r["symbol"].replace("-", "")
        if name not in syms:
            continue
        start = ceil_minute(r["t_entry"])
        bars = paths.bars(r["symbol"], start, start + timedelta(hours=6, minutes=2))
        if len(bars) < 30:
            continue
        one = tr_sim(NO_PP, r["entry"], r["sl"], r["tp"], bars, None)
        i = M.idx(start) + 1
        sl_pct = (1 - r["sl"] / r["entry"]) * 100
        tp_pct = (r["tp"] / r["entry"] - 1) * 100
        five = M.simulate(syms[name], i, sl_pct, tp_pct, pp=False)
        if one is not None and five is not None:
            pairs.append((one.pnl, five["net"], one.reason, five["reason"]))
    agree = sum(a[2] == b for a, *_, b in [(p, p[3]) for p in pairs])
    return {"n": len(pairs), "bingx_1m_total": sum(p[0] for p in pairs), "binance_5m_total": sum(p[1] for p in pairs),
            "bingx_1m_mean": st.mean(p[0] for p in pairs), "binance_5m_mean": st.mean(p[1] for p in pairs),
            "same_exit_reason_share": sum(p[2] == p[3] for p in pairs) / len(pairs)}


def main() -> None:
    out = {"ai_selection_same_geometry": check_ai_selection(), "resolution": check_resolution()}
    print(json.dumps(out, indent=1, default=str))
    M.OUT.with_name("2026-10-02-original-bot-6m-checks.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
