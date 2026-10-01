"""Part 6 of the 2026-10-01 review: GODFATHER as position MANAGER, replayed.

For every bot trade, GODFATHER is asked at the moments the user's problem is
about - the first time the trade's open profit reaches +2 %, +3 % and +4 %,
the first time the price has given back >= 40 % of a >= +2 % peak, and once
at 60 minutes if the trade is then <= -2 % - with ONLY what was known at that
minute (entry, SL/TP, current stop, MFE/MAE so far, the last hour's 5m path,
volatility, BTC/market state, the original signal and AI forecast).

Its answer (HOLD / PROTECT / TIGHTEN_SL / EXIT + an optional stop level) is
then applied in the same 1m replay as every other exit model, through the
same safety rules the LIVE system has: a stop is only ever RAISED, never
above the current price, the last SL is never removed, no size change.

Research only: writes `data/entry_research/godfather_manager_replay.json`.

    python -m crypto_trading.entry_research.godfather_manager_replay [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics as st
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.agents.runner import RealClaudeRunner
from crypto_trading.entry_research.trade_review import (
    BOT_DB,
    RULES,
    Market,
    Paths,
    _resample,
    _rsi,
    load_trades,
    simulate,
)
from crypto_trading.entry_research.trade_review_run import MANUAL_LIVE, attach_paths
from crypto_trading.schemas.assessments import AssessmentBase

OUT = Path("data/entry_research/godfather_manager_replay.json")
BASE = next(r for r in RULES if r.name == "BASE")
COST_CAP_USD = 12.0

SYSTEM_PROMPT = """Du är GODFATHER, det intelligenta besluts- och managerlagret ovanpå
Position Guardian i crypto_trading. Du förvaltar EN redan öppen LONG-position
(1000 USDT notional, 10x). Guardian/Safety Kernel är slutlig auktoritet.

Ditt mål är netto-lönsamhet över många trades, inte att vinna just den här:
- skydda redan uppnådd vinst när momentum tydligt försvagas,
- men kapa inte vinnare i onödan - en trade som fortfarande har momentum ska
  få nå sitt target; varje stop-utlösning kostar avgift + slippage (~0.2 %),
- begränsa förluster när tesen tydligt är bruten.

Du får exakt det som var känt vid beslutsminuten, inget om framtiden.

Tillåtna beslut:
- HOLD: ingen ändring.
- PROTECT: höj stop till en nivå med låst vinst (new_stop_pct = stopnivå i %
  relativt entry, t.ex. 1.5 betyder entry*1.015).
- TIGHTEN_SL: höj stop (även under entry) till new_stop_pct.
- EXIT: stäng positionen nu till marknadspris.

Hårda regler (upprätthålls även i kod): stop får bara HÖJAS, aldrig sänkas,
aldrig ligga över nuvarande pris; sista SL tas aldrig bort; ingen ökning av
storlek eller hävstång; du öppnar aldrig positioner.

