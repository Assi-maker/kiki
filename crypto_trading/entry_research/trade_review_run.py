"""Run the 2026-10-01 trading-system review (research only).

    python -m crypto_trading.entry_research.trade_review_run

Pre-registered approval rules (fixed before any result was computed):

EXIT rule (vs BASE, paired per trade, same entries => 100 % retention):
  1. selection on TRAIN+VALID only: mean paired diff > 0 on TRADES and on
     SIGNALS, Benjamini-Hochberg q < 0.10 across all rules on SIGNALS;
  2. TEST and HOLDOUT each > 0 on SIGNALS, and TEST+HOLDOUT > 0 on TRADES;
  3. day-clustered bootstrap 90 % CI of the OOS (TEST+HOLDOUT) SIGNALS diff
     excludes 0.
ENTRY condition (a "late / exhaustion" state), per hypothesis:
  1. flagged share <= 20 % of SIGNALS (keeps >= 80 % of signals);
  2. flagged minus unflagged mean BASE P/L < 0 in TRAIN+VALID with q < 0.10
     across hypotheses;
  3. the same sign in TEST and in HOLDOUT on SIGNALS, and on TRADES OOS;
  4. removing it raises OOS expectancy AND OOS total P/L.
Anything that fails is reported as a rejected hypothesis, not implemented.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_trading.entry_research.trade_review import (
    BOT_DB,
    RULES,
    Market,
    Paths,
    entry_features,
    load_signals,
    load_trades,
    period_of,
    simulate,
)

OUT = Path("data/entry_research/trade_review_results.json")

# The four LIVE positions that were still ACTIVE in the DB when the bot died
# (2026-10-01 08:10Z). Their closes come from the exchange's own order
# history and income ledger, read 2026-10-01 11:0xZ (see the report).
MANUAL_LIVE = {
    "bc1cf15eab0494ca10283f439e5d033b04e9db0fb81177919e2443c06c54f426": {
        "pnl": -53.0737,
        "fees": 0.496245 + 0.469708,
        "exit_class": "EXCHANGE_STOP_LOSS",
        "entry_at": "2026-10-01T07:23:08+00:00",
        "exit_at": "2026-10-01T08:12:12+00:00",
    },
    "f2056dbcb8abda685b007cac556b77da4fd49cdb72cfbbe207f06c3511c3cbe7": {
        "pnl": -51.0949,
        "fees": 0.499671 + 0.474123,
        "exit_class": "EXCHANGE_STOP_LOSS",
        "entry_at": "2026-10-01T07:10:41+00:00",
        "exit_at": "2026-10-01T08:36:19+00:00",
    },
    "331bc1f3f5544b2214ab04f9e3cc7e4deb583768b2de114fc4275ca88c5823fb": {
        "pnl": -22.8520,
        "fees": 0.498016 + 0.486590,
        "exit_class": "MANUAL_MARKET_CLOSE",
        "entry_at": "2026-10-01T07:14:40+00:00",
        "exit_at": "2026-10-01T09:15:05+00:00",
    },
    "5e7889bfb7d1c7d788393d59d5b38baf81406aa62fd44a88215933e27def7932": {
        "pnl": 3.0227,
        "fees": 0.496381 + 0.497892,
        "exit_class": "MANUAL_MARKET_CLOSE",
        "entry_at": "2026-10-01T07:31:12+00:00",
        "exit_at": "2026-10-01T09:16:54+00:00",
    },
}


def ceil_minute(t: datetime) -> datetime:
    base = t.replace(second=0, microsecond=0)
    return base if base == t else base + timedelta(minutes=1)


def summarize(pnls: list[float], days: float | None = None) -> dict:
    if not pnls:
        return {"n": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gross_loss = -sum(losses)
    return {
        "n": len(pnls),
        "total": sum(pnls),
        "mean": st.mean(pnls),
        "median": st.median(pnls),
        "win_rate": len(wins) / len(pnls),
        "avg_win": st.mean(wins) if wins else 0.0,
        "avg_loss": st.mean(losses) if losses else 0.0,
        "pf": sum(wins) / gross_loss if gross_loss > 0 else None,
        "max_dd": dd,
        "per_day": len(pnls) / days if days else None,
    }


def p_value_mean(xs: list[float]) -> float:
    """Two-sided normal-approximation p for mean != 0."""
    if len(xs) < 3:
        return 1.0
    sd = st.stdev(xs)
    if sd == 0:
        return 1.0 if st.mean(xs) == 0 else 0.0
    z = st.mean(xs) / (sd / math.sqrt(len(xs)))
    return math.erfc(abs(z) / math.sqrt(2))


def bh(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    q, prev = {}, 1.0
    for rank in range(m, 0, -1):
        key, p = items[rank - 1]
        prev = min(prev, p * m / rank)
        q[key] = prev
    return q


def cluster_bootstrap(rows: list[tuple[str, float]], n_boot: int = 2000, seed: int = 7) -> tuple:
    """90 % CI of the mean, resampling whole days."""
    by_day = defaultdict(list)
    for day, x in rows:
        by_day[day].append(x)
    days = list(by_day)
    if len(days) < 3:
        return (None, None)
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [x for _ in days for x in by_day[rng.choice(days)]]
        means.append(st.mean(sample))
    means.sort()
    return (means[int(0.05 * n_boot)], means[int(0.95 * n_boot)])


def attach_paths(rows: list[dict], paths: Paths, market: Market) -> list[dict]:
    kept = []
    for row in rows:
        start = ceil_minute(row["t_entry"])
        bars = paths.bars(row["symbol"], start, start + timedelta(hours=6, minutes=2))
        if len(bars) < 30:
            continue
        row["bars"] = bars
        row["feat"] = entry_features(paths, row["symbol"], row["t_entry"], market)
        row["period"] = period_of(row["t_entry"])
        row["day"] = row["t_entry"].date().isoformat()
        kept.append(row)
    return kept


def run_rules(rows: list[dict]) -> None:
    for row in rows:
        atr = row["feat"].get("atr15_pct")
        row["sim"] = {}
        for rule in RULES:
            out = simulate(rule, row["entry"], row["sl"], row["tp"], row["bars"], atr)
            row["sim"][rule.name] = out


def rule_table(rows: list[dict], label: str) -> dict:
    span = {
        p: max(1.0, len({r["day"] for r in rows if r["period"] == p}))
        for p in ("TRAIN", "VALID", "TEST", "HOLDOUT")
    }
    table = {}
    for rule in RULES:
        entry = {}
        for period in ("TRAIN", "VALID", "TEST", "HOLDOUT", "ALL", "SEL", "OOS"):
            sel = [
                r
                for r in rows
                if r["sim"][rule.name] is not None
                and (
                    period == "ALL"
                    or r["period"] == period
                    or (period == "SEL" and r["period"] in ("TRAIN", "VALID"))
                    or (period == "OOS" and r["period"] in ("TEST", "HOLDOUT"))
                )
            ]
            sel.sort(key=lambda r: r["t_entry"])
            pnls = [r["sim"][rule.name].pnl for r in sel]
            base = [r["sim"]["BASE"].pnl for r in sel]
            diffs = [a - b for a, b in zip(pnls, base, strict=False)]
            s = summarize(pnls, span.get(period))
            s["diff_mean"] = st.mean(diffs) if diffs else None
            s["diff_p"] = p_value_mean(diffs)
            s["winners_cut"] = sum(
                1
                for r in sel
                if r["sim"]["BASE"].reason == "TP" and r["sim"][rule.name].reason != "TP"
            )
            s["losses_avoided"] = sum(
                1 for r in sel if r["sim"]["BASE"].pnl < 0 <= r["sim"][rule.name].pnl
            )
            s["profit_protected"] = sum(max(0.0, d) for d in diffs)
            s["profit_given_up"] = sum(min(0.0, d) for d in diffs)
            if period == "OOS":
                pairs = [(r["day"], d) for r, d in zip(sel, diffs, strict=True)]
                s["diff_ci90"] = cluster_bootstrap(pairs)
            entry[period] = s
        table[rule.name] = entry
    q = bh({name: v["SEL"]["diff_p"] for name, v in table.items() if name != "BASE"})
    for name, v in table.items():
        v["SEL"]["diff_q"] = q.get(name)
    return table


def mfe_ladder(rows: list[dict]) -> list[dict]:
    """For trades whose path reached +x % (under BASE replay rules: the MFE
    of the actual baseline holding window), what happened afterwards."""
    out = []
    for x in (0.5, 1, 2, 3, 4, 4.5, 5, 6):
        reached = [r for r in rows if r["sim"]["NO_PP"].mfe_pct >= x]
        if not reached:
            continue
        nopp = [r["sim"]["NO_PP"] for r in reached]
        base = [r["sim"]["BASE"] for r in reached]
        out.append(
            {
                "mfe_at_least": x,
                "n": len(reached),
                "share_of_all": len(reached) / len(rows),
                "hit_tp_nopp": sum(o.reason == "TP" for o in nopp) / len(reached),
                "ended_loss_nopp": sum(o.pnl < 0 for o in nopp) / len(reached),
                "ended_loss_base": sum(o.pnl < 0 for o in base) / len(reached),
                "mean_pnl_base": st.mean(o.pnl for o in base),
                "mean_pnl_nopp": st.mean(o.pnl for o in nopp),
                "mean_mfe_pct": st.mean(o.mfe_pct for o in nopp),
                "mean_giveback_pct_base": st.mean(o.mfe_pct - o.gross_pct for o in base),
                "tp_dist_median_pct": st.median((r["tp"] / r["entry"] - 1) * 100 for r in reached),
            }
        )
    return out


def near_target_failures(rows: list[dict]) -> dict:
    """The user's scenario: reached >= 80 % of the target distance, missed TP."""
    hits = []
    for r in rows:
        tp_d = (r["tp"] / r["entry"] - 1) * 100
        o = r["sim"]["BASE"]
        if o.reason != "TP" and o.mfe_pct >= 0.8 * tp_d:
            hits.append(
                {
                    "symbol": r["symbol"],
                    "t": r["t_entry"].isoformat(),
                    "tp_dist": tp_d,
                    "mfe": o.mfe_pct,
                    "exit_reason": o.reason,
                    "pnl": o.pnl,
                }
            )
    return {
        "n": len(hits),
        "of": len(rows),
        "total_pnl": sum(h["pnl"] for h in hits),
        "examples": hits[:40],
    }


