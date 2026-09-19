"""Read-only AI-cost-per-LIVE-position report (2026-09-19).

Measures where discovery AI credits go relative to real LIVE positions, so
the effect of the discovery-side capacity/capital gate
(paper_trading/live_discovery_gate.py) can be judged from data - and so later
cost decisions (opportunity_screening_enforce, early role abort, model
changes) are made on evidence, not guesses. Pure reporting: reads the DB in
SQLite read-only mode, never writes, never started by run.py. Run manually:

    python -m crypto_trading.performance.live_ai_cost_report --since 2026-09-19

Definitions (all over the [since, until) window, on occurred_at/claimed_at/
opened_at):
- discovery AI calls/cost: AI_CALL_MADE rows of the 7 pipeline roles only
  (Guardian/Detective/GODFATHER calls are excluded - they are not discovery).
- full analyses: distinct candidates that received at least one such call.
- LIVE orders: live_executions rows whose entry order was really placed.
- cost on candidates that never became LIVE, split in two:
  * no_signal: analysed candidates whose verdict was NO_TRADE/REJECTED/other -
    the normal price of looking; no gate could have avoided it after the fact.
  * confirmed_never_live: candidates the Gate CONFIRMED (they became PAPER)
    but LIVE never opened - blocked by capacity, capital, signal TTL,
    duplicate symbol or exchange minimum. This is the spend the discovery
    gate exists to shrink; it is an upper bound on "wasted because of
    capacity/capital" (it also contains the other reasons above).
- avoided analyses: candidates the gate/stale rule turned into BUDGET_LIMITED
  with reason live_slot_budget / stale_signal instead of analysing them."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

_PIPELINE_ROLES = (
    "news_sentiment", "technical", "bull_thesis", "forecast", "risk", "bear_adversarial", "qa",
)
_GATE_OUTCOMES = (
    "suppressed_capacity", "suppressed_capital", "suppressed_check_failed", "capped", "normal",
)


def _usd(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))


def build_live_ai_cost_report(
    conn: sqlite3.Connection, since: datetime, until: datetime | None = None
) -> dict:
    conn.row_factory = sqlite3.Row
    lo = since.isoformat()
    hi = (until or datetime(9999, 1, 1, tzinfo=UTC)).isoformat()

    def rows(sql: str, *extra) -> list[sqlite3.Row]:
        return conn.execute(sql, (lo, hi, *extra)).fetchall()

    # --- discovery AI spend, per candidate -------------------------------
    cost_by_candidate: dict[str, Decimal] = {}
    calls = 0
    for r in rows(
        "SELECT aggregate_id, payload FROM events WHERE event_type = 'AI_CALL_MADE' "
        "AND occurred_at >= ? AND occurred_at < ?"
    ):
        payload = json.loads(r["payload"])
        if payload.get("role") not in _PIPELINE_ROLES:
            continue
        calls += 1
        cost_by_candidate[r["aggregate_id"]] = cost_by_candidate.get(
            r["aggregate_id"], Decimal("0")
        ) + Decimal(str(payload.get("cost_usd") or "0"))
    total_cost = sum(cost_by_candidate.values(), Decimal("0"))

    # --- outcomes -----------------------------------------------------------
    confirmed = 0
    avoided: Counter[str] = Counter()
    for r in rows(
        "SELECT payload FROM events WHERE event_type = 'CANDIDATE_TRANSITIONED' "
        "AND occurred_at >= ? AND occurred_at < ?"
    ):
        payload = json.loads(r["payload"])
        if payload.get("to") == "CONFIRMED":
            confirmed += 1
        elif payload.get("to") == "BUDGET_LIMITED" and payload.get("reason") in (
            "live_slot_budget", "stale_signal",
        ):
            avoided[payload["reason"]] += 1

    live_ids = {
        r["position_id"]
        for r in rows(
            "SELECT position_id FROM live_executions WHERE entry_exchange_order_id IS NOT NULL "
            "AND claimed_at >= ? AND claimed_at < ?"
        )
    }
    # A LIVE order placed in the window may belong to a candidate analysed
    # slightly earlier; cost attribution below uses ALL live rows so a
    # window edge never misclassifies an analysis as "never LIVE".
    ever_live_ids = {
        r["position_id"]
        for r in conn.execute(
            "SELECT position_id FROM live_executions WHERE entry_exchange_order_id IS NOT NULL"
        )
    }
    status_by_candidate = {
        r["candidate_id"]: r["status"]
        for r in conn.execute("SELECT candidate_id, status FROM candidates")
    }
    cost_no_signal = cost_confirmed_never_live = cost_live = Decimal("0")
    for candidate_id, cost in cost_by_candidate.items():
        if candidate_id in ever_live_ids:
            cost_live += cost
        elif status_by_candidate.get(candidate_id) == "CONFIRMED":
            cost_confirmed_never_live += cost
        else:
            cost_no_signal += cost

    paper_positions = rows(
        "SELECT COUNT(*) AS n FROM positions WHERE opened_at >= ? AND opened_at < ?"
    )[0]["n"]
    ticks = rows(
        "SELECT COUNT(*) AS n FROM runs WHERE run_type = 'discovery' "
        "AND started_at >= ? AND started_at < ?"
    )[0]["n"]

    gate_counts: Counter[str] = Counter()
    for r in rows(
        "SELECT payload FROM events WHERE event_type = 'DISCOVERY_LIVE_GATE' "
        "AND occurred_at >= ? AND occurred_at < ?"
    ):
        gate_counts[json.loads(r["payload"]).get("outcome", "unknown")] += 1
    gate_ticks = sum(gate_counts.values())
    suppressed = sum(v for k, v in gate_counts.items() if k.startswith("suppressed_"))

    live_orders = len(live_ids)
    return {
        "window": {"since": lo, "until": None if until is None else hi},
        "discovery_ticks": ticks,
        "discovery_ai_calls": calls,
        "discovery_ai_cost_usd": _usd(total_cost),
        "full_analyses": len(cost_by_candidate),
        "confirmed": confirmed,
        "live_orders": live_orders,
        "paper_positions": paper_positions,
        "ai_cost_per_live_position_usd": (
            _usd(total_cost / live_orders) if live_orders else None
        ),
        "ai_cost_usd_on_candidates_that_became_live": _usd(cost_live),
        "ai_cost_usd_no_signal": _usd(cost_no_signal),
        "ai_cost_usd_confirmed_never_live": _usd(cost_confirmed_never_live),
        "gate_ticks_recorded": gate_ticks,
        "gate_outcomes": {k: gate_counts.get(k, 0) for k in _GATE_OUTCOMES},
        "gate_blocked_share": (round(suppressed / gate_ticks, 3) if gate_ticks else None),
        "avoided_analyses": {
            "live_slot_budget": avoided.get("live_slot_budget", 0),
            "stale_signal": avoided.get("stale_signal", 0),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="data/crypto_trading.db")
    parser.add_argument("--since", required=True, help="UTC date/datetime, e.g. 2026-09-19")
    parser.add_argument("--until", default=None)
    args = parser.parse_args()

    def parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    uri = f"file:{Path(args.db).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        report = build_live_ai_cost_report(
            conn, parse(args.since), parse(args.until) if args.until else None
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
