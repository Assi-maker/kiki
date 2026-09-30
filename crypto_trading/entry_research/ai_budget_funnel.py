"""AI-budget funnel per period (2026-09-30) - read-only against the bot DB.

    python -m crypto_trading.entry_research.ai_budget_funnel

Where the daily AI budget (500 calls / 10 USD) goes: candidates -> AI
analyses -> Gate -> Safety Kernel -> LIVE, per period (before P0, P0 with
% caps, caps observe-only), and the AI cost per real LIVE position.
Research only; nothing in the bot imports this module."""

import glob
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime

DB = "file:data/crypto_trading.db?mode=ro"
PERIODS = [  # (name, start, end) UTC ISO
    ("pre_P0", "2026-09-26T17:27", "2026-09-28T11:15"),
    ("P0_caps", "2026-09-28T22:15", "2026-09-30T08:32"),
    ("caps_off", "2026-09-30T08:32", "2099"),
]
CHAIN = {"news_sentiment", "technical", "bull_thesis", "forecast", "risk", "bear_adversarial", "qa"}
c = sqlite3.connect(DB, uri=True)


def period(ts):
    for n, a, b in PERIODS:
        if a <= ts < b:
            return n
    return None


def hours(n):
    a, b = next((a, b) for m, a, b in PERIODS if m == n)
    b = min(b, datetime.now(UTC).replace(tzinfo=None).isoformat())
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


out = defaultdict(Counter)
cost = defaultdict(Counter)
cand_period = {}
for cid, created in c.execute("SELECT candidate_id, created_at FROM candidates"):
    p = period(created)
    if p:
        cand_period[cid] = p
        out[p]["1_candidates"] += 1

for occ, payload in c.execute(
    "SELECT occurred_at, payload FROM events WHERE event_type = 'CANDIDATE_TRANSITIONED'"
):
    p = period(occ)
    if not p:
        continue
    d = json.loads(payload)
    to = d.get("to")
    if to == "UNDER_AI_ANALYSIS":
        out[p]["2_ai_analysed"] += 1
    elif to == "BUDGET_LIMITED":
        out[p][f"budget_limited:{d.get('reason')}"] += 1
    elif to in ("CONFIRMED", "NO_TRADE", "REJECTED"):
        out[p][f"3_gate:{to}"] += 1

chain_cost_by_cand = Counter()
for occ, agg, payload in c.execute(
    "SELECT occurred_at, aggregate_id, payload FROM events WHERE event_type='AI_CALL_MADE'"
):
    p = period(occ)
    if not p:
        continue
    d = json.loads(payload)
    role = d.get("role")
    grp = "chain" if role in CHAIN else role
    out[p][f"ai_calls:{grp}"] += 1
    cost[p][grp] += float(d.get("cost_usd") or 0)
    if role in CHAIN:
        chain_cost_by_cand[agg] += float(d.get("cost_usd") or 0)

# gate CONFIRMED -> kernel -> LIVE
pos_by_cand = {
    cid: pid for pid, cid in c.execute("SELECT position_id, candidate_id FROM positions")
}
kernel = {
    pid: (a, json.loads(dj))
    for pid, _, a, dj in c.execute(
        "SELECT position_id, decided_at, action, detail_json FROM safety_kernel_decisions"
    )
}
live = {
    r[0]: r[1:]
    for r in c.execute(
        "SELECT position_id, phase, last_error, exchange_fill_entry, notional_usdt"
        " FROM live_executions"
    )
}
wasted = defaultdict(float)
for cid, ev in c.execute(
    "SELECT candidate_id, evaluated_at FROM gate_evaluations WHERE outcome='CONFIRMED'"
):
    p = period(ev)
    if not p:
        continue
    pid = pos_by_cand.get(cid)
    le = live.get(pid)
    k = kernel.get(pid)
    if le and le[2]:
        out[p]["5_LIVE_filled"] += 1
        continue
    if k and k[0] != "APPROVE":
        why = "kernel_reject:" + "+".join(sorted(k[1].get("reasons") or ["?"]))
    elif le and le[0] == "SKIPPED":
        why = "live_skipped:" + str(le[1]).split(":")[-1][:40]
    elif pid is None:
        why = "no_paper_position"
    else:
        why = "never_claimed_by_live(stale/capacity/symbol_open)"
    out[p][f"4_confirmed_not_live:{why}"] += 1
    wasted[p] += chain_cost_by_cand.get(cid, 0.0)

# pre-AI shadow: candidates structurally infeasible under the (then) caps
for at, feasible in c.execute("SELECT assessed_at, feasible FROM pre_ai_feasibility_shadow"):
    p = period(at)
    if p:
        out[p]["pre_ai_shadow_assessed"] += 1
        out[p]["pre_ai_bound_infeasible"] += int(feasible == 0)

for occ, payload in c.execute(
    "SELECT occurred_at, payload FROM events WHERE event_type='DISCOVERY_LIVE_GATE'"
):
    p = period(occ)
    if p:
        out[p]["live_gate:" + json.loads(payload).get("outcome", "?")] += 1

stale = Counter()
for f in glob.glob("logs/crypto_trading.log*"):
    for line in open(f, encoding="utf-8", errors="replace"):
        if '"live_signal_stale_skipped"' in line:
            d = json.loads(line.split(" ", 2)[2])
            p = period(line[:19])
            if p and (d.get("signal_age_seconds") or 1e9) < 6 * 3600:
                stale[p] += 1

for n, _, _ in PERIODS:
    h = hours(n)
    o = out[n]
    live_n = o["5_LIVE_filled"]
    total_cost = sum(cost[n].values())
    print(f"\n=== {n}  ({h:.1f} h) ===")
    for k in sorted(o):
        print(f"  {k:70s} {o[k]:6d}   /day {o[k] * 24 / h:7.1f}")
    print(f"  stale_skips_signal_younger_than_6h (log)                            {stale[n]:6d}")
    print(
        "  AI cost USD:",
        {k: round(v, 2) for k, v in cost[n].items()},
        "total",
        round(total_cost, 2),
        "/day",
        round(total_cost * 24 / h, 2),
    )
    print("  chain cost on CONFIRMED that never went LIVE:", round(wasted[n], 2))
    if live_n:
        print(
            f"  AI cost per LIVE position: chain {cost[n]['chain'] / live_n:.2f}"
            f"  all {total_cost / live_n:.2f}"
        )
