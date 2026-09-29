"""Pre-AI feasibility SHADOW - forward report (2026-09-29), read-only.

    python -m crypto_trading.performance.pre_ai_feasibility_report [--since ...]

Joins every shadow verdict with what really happened afterwards: did the
candidate become CONFIRMED, what did the Safety Kernel decide, did it go
LIVE, how did it end. Measures precision of INFEASIBLE, false rejections
(verdict INFEASIBLE but the kernel APPROVED), and the AI cost the verdict
could have saved. The verdict itself never influences any of those.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime

COST_PER_ANALYSIS_USD = 0.133


def rows(conn: sqlite3.Connection, since: datetime) -> list[dict]:
    conn.row_factory = sqlite3.Row
    out = []
    for r in conn.execute(
        "SELECT s.candidate_id, s.feasible, s.record_json, k.status FROM pre_ai_feasibility_shadow s "
        "JOIN candidates k USING(candidate_id) WHERE s.assessed_at >= ?", (since.isoformat(),),
    ).fetchall():
        rec = json.loads(r["record_json"])
        pos = conn.execute("SELECT position_id FROM positions WHERE candidate_id = ?", (r["candidate_id"],)).fetchone()
        kernel = live = None
        if pos is not None:
            k = conn.execute("SELECT action, detail_json FROM safety_kernel_decisions WHERE position_id = ?",
                             (pos["position_id"],)).fetchone()
            kernel = k["action"] if k else None
            x = conn.execute("SELECT phase, exit_reason, exchange_realized_pnl_usdt FROM live_executions "
                             "WHERE position_id = ?", (pos["position_id"],)).fetchone()
            live = dict(x) if x else None
        analysed = conn.execute(
            "SELECT count(*) FROM events WHERE event_type='AI_CALL_MADE' AND aggregate_id = ?",
            (r["candidate_id"],)).fetchone()[0] > 0
        eq = conn.execute("SELECT record_json FROM entry_quality_shadow WHERE candidate_id = ?",
                          (r["candidate_id"],)).fetchone()
        std_r = ((json.loads(eq["record_json"]).get("outcome") or {}).get("r")) if eq else None
        fh = (rec.get("failure_hypotheses") or [{}])[0]
        out.append({"candidate_id": r["candidate_id"], "symbol": rec.get("symbol"), "verdict": r["feasible"],
                    "no_momentum_4h": fh.get("flag"), "std_r": std_r,
                    "reason": rec.get("reason"), "stop_interval": rec.get("estimated_stop_pct_interval"),
                    "estimate_fits": (rec.get("shadow_estimate") or {}).get("fits"),
                    "status": r["status"], "ai_analysed": analysed, "kernel": kernel, "live": live})
    return out


def _flag_split(rs: list[dict]) -> dict:
    """Forward test of the NO_4H_MOMENTUM failure hypothesis: standardized
    outcome (Entry Quality Layer, 6 h later) of flagged vs not flagged."""
    out = {}
    for name, sel in (("flagged", [r for r in rs if r["no_momentum_4h"] is True]),
                      ("not_flagged", [r for r in rs if r["no_momentum_4h"] is False])):
        rr = [r["std_r"] for r in sel if r["std_r"] is not None]
        out[name] = {"n": len(sel), "n_with_outcome": len(rr),
                     "mean_std_r": round(sum(rr) / len(rr), 4) if rr else None,
                     "kernel_approve": sum(r["kernel"] == "APPROVE" for r in sel),
                     "ai_cost_usd": round(sum(r["ai_analysed"] for r in sel) * COST_PER_ANALYSIS_USD, 2)}
    return out


def summarize(conn: sqlite3.Connection, since: datetime) -> dict:
    rs = rows(conn, since)
    inf = [r for r in rs if r["verdict"] == "false"]
    judged = [r for r in inf if r["kernel"] is not None]
    return {
        "since": since.isoformat(), "n": len(rs), "verdicts": dict(Counter(r["verdict"] for r in rs)),
        "reasons": dict(Counter(r["reason"] for r in rs)),
        "confirmed": sum(r["status"] == "CONFIRMED" for r in rs),
        "kernel_decisions": dict(Counter(f'{r["verdict"]}->{r["kernel"]}' for r in rs if r["kernel"])),
        "infeasible_precision_vs_kernel": (round(sum(r["kernel"] == "REJECT" for r in judged) / len(judged), 3)
                                           if judged else None),
        "false_rejections_kernel_approved": sum(r["kernel"] == "APPROVE" for r in inf),
        "went_live": sum(1 for r in rs if r["live"]),
        "ai_cost_avoidable_usd": round(sum(r["ai_analysed"] for r in inf) * COST_PER_ANALYSIS_USD, 2),
        "no_momentum_4h": _flag_split(rs),
        "estimate_would_skip": sum(1 for r in rs if r["estimate_fits"] is False),
        "estimate_false_rejections_kernel_approved": sum(
            1 for r in rs if r["estimate_fits"] is False and r["kernel"] == "APPROVE"),
    }


def main() -> None:
    from crypto_trading.config.loader import get_settings

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2000-01-01T00:00:00+00:00")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{get_settings().db_path}?mode=ro", uri=True)
    print(json.dumps(summarize(conn, datetime.fromisoformat(args.since.replace("Z", "+00:00"))), indent=1))


if __name__ == "__main__":
    main()
