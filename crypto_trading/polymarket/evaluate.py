"""Evaluate the Polymarket rewards PAPER test against its pre-registered criteria.

Usage: python -m crypto_trading.polymarket.evaluate [--db data/polymarket_paper/paper.db]
Pre-registration: docs/superpowers/reports/2026-10-02-polymarket-rewards-paper-test.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from crypto_trading.polymarket.paper import BUDGET, VARIANTS, Position, _get

PRIMARY = "d2_all"
MIN_COVERAGE = 0.80          # a UTC day with fewer ticks than this share of 1440 is invalid
N_DAYS = 7
GAMMA = "https://gamma-api.polymarket.com/markets?condition_ids="


def day_of(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def resolution(cid):
    """Resolved Yes price (0/1) or None."""
    try:
        g = _get(GAMMA + cid)
    except Exception:  # noqa: BLE001
        return None
    if not g or not g[0].get("closed"):
        return None
    try:
        prices = [float(x) for x in json.loads(g[0].get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return None
    if len(prices) == 2 and prices[0] in (0.0, 1.0):
        return prices[0]
    return None


def valid_days(db):
    cov = defaultdict(int)
    for (ts,) in db.execute("SELECT ts FROM ticks"):
        cov[day_of(ts)] += 1
    first = min(cov) if cov else None
    days = sorted(d for d in cov if d != first)          # the start day is a partial warm-up
    return [d for d in days if cov[d] >= MIN_COVERAGE * 1440][:N_DAYS], cov


def evaluate(db, resolve=True):
    days, cov = valid_days(db)
    if not days:
        return dict(status="NO_DATA", coverage=dict(cov))
    end_day = days[-1]
    mids = defaultdict(list)                               # cid -> [(ts, mid)]
    for ts, cid, mid in db.execute("SELECT ts, cid, mid FROM samples ORDER BY ts"):
        mids[cid].append((ts, mid))
    sizes = {c: n for c, n in db.execute("SELECT cid, n FROM markets")}
    out = {}
    res_cache = {}
    for name, *_ in VARIANTS:
        rewards_day, rewards_mkt = defaultdict(float), defaultdict(float)
        for ts, cid, r in db.execute("SELECT ts, cid, reward FROM samples WHERE variant=?", (name,)):
            if day_of(ts) <= end_day:
                rewards_day[day_of(ts)] += r
                rewards_mkt[cid] += r
        fills = db.execute("SELECT ts, cid, side, price, size, reason FROM fills WHERE variant=? ORDER BY ts",
                           (name,)).fetchall()
        fills = [f for f in fills if day_of(f[0]) <= end_day]

        def mark(cid, until_ts):
            last = None
            for ts, m in mids[cid]:
                if ts > until_ts:
                    break
                last = m
            return last

        def values(until_ts, final=False):
            pos = {}
            for ts, cid, side, price, size, _ in fills:
                if ts > until_ts:
                    break
                p = pos.setdefault(cid, Position(cid=cid, size=size or sizes.get(cid, 0)))
                p.fill_bid(price) if side == "bid" else p.fill_ask(price)
            vals = {}
            for cid, p in pos.items():
                y = None
                if final and resolve and (p.yes > 0 or p.no > 0):
                    if cid not in res_cache:
                        res_cache[cid] = resolution(cid)
                    y = res_cache[cid]
                if y is None:
                    y = mark(cid, until_ts)
                vals[cid] = p.value(y if y is not None else 0.5)
            return vals

        def day_end(d):
            return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400 - 1

        start_before = day_end(days[0]) - 86400
        prev_v = sum(values(start_before).values())
        daily = []
        for d in days:
            v = sum(values(day_end(d)).values())
            daily.append(dict(day=d, rewards=round(rewards_day[d], 2), inventory_pnl=round(v - prev_v, 2),
                              net=round(rewards_day[d] + v - prev_v, 2), ticks=cov[d]))
            prev_v = v
        final_vals = values(day_end(end_day), final=True)
        total_rewards = sum(rewards_day[d] for d in rewards_day)
        inv_total = sum(final_vals.values())
        per_mkt = defaultdict(float)
        for c, r in rewards_mkt.items():
            per_mkt[c] += r
        for c, v in final_vals.items():
            per_mkt[c] += v
        best = max(per_mkt.items(), key=lambda x: x[1]) if per_mkt else (None, 0.0)
        total = total_rewards + inv_total
        reasons = defaultdict(int)
        for f in fills:
            reasons[f[5]] += 1
        crit = dict(
            total_positive=total > 0,
            days_positive=sum(1 for x in daily if x["net"] > 0),
            without_best_market_positive=total - best[1] > 0,
            avg_reward_per_day_ge_1=total_rewards / max(1, len(rewards_day)) >= 1.0,
        )
        complete = len(days) >= N_DAYS
        passed = (complete and crit["total_positive"] and crit["days_positive"] >= 5
                  and crit["without_best_market_positive"] and crit["avg_reward_per_day_ge_1"])
        out[name] = dict(
            primary=name == PRIMARY, budget=BUDGET, days_evaluated=len(days), complete=complete,
            total_net=round(total, 2), total_rewards=round(total_rewards, 2), inventory_pnl=round(inv_total, 2),
            fills=len(fills), fill_reasons=dict(reasons),
            unresolved_marked_at_mid=sum(1 for c in final_vals if res_cache.get(c) is None),
            best_market=best[0], best_market_net=round(best[1], 2), criteria=crit,
            verdict="PASS" if passed else ("FAIL" if complete else "INCOMPLETE"), daily=daily)
    inv = db.execute("SELECT max(max_capital), max(max_net_inv_ratio), sum(trades_missing), count(*) FROM ticks").fetchone()
    return dict(status="OK", primary=PRIMARY, days=days,
                invariants=dict(max_capital=inv[0], capital_ok=(inv[0] or 0) <= BUDGET + 1e-6,
                                max_net_inv_ratio=inv[1], inventory_ok=(inv[1] or 0) <= 1 + 1e-6,
                                trades_missing=inv[2], ticks=inv[3]),
                variants=out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/polymarket_paper/paper.db")
    ap.add_argument("--no-resolve", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res = evaluate(sqlite3.connect(a.db), resolve=not a.no_resolve)
    txt = json.dumps(res, indent=1, ensure_ascii=False)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt)
    print(txt)


if __name__ == "__main__":
    main()