def groups(rows: list[dict]) -> dict:
    """Entry vs management quality on the BASE replay.

    good entry: the path offered >= max(2 %, 40 % of the target distance)
    before the trade ended; bad entry: never reached +1 % (PP threshold).
    good management: a good entry kept >= 0 P/L, a bad entry lost < 60 % of
    its full stop distance; otherwise bad management."""
    out = defaultdict(lambda: {"n": 0, "pnl": 0.0, "loss": 0.0})
    for r in rows:
        o = r["sim"]["BASE"]
        tp_d = (r["tp"] / r["entry"] - 1) * 100
        sl_d = (1 - r["sl"] / r["entry"]) * 100
        if o.mfe_pct >= max(2.0, 0.4 * tp_d):
            entry = "GOOD"
            mgmt = "GOOD" if o.pnl >= 0 else "BAD"
        elif o.mfe_pct < 1.0:
            entry = "BAD"
            mgmt = "GOOD" if o.gross_pct > -0.6 * sl_d else "BAD"
        else:
            entry = "MIXED"
            mgmt = "GOOD" if o.pnl >= 0 else "BAD"
        key = {
            ("GOOD", "BAD"): "A good entry + bad mgmt",
            ("BAD", "GOOD"): "B bad entry + good mgmt",
            ("BAD", "BAD"): "C bad entry + bad mgmt",
            ("GOOD", "GOOD"): "D good entry + good mgmt",
            ("MIXED", "GOOD"): "M small MFE (1-2 %) + kept",
            ("MIXED", "BAD"): "M small MFE (1-2 %) + lost",
        }[(entry, mgmt)]
        g = out[key]
        g["n"] += 1
        g["pnl"] += o.pnl
        g["loss"] += min(0.0, o.pnl)
    return dict(out)