Svara med action, new_stop_pct (null för HOLD/EXIT), confidence 0-1 och
reasoning (1-3 meningar)."""


class ManagerDecision(AssessmentBase):
    action: Literal["HOLD", "PROTECT", "TIGHTEN_SL", "EXIT"]
    new_stop_pct: float | None = None
    confidence: float
    reasoning: str


def decision_points(trade: dict) -> list[dict]:
    """Milestones on the BASE path, each at a bar CLOSE (decision usable from
    the next bar). Only while the BASE trade is still open."""
    base = trade["sim"]["BASE"]
    bars = trade["bars"]
    entry = trade["entry"]
    t0 = bars[0][0]
    end_ms = t0 + base.minutes * 60_000
    peak = entry
    hit = set()
    points = []
    for t, _o, h, _l, c, _v in bars:
        if t >= end_ms:
            break
        peak = max(peak, h)
        mfe = (peak / entry - 1) * 100
        now = (c / entry - 1) * 100
        mins = (t - t0) / 60_000 + 1
        for level in (2.0, 3.0, 4.0):
            if mfe >= level and level not in hit:
                hit.add(level)
                points.append(
                    {"t_ms": t + 60_000, "trigger": f"MFE_{level:g}", "mfe": mfe, "now": now}
                )
        if mfe >= 2.0 and now <= 0.6 * mfe and "RETRACE" not in hit:
            hit.add("RETRACE")
            points.append({"t_ms": t + 60_000, "trigger": "RETRACE_40", "mfe": mfe, "now": now})
        if 59 <= mins < 60 and now <= -2.0 and "EARLY_LOSS" not in hit:
            hit.add("EARLY_LOSS")
            points.append({"t_ms": t + 60_000, "trigger": "EARLY_LOSS_60M", "mfe": mfe, "now": now})
    return points


def context_for(trade: dict, point: dict, paths: Paths, market: Market) -> dict:
    entry, t_ms = trade["entry"], point["t_ms"]
    t = datetime.fromtimestamp(t_ms / 1000, UTC)
    held = [b for b in trade["bars"] if b[0] < t_ms]
    pre = paths.bars(trade["symbol"], t - timedelta(hours=2), t)
    five = _resample(pre, 5)
    peak = max([entry] + [b[2] for b in held])
    trough = min([entry] + [b[3] for b in held])
    stop = trade["sl"] if (peak / entry - 1) < 0.01 else max(trade["sl"], entry)
    price = held[-1][4]
    mstate = market.state(t, trade["symbol"], None)
    return {
        "run_id": "godfather-manager-replay",
        "instrument": trade["symbol"],
        "minutes_in_trade": round((t_ms - trade["bars"][0][0]) / 60_000),
        "entry_price": entry,
        "current_price": price,
        "unrealized_pct": round((price / entry - 1) * 100, 3),
        "unrealized_usdt": round(1000 * (price / entry - 1), 2),
        "mfe_pct_so_far": round((peak / entry - 1) * 100, 3),
        "mae_pct_so_far": round((trough / entry - 1) * 100, 3),
        "current_stop_pct": round((stop / entry - 1) * 100, 3),
        "target_pct": round((trade["tp"] / entry - 1) * 100, 3),
        "distance_to_target_pct": round((trade["tp"] / price - 1) * 100, 3),
        "distance_to_stop_pct": round((stop / price - 1) * 100, 3),
        "max_hold_minutes": 360,
        "trigger": point["trigger"],
        "last_2h_5m_closes_pct_vs_entry": [round((b[4] / entry - 1) * 100, 2) for b in five[-24:]],
        "last_2h_5m_volumes_rel": [
            round(b[5] / (st.mean([x[5] for x in five]) or 1), 2) for b in five[-24:]
        ],
        "rsi_5m": _rsi([b[4] for b in five]),
        "atr15_pct_at_entry": trade["feat"].get("atr15_pct"),
        "market": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in mstate.items()},
        "signal": {
            "triggers": trade.get("triggers"),
            "chg_30m": trade.get("ev_chg_30m"),
            "rsi_30m": trade.get("ev_rsi_30m"),
            "volume_z_30m": trade.get("ev_volz_30m"),
            "funding_pct": trade.get("ev_funding"),
            "forecast_bull_minus_bear": trade.get("forecast_edge"),
        },
        "costs_note": "fee 0.05 % per side, stop slippage ~0.10 %",
    }


def to_event(decision: ManagerDecision, ctx: dict, t_ms: int, entry: float) -> tuple | None:
    if decision.action == "EXIT":
        return (t_ms, "EXIT", 0.0)
    if decision.action in ("PROTECT", "TIGHTEN_SL") and decision.new_stop_pct is not None:
        level = entry * (1 + decision.new_stop_pct / 100)
        current = entry * (1 + ctx["current_stop_pct"] / 100)
        if current < level < ctx["current_price"]:
            return (t_ms, "STOP", level)
    return None  # HOLD, or an invalid stop the safety layer would refuse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    load_dotenv(".env")
    bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    paths = Paths()
    market = Market(paths)
    trades = attach_paths(load_trades(bot, MANUAL_LIVE), paths, market)
    for tr in trades:
        tr["sim"] = {"BASE": simulate(BASE, tr["entry"], tr["sl"], tr["tp"], tr["bars"])}
    jobs = []
    for i, tr in enumerate(trades):
        if tr["sim"]["BASE"] is None:
            continue
        for p in decision_points(tr):
            jobs.append((i, p, context_for(tr, p, paths, market)))
    if args.limit:
        jobs = jobs[: args.limit]
    print("decision points", len(jobs), flush=True)
    agent = AgentDefinition(
        name="godfather_manager_replay", description="replay", tools=[], system_prompt=SYSTEM_PROMPT
    )
    local = threading.local()
    spent = [0.0]
    lock = threading.Lock()

    def ask(job):
        i, p, ctx = job
        with lock:
            if spent[0] > COST_CAP_USD:
                return (i, p, ctx, None, "cost cap")
        if not hasattr(local, "r"):
            local.r = RealClaudeRunner(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                model=os.environ.get("CRYPTO_TRADING_CLAUDE_MODEL", "claude-sonnet-5"),
                timeout_seconds=90.0,
                max_retries=2,
            )
        try:
            d = local.r.run(agent, ctx, ManagerDecision)
            with lock:
                spent[0] += float(getattr(local.r, "last_call_cost_usd", Decimal("0")))
            return (
                i,
                p,
                ctx,
                d if d.status == "ok" else None,
                None if d.status == "ok" else d.status,
            )
        except Exception as exc:  # noqa: BLE001
            return (i, p, ctx, None, f"{type(exc).__name__}: {exc}"[:200])

    with ThreadPoolExecutor(args.workers) as pool:
        answers = list(pool.map(ask, jobs))
    per_trade: dict[int, list] = {}
    records = []
    for i, p, ctx, d, err in answers:
        tr = trades[i]
        rec = {
            "symbol": tr["symbol"],
            "t_entry": tr["t_entry"].isoformat(),
            "period": tr["period"],
            "trigger": p["trigger"],
            "t_ms": p["t_ms"],
            "ctx_unrealized": ctx["unrealized_pct"],
            "ctx_mfe": ctx["mfe_pct_so_far"],
            "error": err,
        }
        if d is not None:
            rec.update(
                action=d.action,
                new_stop_pct=d.new_stop_pct,
                confidence=d.confidence,
                reasoning=d.reasoning,
            )
            ev = to_event(d, ctx, p["t_ms"], tr["entry"])
            rec["applied"] = ev is not None
            if ev:
                per_trade.setdefault(i, []).append(ev)
        records.append(rec)
    trade_rows = []
    for i, tr in enumerate(trades):
        base = tr["sim"]["BASE"]
        if base is None:
            continue
        gf = simulate(
            BASE, tr["entry"], tr["sl"], tr["tp"], tr["bars"], events=tuple(per_trade.get(i, ()))
        )
        trade_rows.append(
            {
                "symbol": tr["symbol"],
                "t_entry": tr["t_entry"].isoformat(),
                "period": tr["period"],
                "is_live": tr["is_live"],
                "base_pnl": base.pnl,
                "gf_pnl": gf.pnl,
                "base_reason": base.reason,
                "gf_reason": gf.reason,
                "mfe": base.mfe_pct,
                "n_events": len(per_trade.get(i, ())),
            }
        )
    OUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "cost_usd": spent[0],
                "decisions": records,
                "trades": trade_rows,
            },
            default=str,
        )
    )
    print(
        "cost",
        round(spent[0], 2),
        "decisions",
        len(records),
        "errors",
        sum(r["error"] is not None for r in records),
        flush=True,
    )


if __name__ == "__main__":
    main()