# ------------------------------------------------------------- entry states

HYPOTHESES = {
    # name: (description, predicate on features)
    "LATE_4H_EXTENDED": (
        "4h return in the top 20 % of signals and within 1 % of the 4h high",
        None,
    ),
    "PRICE_Z_EXTREME": ("1h return z-score vs own 24h distribution > 2.5", None),
    "VWAP24_STRETCH": ("price > 6 % above its 24h VWAP", None),
    "BLOWOFF_WICK": ("upper wick >= 50 % of the last 15m range after a >= 3 % 1h rise", None),
    "ISOLATED_PUMP": ("coin 1h return >= 3 % while the median large cap is <= 0", None),
    "EXHAUSTED_VOLUME": (
        "volume z >= 2.5 but price within 0.3 % of 30m earlier (no continuation)",
        None,
    ),
    "NO_4H_MOMENTUM": ("4h return <= 0.17 % (prior failure hypothesis, re-tested)", None),
    "RSI_HOT_NO_MOVE": ("RSI 30m >= 70 but 1h return < 1 % (RSI artefact / thin move)", None),
}


def flag(name: str, f: dict, q80_ret240: float) -> bool | None:
    def g(k):
        return f.get(k)

    try:
        if name == "LATE_4H_EXTENDED":
            return (
                g("ret_240m") is not None
                and g("ret_240m") >= q80_ret240
                and g("dist_hi_4h") >= -1.0
            )
        if name == "PRICE_Z_EXTREME":
            return g("ret_60m_z") is not None and g("ret_60m_z") > 2.5
        if name == "VWAP24_STRETCH":
            return g("dist_vwap_24h") is not None and g("dist_vwap_24h") > 6.0
        if name == "BLOWOFF_WICK":
            return (
                g("upper_wick_15m") is not None
                and g("ret_60m") is not None
                and g("upper_wick_15m") >= 0.5
                and g("ret_60m") >= 3.0
            )
        if name == "ISOLATED_PUMP":
            return (
                g("ret_60m") is not None
                and g("mkt_ret_60m") is not None
                and g("ret_60m") >= 3.0
                and g("mkt_ret_60m") <= 0.0
            )
        if name == "EXHAUSTED_VOLUME":
            return (
                g("volz_30m") is not None
                and g("ret_30m") is not None
                and g("volz_30m") >= 2.5
                and abs(g("ret_30m")) < 0.3
            )
        if name == "NO_4H_MOMENTUM":
            return g("ret_240m") is not None and g("ret_240m") <= 0.17
        if name == "RSI_HOT_NO_MOVE":
            return (
                g("rsi_30m") is not None
                and g("ret_60m") is not None
                and g("rsi_30m") >= 70
                and g("ret_60m") < 1.0
            )
    except TypeError:
        return None
    return None


def entry_state_tests(signals: list[dict], trades: list[dict]) -> dict:
    rets = sorted(
        r["feat"]["ret_240m"]
        for r in signals
        if r["feat"].get("ret_240m") is not None and r["period"] in ("TRAIN", "VALID")
    )
    q80 = rets[int(0.8 * len(rets))] if rets else 99.0
    result = {"q80_ret240_train": q80}
    pvals = {}
    for name, (desc, _) in HYPOTHESES.items():
        res = {"desc": desc}
        for pop_name, pop in (("SIGNALS", signals), ("TRADES", trades)):
            for period in ("SEL", "TEST", "HOLDOUT", "OOS"):
                sel = [
                    r
                    for r in pop
                    if period == r["period"]
                    or (period == "SEL" and r["period"] in ("TRAIN", "VALID"))
                    or (period == "OOS" and r["period"] in ("TEST", "HOLDOUT"))
                ]
                fl = [r for r in sel if flag(name, r["feat"], q80)]
                un = [r for r in sel if flag(name, r["feat"], q80) is False]
                fp = [r["sim"]["BASE"].pnl for r in fl]
                up = [r["sim"]["BASE"].pnl for r in un]
                allp = fp + up
                d = {
                    "n": len(sel),
                    "flagged": len(fl),
                    "share": len(fl) / len(sel) if sel else None,
                    "flag_mean": st.mean(fp) if fp else None,
                    "unflag_mean": st.mean(up) if up else None,
                    "diff": (st.mean(fp) - st.mean(up)) if fp and up else None,
                    "all_total": sum(allp),
                    "kept_total": sum(up),
                    "all_mean": st.mean(allp) if allp else None,
                    "kept_mean": st.mean(up) if up else None,
                    "flag_mfe": st.mean(r["sim"]["NO_PP"].mfe_pct for r in fl) if fl else None,
                    "flag_tp_rate": (sum(r["sim"]["BASE"].reason == "TP" for r in fl) / len(fl))
                    if fl
                    else None,
                }
                if fp and up and len(fp) > 2 and len(up) > 2:
                    se = math.sqrt(st.variance(fp) / len(fp) + st.variance(up) / len(up))
                    d["p"] = math.erfc(abs(d["diff"] / se) / math.sqrt(2)) if se > 0 else 1.0
                res[f"{pop_name}_{period}"] = d
        pvals[name] = res["SIGNALS_SEL"].get("p", 1.0)
        result[name] = res
    q = bh(pvals)
    for name in HYPOTHESES:
        result[name]["q_sel"] = q[name]
    return result


def feature_deciles(signals: list[dict], keys: list[str]) -> dict:
    """Descriptive: mean BASE P/L per feature quintile, SEL vs OOS."""
    out = {}
    for k in keys:
        vals = [r for r in signals if r["feat"].get(k) is not None]
        if len(vals) < 50:
            continue
        cuts = sorted(r["feat"][k] for r in vals if r["period"] in ("TRAIN", "VALID"))
        if len(cuts) < 25:
            continue
        edges = [cuts[int(len(cuts) * q / 5)] for q in (1, 2, 3, 4)]

        def bucket(x, edges=edges):
            return sum(x > e for e in edges)

        rows = []
        for b in range(5):
            sel = [
                r["sim"]["BASE"].pnl
                for r in vals
                if bucket(r["feat"][k]) == b and r["period"] in ("TRAIN", "VALID")
            ]
            oos = [
                r["sim"]["BASE"].pnl
                for r in vals
                if bucket(r["feat"][k]) == b and r["period"] in ("TEST", "HOLDOUT")
            ]
            rows.append(
                {
                    "q": b + 1,
                    "lo": edges[b - 1] if b else None,
                    "sel_n": len(sel),
                    "sel_mean": st.mean(sel) if sel else None,
                    "oos_n": len(oos),
                    "oos_mean": st.mean(oos) if oos else None,
                }
            )
        out[k] = rows
    return out


def forward_returns(signals: list[dict]) -> dict:
    """Part 7: does price continue in the direction of the pre-signal move?"""
    out = {}
    for trig in ("price_volatility", "momentum_breakout", "volume", "funding_oi", "ANY"):
        sel = [r for r in signals if trig == "ANY" or trig in r["triggers"]]
        for pre in ("UP", "DOWN"):
            sub = [
                r
                for r in sel
                if r["feat"].get("ret_60m") is not None
                and ((r["feat"]["ret_60m"] > 0) == (pre == "UP"))
            ]
            rec = {"n": len(sub)}
            for m in (30, 60, 120, 240):
                vals = []
                for r in sub:
                    bars = r["bars"]
                    if len(bars) > m:
                        vals.append((bars[m - 1][4] / r["entry"] - 1) * 100)
                rec[f"fwd_{m}m_mean"] = st.mean(vals) if vals else None
                rec[f"fwd_{m}m_up_share"] = (sum(v > 0 for v in vals) / len(vals)) if vals else None
            out[f"{trig}_{pre}"] = rec
    return out


def strip(rows: list[dict]) -> list[dict]:
    keep = []
    for r in rows:
        d = {k: v for k, v in r.items() if k not in ("bars", "sim")}
        d["t_entry"] = r["t_entry"].isoformat()
        if isinstance(d.get("t_signal"), datetime):
            d["t_signal"] = d["t_signal"].isoformat()
        d["sim"] = {k: (v.__dict__ if v else None) for k, v in r["sim"].items()}
        keep.append(d)
    return keep


def main() -> None:
    bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    paths = Paths()
    market = Market(paths)
    trades = attach_paths(load_trades(bot, MANUAL_LIVE), paths, market)
    signals = attach_paths(load_signals(bot, paths), paths, market)
    run_rules(trades)
    run_rules(signals)
    live = [t for t in trades if t["is_live"] and t.get("actual_pnl") is not None]
    bot_exits = [
        t for t in live if t["actual_exit_class"] not in ("MANUAL_CLOSE", "MANUAL_MARKET_CLOSE")
    ]
    validation_bot = {
        "n": len(bot_exits),
        "actual_total_1000": sum(t["actual_pnl"] for t in bot_exits),
        "sim_base_total": sum(t["sim"]["BASE"].pnl for t in bot_exits),
        "corr": (
            st.correlation(
                [t["actual_pnl"] for t in bot_exits], [t["sim"]["BASE"].pnl for t in bot_exits]
            )
            if len(bot_exits) > 2
            else None
        ),
    }
    manual = [t for t in live if t not in bot_exits]
    manual_effect = {
        "n": len(manual),
        "actual_total_1000": sum(t["actual_pnl"] for t in manual),
        "sim_base_total": sum(t["sim"]["BASE"].pnl for t in manual),
        "actual_real_usdt": sum(t["actual_pnl_usdt_real"] for t in manual),
    }
    validation = {
        "n": len(live),
        "actual_total": sum(t["actual_pnl"] for t in live),
        "sim_base_total": sum(t["sim"]["BASE"].pnl for t in live),
        "corr": (
            st.correlation([t["actual_pnl"] for t in live], [t["sim"]["BASE"].pnl for t in live])
            if len(live) > 2
            else None
        ),
    }
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "trades": len(trades),
            "live_trades": sum(t["is_live"] for t in trades),
            "signals": len(signals),
            "signals_per_period": {
                p: sum(r["period"] == p for r in signals)
                for p in ("TRAIN", "VALID", "TEST", "HOLDOUT")
            },
            "trades_per_period": {
                p: sum(r["period"] == p for r in trades)
                for p in ("TRAIN", "VALID", "TEST", "HOLDOUT")
            },
        },
        "base_validation_live": validation,
        "base_validation_bot_exits": validation_bot,
        "manual_close_effect": manual_effect,
        "rules_trades": rule_table(trades, "TRADES"),
        "rules_signals": rule_table(signals, "SIGNALS"),
        "rules_live": rule_table(live, "LIVE"),
        "mfe_ladder_trades": mfe_ladder(trades),
        "mfe_ladder_live": mfe_ladder(live),
        "mfe_ladder_signals": mfe_ladder(signals),
        "near_target_trades": near_target_failures(trades),
        "near_target_signals": near_target_failures(signals),
        "groups_trades": groups(trades),
        "groups_live": groups(live),
        "groups_signals": groups(signals),
        "entry_states": entry_state_tests(signals, trades),
        "feature_quintiles": feature_deciles(
            signals,
            [
                "ret_30m",
                "ret_60m",
                "ret_240m",
                "ret_1380m",
                "dist_hi_4h",
                "dist_hi_24h",
                "dist_vwap_24h",
                "dist_vwap_4h",
                "ret_60m_z",
                "accel_30m",
                "atr_expansion",
                "green_streak_5m",
                "upper_wick_15m",
                "volz_30m",
                "rsi_30m",
                "rsi_60m",
                "coin_vs_mkt_60m",
                "breadth_60m",
                "btc_ret_240m",
                "atr15_pct",
                "pos_24h",
                "dist_hi_60m",
            ],
        ),
        "forward_returns": forward_returns(signals),
        "trades": strip(trades),
        "signals": strip(signals),
    }
    OUT.write_text(json.dumps(results, default=str))
    print(
        "trades", len(trades), "signals", len(signals), "validation", validation_bot, manual_effect
    )


if __name__ == "__main__":
    main()
